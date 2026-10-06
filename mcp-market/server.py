"""
行情 MCP —— 只读。

安全契约（刻意写成硬约束）：
  * 本容器内不存在任何 API key / secret 环境变量。
  * 只调用 Binance 的公开 REST 端点（/api/v3/*，无需签名）。
  * 代码里没有、也不会引入任何下单/撤单/资金划转的调用路径。
  即使 Agent 被完全策反，它在这个容器里能做的上限就是「读公开行情」。
"""
from __future__ import annotations

import math
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

BASE = os.environ.get("MARKET_API_BASE", "https://api.binance.com").rstrip("/")
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "15"))
PORT = int(os.environ.get("MCP_PORT", "8081"))

mcp = FastMCP("market")
mcp.settings.host = "0.0.0.0"
mcp.settings.port = PORT

# MCP 的 streamable-http 默认开启 DNS 重绑定保护，只放行 localhost。
# 容器之间靠服务名互访，必须把服务名加进允许列表，否则会收到 421。
_ALLOWED = [
    h.strip()
    for h in os.environ.get(
        "MCP_ALLOWED_HOSTS",
        "localhost,127.0.0.1,[::1],mcp-market,trader-mcp-market,mcp-paper,trader-mcp-paper,brain,trader-brain",
    ).split(",")
    if h.strip()
]
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[p for host in _ALLOWED for p in (host, f"{host}:*")],
    allowed_origins=["*"],
)

_http: httpx.AsyncClient | None = None

VALID_INTERVALS = {
    "1s", "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}


async def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": "trader-harness/1.0"},
            follow_redirects=True,
        )
    return _http


async def _get(path: str, **params: Any) -> Any:
    client = await http()
    clean = {k: v for k, v in params.items() if v is not None}
    resp = await client.get(f"{BASE}{path}", params=clean)
    if resp.status_code >= 400:
        raise RuntimeError(f"binance {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _sym(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")
    if not s or len(s) > 24 or not s.isalnum():
        raise ValueError(f"invalid symbol: {symbol!r}")
    return s


def _iv(interval: str) -> str:
    i = str(interval or "1h").strip()
    if i not in VALID_INTERVALS:
        raise ValueError(f"invalid interval {interval!r}; valid: {sorted(VALID_INTERVALS)}")
    return i


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# 指标计算（纯 Python，不引入 numpy/pandas，镜像保持小）
# --------------------------------------------------------------------------
def _sma(v: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(v)
    if len(v) < n:
        return out
    s = sum(v[:n])
    out[n - 1] = s / n
    for i in range(n, len(v)):
        s += v[i] - v[i - n]
        out[i] = s / n
    return out


def _ema(v: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(v)
    if len(v) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(v[:n]) / n
    out[n - 1] = e
    for i in range(n, len(v)):
        e = v[i] * k + e * (1 - k)
        out[i] = e
    return out


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    a = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
    return a


def _stdev(v: list[float]) -> float:
    if len(v) < 2:
        return 0.0
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
@mcp.tool()
async def get_price(symbol: str) -> dict:
    """获取交易对的最新成交价。symbol 形如 BTCUSDT。"""
    s = _sym(symbol)
    data = await _get("/api/v3/ticker/price", symbol=s)
    return {"symbol": data["symbol"], "price": _f(data["price"])}


@mcp.tool()
async def get_prices(symbols: list[str]) -> list[dict]:
    """批量获取最新价。一次拿多个，比逐个调用省时间。"""
    syms = [_sym(s) for s in symbols][:40]
    if not syms:
        return []
    # ★ 必须序列化成【无空格】的 JSON 数组。
    # Binance 对 symbols 这个参数做正则校验，合法形状是 ["A","B"]，
    # 而 str(list) 会产出 ['A', 'B'] —— 把单引号换成双引号之后逗号后面
    # 还留着一个空格，服务端直接 400（code -1100）。
    # 这个工具长期是坏的，只是因为以前只有批量形态的 get_market_overview 被用过，
    # 没人踩到。价格监控成了第一个真实用户才暴露出来。
    payload = "[" + ",".join(f'"{s}"' for s in syms) + "]"
    data = await _get("/api/v3/ticker/price", symbols=payload)
    return [{"symbol": d["symbol"], "price": _f(d["price"])} for d in data]


@mcp.tool()
async def get_24hr_ticker(symbol: str) -> dict:
    """24 小时行情统计：涨跌幅、最高最低、成交量、成交额。判断当下热度用。"""
    s = _sym(symbol)
    d = await _get("/api/v3/ticker/24hr", symbol=s)
    return {
        "symbol": d["symbol"],
        "last": _f(d["lastPrice"]),
        "change_pct_24h": _f(d["priceChangePercent"]),
        "change_24h": _f(d["priceChange"]),
        "high_24h": _f(d["highPrice"]),
        "low_24h": _f(d["lowPrice"]),
        "open_24h": _f(d["openPrice"]),
        "volume_base": _f(d["volume"]),
        "volume_quote": _f(d["quoteVolume"]),
        "trades": int(d.get("count", 0)),
        "weighted_avg_price": _f(d["weightedAvgPrice"]),
    }


@mcp.tool()
async def get_klines(symbol: str, interval: str = "1h", limit: int = 60) -> dict:
    """
    获取 K 线（OHLCV）。interval 可为 1m/5m/15m/1h/4h/1d/1w 等；limit 1-500。

    返回紧凑的列式数据以节省上下文：candles 是 [时间戳, 开, 高, 低, 收, 量] 的数组。
    默认只返回最近 12 根 —— 大多数判断用 get_technical_snapshot 就够了，
    只有确实需要逐根检视形态时才来这里，并把 limit 调大。
    """
    s, iv = _sym(symbol), _iv(interval)
    n = max(1, min(int(limit), 500))
    raw = await _get("/api/v3/klines", symbol=s, interval=iv, limit=n)
    candles = [
        [int(k[0]), _f(k[1]), _f(k[2]), _f(k[3]), _f(k[4]), round(_f(k[5]), 4)]
        for k in raw
    ]
    return {
        "symbol": s,
        "interval": iv,
        "count": len(candles),
        "cols": "[ts,open,high,low,close,volume]",
        "candles": candles[-12:] if n > 12 else candles,
    }


@mcp.tool()
async def get_orderbook(symbol: str, levels: int = 5, include_levels: bool = False) -> dict:
    """
    盘口快照：点差、买卖压力失衡（imbalance）。

    默认只给汇总数字，不返回逐档明细 —— 逐档数据很占上下文，
    只有你确实要判断大单挂单位置时才把 include_levels 设成 true。
    """
    s = _sym(symbol)
    n = max(5, min(int(levels), 50))
    d = await _get("/api/v3/depth", symbol=s, limit=20)
    bids = [[_f(b[0]), _f(b[1])] for b in d["bids"][:n]]
    asks = [[_f(a[0]), _f(a[1])] for a in d["asks"][:n]]
    bid_vol = sum(b[1] for b in bids)
    ask_vol = sum(a[1] for a in asks)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = (best_ask - best_bid) if (best_bid and best_ask) else None
    out = {
        "symbol": s,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": round(spread / best_bid * 10000, 3) if (spread and best_bid) else None,
        "bid_volume_top": round(bid_vol, 4),
        "ask_volume_top": round(ask_vol, 4),
        "imbalance": round((bid_vol - ask_vol) / (bid_vol + ask_vol), 4) if (bid_vol + ask_vol) else None,
        "reading": (
            "买盘更厚（下方有支撑）" if bid_vol > ask_vol * 1.5
            else "卖盘更厚（上方有压力）" if ask_vol > bid_vol * 1.5
            else "买卖均衡"
        ),
    }
    if include_levels:
        out["cols"] = "[price,qty]"
        out["bids"] = bids
        out["asks"] = asks
    return out


@mcp.tool()
async def get_technical_snapshot(symbol: str, interval: str = "1h", limit: int = 300) -> dict:
    """
    一次性给出该交易对的技术面摘要：均线、RSI、MACD、ATR、布林带、量能趋势、
    近期波动率与位置（相对 24h/区间高低点）。
    本工具在服务端算好数字，避免你自己算错。这是判断入场时机的主工具。
    """
    s, iv = _sym(symbol), _iv(interval)
    n = max(60, min(int(limit), 500))
    raw = await _get("/api/v3/klines", symbol=s, interval=iv, limit=n)

    opens = [_f(k[1]) for k in raw]
    highs = [_f(k[2]) for k in raw]
    lows = [_f(k[3]) for k in raw]
    closes = [_f(k[4]) for k in raw]
    vols = [_f(k[5]) for k in raw]

    last = closes[-1]
    sma20 = _sma(closes, 20)[-1]
    sma50 = _sma(closes, 50)[-1]
    sma200 = _sma(closes, 200)[-1]
    ema12 = _ema(closes, 12)[-1]
    ema26 = _ema(closes, 26)[-1]
    macd_line = (ema12 - ema26) if (ema12 is not None and ema26 is not None) else None

    macd_series = []
    e12, e26 = _ema(closes, 12), _ema(closes, 26)
    for a, b in zip(e12, e26):
        macd_series.append((a - b) if (a is not None and b is not None) else None)
    macd_clean = [x for x in macd_series if x is not None]
    macd_signal = _ema(macd_clean, 9)[-1] if len(macd_clean) >= 9 else None
    histogram = (macd_line - macd_signal) if (macd_line is not None and macd_signal is not None) else None

    rsi14 = _rsi(closes, 14)
    atr14 = _atr(highs, lows, closes, 14)

    window = closes[-20:]
    mid = sum(window) / len(window)
    sd = _stdev(window)
    bb_upper, bb_lower = mid + 2 * sd, mid - 2 * sd

    recent_vol = sum(vols[-5:]) / 5
    base_vol = sum(vols[-20:]) / 20
    vol_ratio = (recent_vol / base_vol) if base_vol else None

    hi, lo = max(highs), min(lows)
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes)) if closes[i - 1]
    ]
    volatility = _stdev(rets[-30:]) if len(rets) >= 5 else None

    def r(x: float | None, nd: int = 6) -> float | None:
        return round(x, nd) if isinstance(x, (int, float)) else None

    return {
        "symbol": s,
        "interval": iv,
        "candles_used": len(closes),
        "note": (
            f"本周期只拿到 {len(closes)} 根 K 线，不足 200 根，sma200 为 null。"
            "若需要 sma200 请把 limit 提到 200 以上。"
        ) if sma200 is None else None,
        "last_close": last,
        "trend": {
            "sma20": r(sma20), "sma50": r(sma50), "sma200": r(sma200),
            "price_vs_sma20_pct": r((last / sma20 - 1) * 100 if sma20 else None, 3),
            "price_vs_sma50_pct": r((last / sma50 - 1) * 100 if sma50 else None, 3),
            "price_vs_sma200_pct": r((last / sma200 - 1) * 100 if sma200 else None, 3),
            "ema12": r(ema12), "ema26": r(ema26),
            "stack": (
                "bullish" if (sma20 and sma50 and sma200 and last > sma20 > sma50 > sma200)
                else "bearish" if (sma20 and sma50 and sma200 and last < sma20 < sma50 < sma200)
                else "mixed"
            ),
        },
        "momentum": {
            "rsi14": r(rsi14, 2),
            "rsi_state": (
                "overbought" if (rsi14 or 0) > 70
                else "oversold" if (rsi14 or 100) < 30
                else "neutral"
            ),
            "macd": r(macd_line), "macd_signal": r(macd_signal), "macd_hist": r(histogram),
            "macd_state": (
                "bullish" if (histogram or 0) > 0 else "bearish" if histogram is not None else None
            ),
        },
        "volatility": {
            "atr14": r(atr14),
            "atr_pct_of_price": r((atr14 / last * 100) if (atr14 and last) else None, 3),
            "stdev_30bar_returns": r(volatility, 5),
            "bb_upper": r(bb_upper), "bb_mid": r(mid), "bb_lower": r(bb_lower),
            "bb_position_pct": r((last - bb_lower) / (bb_upper - bb_lower) * 100 if (bb_upper - bb_lower) else None, 2),
            "suggested_stop_distance_pct": r((atr14 / last * 200) if (atr14 and last) else None, 3),
        },
        "volume": {
            "recent_5_vs_20_ratio": r(vol_ratio, 3),
            "volume_trend": (
                "expanding" if (vol_ratio or 0) > 1.3
                else "contracting" if (vol_ratio or 1) < 0.7
                else "normal"
            ),
        },
        "range_position": {
            "high": hi, "low": lo,
            "pct_from_high": r((last / hi - 1) * 100, 3),
            "pct_above_low": r((last / lo - 1) * 100, 3),
            "where_in_range_pct": r((last - lo) / (hi - lo) * 100 if (hi - lo) else None, 2),
        },
    }


@mcp.tool()
async def list_symbols(quote: str = "USDT", limit: int = 60, search: str = "") -> dict:
    """列出可交易的现货交易对。quote 默认 USDT；search 可按关键字过滤（如 'AI'）。"""
    q = str(quote or "USDT").strip().upper()
    info = await _get("/api/v3/exchangeInfo")
    out = []
    needle = str(search or "").strip().upper()
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != q:
            continue
        if s.get("isSpotTradingAllowed") is False:
            continue
        sym = s["symbol"]
        if needle and needle not in sym:
            continue
        out.append(sym)
    out.sort()
    return {"quote": q, "total": len(out), "symbols": out[: max(1, min(int(limit), 300))]}


@mcp.tool()
async def get_fear_greed(limit: int = 7) -> dict:
    """
    加密市场恐慌贪婪指数（alternative.me 公开数据，0=极度恐慌 100=极度贪婪）。
    作为宏观情绪的一个参考锚，不是交易信号本身。
    """
    c = await http()
    n = max(1, min(int(limit), 30))
    resp = await c.get("https://api.alternative.me/fng/", params={"limit": n})
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return {
        "now": {"value": int(data[0]["value"]), "label": data[0]["value_classification"]} if data else None,
        "history": [
            {"value": int(d["value"]), "label": d["value_classification"]}
            for d in data
        ],
        "source": "alternative.me",
    }


@mcp.tool()
async def get_market_overview(symbols: list[str] | None = None) -> dict:
    """
    一次性给出关注列表的全景：现价、24h 涨跌、24h 成交额、以及该交易对的
    短期技术状态。心跳开始时先调这个，快速建立盘面感知。
    """
    watch = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    syms = [_sym(s) for s in watch][:15] or ["BTCUSDT"]

    tickers = []
    for s in syms:
        try:
            t = await get_24hr_ticker(s)
            tickers.append({
                "symbol": t["symbol"],
                "last": t["last"],
                "change_pct_24h": t["change_pct_24h"],
                "quote_volume_24h": t["volume_quote"],
                "high_24h": t["high_24h"],
                "low_24h": t["low_24h"],
            })
        except Exception as exc:  # 单个交易对失败不该拖垮整个概览
            tickers.append({"symbol": s, "error": str(exc)[:120]})

    ranked = sorted(
        [t for t in tickers if "change_pct_24h" in t],
        key=lambda x: x["change_pct_24h"],
        reverse=True,
    )
    return {
        "tickers": tickers,
        "strongest_24h": ranked[0] if ranked else None,
        "weakest_24h": ranked[-1] if ranked else None,
        "count": len(tickers),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
