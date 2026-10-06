"""
模拟盘 MCP —— 唯一允许"成交"的地方，成交完全发生在本地 SQLite 里。

三条硬性事实：
  1. 本容器不含任何交易所 API key，也没有任何真实下单的代码路径。
     所谓"买入"只是在 SQLite 里改一行数字。
  2. 风控是代码，不是提示词。仓位上限、持仓数上限、日亏熔断、手续费与滑点
     都在 buy()/sell() 内部强制执行。Agent 无法通过"说服"绕过。
  3. 成交价取真实盘口（买吃卖一、卖吃买一），并叠加滑点与手续费，
     所以模拟结果不会比实盘更乐观。

止损/止盈由独立的后台线程每 30 秒巡视一次，到价自动平仓并写入事件流，
这样两次心跳之间的暴跌也能被捕捉到。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

BASE = os.environ.get("MARKET_API_BASE", "https://api.binance.com").rstrip("/")
DB_PATH = os.environ.get("PAPER_DB", "/data/db/trader.db")
PORT = int(os.environ.get("MCP_PORT", "8082"))
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "15"))

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10000"))
MAX_POSITIONS = int(os.environ.get("RISK_MAX_POSITIONS", "5"))
MAX_POSITION_PCT = float(os.environ.get("RISK_MAX_POSITION_PCT", "0.25"))
MAX_DAILY_LOSS_PCT = float(os.environ.get("RISK_MAX_DAILY_LOSS_PCT", "0.06"))
MIN_NOTIONAL = float(os.environ.get("RISK_MIN_NOTIONAL", "20"))
FEE_BPS = float(os.environ.get("RISK_FEE_BPS", "10"))
SLIPPAGE_BPS = float(os.environ.get("RISK_SLIPPAGE_BPS", "5"))
STOP_WATCH_INTERVAL = float(os.environ.get("STOP_WATCH_INTERVAL", "30"))

# --- 睡眠配额。和上面的 RISK_* 一样是硬约束，不是建议。---
# 一天最少睡多久、单次最长睡多久、连续清醒多久必须去睡。
# 这三个数决定了 Agent 每天最多跑多少轮心跳，也就是成本上限。
SLEEP_MIN_HOURS_PER_DAY = float(os.environ.get("SLEEP_MIN_HOURS_PER_DAY", "6.5"))
SLEEP_MAX_SINGLE_HOURS = float(os.environ.get("SLEEP_MAX_SINGLE_HOURS", "11"))
SLEEP_MAX_AWAKE_HOURS = float(os.environ.get("SLEEP_MAX_AWAKE_HOURS", "17.5"))
SLEEP_WINDOW_HOURS = 24.0
ALLOW_RESET = os.environ.get("ALLOW_RESET", "false").lower() == "true"

mcp = FastMCP("paper")
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


# ==========================================================================
# 基础设施
# ==========================================================================
def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                cash          REAL NOT NULL,
                initial_cash  REAL NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                symbol        TEXT PRIMARY KEY,
                qty           REAL NOT NULL,
                avg_cost      REAL NOT NULL,
                opened_at     TEXT NOT NULL,
                stop_loss     REAL,
                take_profit   REAL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                side          TEXT NOT NULL,
                qty           REAL NOT NULL,
                price         REAL NOT NULL,
                notional      REAL NOT NULL,
                fee           REAL NOT NULL,
                realized_pnl  REAL NOT NULL DEFAULT 0,
                reason        TEXT
            );
            CREATE TABLE IF NOT EXISTS equity (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                cash          REAL NOT NULL,
                equity        REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key           TEXT PRIMARY KEY,
                value         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sleep_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at    REAL NOT NULL,
                ended_at      REAL,
                reason        TEXT,
                end_reason    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
            CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
            """
        )
        c.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('sleep_first_seen', ?)",
            (str(time.time()),),
        )
        row = c.execute("SELECT id FROM account WHERE id = 1").fetchone()
        if not row:
            c.execute(
                "INSERT INTO account (id, cash, initial_cash, created_at) VALUES (1, ?, ?, ?)",
                (INITIAL_CASH, INITIAL_CASH, now()),
            )


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def sym(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("/", "").replace("-", "")
    if not s or len(s) > 24 or not s.isalnum():
        raise ValueError(f"invalid symbol: {symbol!r}")
    return s


# ==========================================================================
# 行情取价（只读公共端点）
# ==========================================================================
_http = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "trader-paper/1.0"})
_price_cache: dict[str, tuple[float, float, float]] = {}  # s -> (ts, bid, ask)


def quote(symbol: str, max_age: float = 5.0) -> tuple[float, float]:
    """返回 (bid, ask)。失败则抛错——绝不静默拿一个假价格去成交。"""
    s = sym(symbol)
    cached = _price_cache.get(s)
    if cached and time.time() - cached[0] < max_age:
        return cached[1], cached[2]
    r = _http.get(f"{BASE}/api/v3/ticker/bookTicker", params={"symbol": s})
    if r.status_code >= 400:
        raise RuntimeError(f"取价失败 {s}: {r.status_code} {r.text[:200]}")
    d = r.json()
    bid, ask = float(d["bidPrice"]), float(d["askPrice"])
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"取价失败 {s}: 盘口为空")
    _price_cache[s] = (time.time(), bid, ask)
    return bid, ask


def mark_price(symbol: str) -> float:
    bid, ask = quote(symbol)
    return (bid + ask) / 2.0


# ==========================================================================
# 账户计算
# ==========================================================================
def _positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM positions ORDER BY symbol"))


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    cash = float(acc["cash"])
    rows = _positions(conn)
    pos_out: list[dict[str, Any]] = []
    market_value = 0.0
    unrealized = 0.0
    for p in rows:
        try:
            mark = mark_price(p["symbol"])
        except Exception as exc:
            mark = float(p["avg_cost"])
            pos_out.append({
                "symbol": p["symbol"], "qty": p["qty"],
                "avg_cost": p["avg_cost"], "price_error": str(exc)[:120],
            })
            continue
        mv = p["qty"] * mark
        pnl = (mark - p["avg_cost"]) * p["qty"]
        market_value += mv
        unrealized += pnl
        pos_out.append({
            "symbol": p["symbol"],
            "qty": round(p["qty"], 8),
            "avg_cost": round(p["avg_cost"], 8),
            "mark": round(mark, 8),
            "market_value": round(mv, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_pct": round((mark / p["avg_cost"] - 1) * 100, 2) if p["avg_cost"] else None,
            "stop_loss": p["stop_loss"],
            "take_profit": p["take_profit"],
            "opened_at": p["opened_at"],
        })

    equity = cash + market_value
    realized = float(
        conn.execute("SELECT COALESCE(SUM(realized_pnl),0) AS s FROM trades").fetchone()["s"]
    )
    fees = float(conn.execute("SELECT COALESCE(SUM(fee),0) AS s FROM trades").fetchone()["s"])
    initial = float(acc["initial_cash"])
    return {
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "initial_cash": round(initial, 2),
        "total_return_pct": round((equity / initial - 1) * 100, 3) if initial else None,
        "total_pnl": round(equity - initial, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "fees_paid": round(fees, 2),
        "open_positions": len(pos_out),
        "positions": pos_out,
        "as_of": now(),
    }


def day_anchor(conn: sqlite3.Connection, equity: float) -> float:
    """记录当日开盘权益，用于日亏熔断。跨日自动重置。"""
    d = today()
    if get_meta(conn, "day_date") != d:
        set_meta(conn, "day_date", d)
        set_meta(conn, "day_equity", str(equity))
        return equity
    return float(get_meta(conn, "day_equity", str(equity)))


def risk_gate(conn: sqlite3.Connection) -> dict[str, Any]:
    """返回当前是否允许开新仓，以及原因。这是熔断的唯一判据。"""
    acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    cash = float(acc["cash"])
    held = _positions(conn)
    try:
        mv = sum(p["qty"] * mark_price(p["symbol"]) for p in held)
    except Exception:
        mv = sum(p["qty"] * p["avg_cost"] for p in held)
    equity = cash + mv
    anchor = day_anchor(conn, equity)
    day_pnl_pct = (equity / anchor - 1) if anchor else 0.0
    halted = day_pnl_pct <= -MAX_DAILY_LOSS_PCT
    return {
        "equity": round(equity, 2),
        "day_anchor_equity": round(anchor, 2),
        "day_pnl_pct": round(day_pnl_pct * 100, 3),
        "day_loss_limit_pct": round(MAX_DAILY_LOSS_PCT * 100, 2),
        "halted": halted,
        "halt_reason": (
            f"当日亏损 {day_pnl_pct * 100:.2f}% 已触及熔断线 "
            f"-{MAX_DAILY_LOSS_PCT * 100:.2f}%，禁止开新仓（平仓不受限制）"
        ) if halted else None,
    }


# ==========================================================================
# 成交
# ==========================================================================
def _execute(
    conn: sqlite3.Connection,
    symbol: str,
    side: str,
    qty: float,
    reason: str | None,
) -> dict[str, Any]:
    """按真实盘口成交，叠加滑点与手续费。qty 必须为正。"""
    bid, ask = quote(symbol)
    if side == "BUY":
        raw = ask
        price = raw * (1 + SLIPPAGE_BPS / 10000.0)
    else:
        raw = bid
        price = raw * (1 - SLIPPAGE_BPS / 10000.0)

    notional = qty * price
    fee = notional * FEE_BPS / 10000.0

    acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    cash = float(acc["cash"])
    pos = conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()

    if side == "BUY":
        if cash < notional + fee:
            raise ValueError(
                f"现金不足：需要 {notional + fee:.2f} USDT（含手续费），可用 {cash:.2f}"
            )
        conn.execute("UPDATE account SET cash = cash - ? WHERE id = 1", (notional + fee,))
        if pos:
            new_qty = pos["qty"] + qty
            new_cost = (pos["qty"] * pos["avg_cost"] + notional + fee) / new_qty
            conn.execute(
                "UPDATE positions SET qty = ?, avg_cost = ? WHERE symbol = ?",
                (new_qty, new_cost, symbol),
            )
        else:
            conn.execute(
                "INSERT INTO positions (symbol, qty, avg_cost, opened_at) VALUES (?,?,?,?)",
                (symbol, qty, (notional + fee) / qty, now()),
            )
        realized = 0.0
    else:
        if not pos or pos["qty"] < qty - 1e-12:
            held = pos["qty"] if pos else 0.0
            raise ValueError(f"持仓不足：想卖 {qty} {symbol}，实际持有 {held}")
        realized = (price - pos["avg_cost"]) * qty - fee
        conn.execute("UPDATE account SET cash = cash + ? WHERE id = 1", (notional - fee,))
        remaining = pos["qty"] - qty
        if remaining <= 1e-12:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        else:
            conn.execute("UPDATE positions SET qty = ? WHERE symbol = ?", (remaining, symbol))

    conn.execute(
        "INSERT INTO trades (ts, symbol, side, qty, price, notional, fee, realized_pnl, reason)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (now(), symbol, side, qty, price, notional, fee, realized, reason),
    )
    return {
        "filled": True,
        "symbol": symbol,
        "side": side,
        "qty": round(qty, 8),
        "raw_market_price": round(raw, 8),
        "fill_price": round(price, 8),
        "slippage_bps": SLIPPAGE_BPS,
        "notional": round(notional, 2),
        "fee": round(fee, 2),
        "realized_pnl": round(realized, 2),
        "reason": reason,
        "ts": now(),
    }


# ==========================================================================
# 工具
# ==========================================================================
@mcp.tool()
def get_risk_limits() -> dict:
    """
    返回本模拟盘强制执行的硬性风控参数。这些数字是系统配置，不是建议，
    你无法修改；每次开仓前请确认自己的意图没有越线。开仓前先看这个。
    """
    return {
        "initial_cash": INITIAL_CASH,
        "max_open_positions": MAX_POSITIONS,
        "max_position_pct_of_equity": round(MAX_POSITION_PCT * 100, 2),
        "max_daily_loss_pct": round(MAX_DAILY_LOSS_PCT * 100, 2),
        "min_order_notional": MIN_NOTIONAL,
        "fee_bps_round_trip": FEE_BPS * 2,
        "slippage_bps_per_side": SLIPPAGE_BPS,
        "note": (
            "买入按卖一价 + 滑点成交，卖出按买一价 - 滑点成交，双边收手续费。"
            f"因此一次完整的买卖往返成本约为 {(FEE_BPS * 2 + SLIPPAGE_BPS * 2) / 100:.2f}%，"
            "任何策略的期望收益必须显著大于这个数才值得做。"
        ),
    }


@mcp.tool()
def get_account() -> dict:
    """
    账户总览：现金、持仓市值、总权益、总收益率、已实现/未实现盈亏、累计手续费、
    以及每个持仓的成本/现价/浮盈/止损止盈位。任何决策前先看这里。
    """
    with db() as c:
        data = snapshot(c)
        gate = risk_gate(c)
        if gate["halted"]:
            data["WARNING"] = gate["halt_reason"]
        data["day_pnl_pct"] = gate["day_pnl_pct"]
        return data


@mcp.tool()
def get_positions() -> list[dict]:
    """只返回当前持仓明细（比 get_account 轻量）。"""
    with db() as c:
        return snapshot(c)["positions"]


@mcp.tool()
def get_trade_history(limit: int = 30) -> list[dict]:
    """最近的成交记录，含每笔的成交价、数量、费用、已实现盈亏和当时写的理由。"""
    n = max(1, min(int(limit), 500))
    with db() as c:
        rows = c.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return [
        {
            "id": r["id"], "ts": r["ts"], "symbol": r["symbol"], "side": r["side"],
            "qty": round(r["qty"], 8), "price": round(r["price"], 8),
            "notional": round(r["notional"], 2), "fee": round(r["fee"], 2),
            "realized_pnl": round(r["realized_pnl"], 2), "reason": r["reason"],
        }
        for r in rows
    ]


@mcp.tool()
def get_equity_curve(limit: int = 200) -> list[dict]:
    """权益曲线采样点（时间 / 现金 / 总权益），用来判断自己的曲线形态。"""
    n = max(2, min(int(limit), 2000))
    with db() as c:
        rows = c.execute("SELECT * FROM equity ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [
        {"ts": r["ts"], "cash": round(r["cash"], 2), "equity": round(r["equity"], 2)}
        for r in reversed(rows)
    ]


@mcp.tool()
def get_performance() -> dict:
    """
    交易绩效统计：胜率、平均盈利/亏损、盈亏比、最大连续亏损、
    最好和最差的一笔。用来做复盘，别只看总权益。
    """
    with db() as c:
        closes = [r for r in c.execute(
            "SELECT * FROM trades WHERE side='SELL' ORDER BY id"
        ).fetchall()]
        snaps = [r["equity"] for r in c.execute("SELECT equity FROM equity ORDER BY id").fetchall()]

    wins = [t["realized_pnl"] for t in closes if t["realized_pnl"] > 0]
    losses = [t["realized_pnl"] for t in closes if t["realized_pnl"] < 0]
    streak = worst_streak = 0
    for t in closes:
        if t["realized_pnl"] < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    peak, max_dd = None, 0.0
    for e in snaps:
        peak = e if peak is None else max(peak, e)
        if peak:
            max_dd = max(max_dd, (peak - e) / peak * 100)

    return {
        "closed_trades": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closes) * 100, 2) if closes else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) != 0 else None,
        "gross_profit": round(sum(wins), 2),
        "gross_loss": round(sum(losses), 2),
        "worst_losing_streak": worst_streak,
        "max_drawdown_pct": round(max_dd, 3),
        "best_trade": round(max([t["realized_pnl"] for t in closes]), 2) if closes else None,
        "worst_trade": round(min([t["realized_pnl"] for t in closes]), 2) if closes else None,
    }


@mcp.tool()
def buy(
    symbol: str,
    quote_amount: float,
    reason: str,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict:
    """
    市价买入。quote_amount 是打算花掉的计价货币数量（USDT），不是币的数量。
    reason 必填：写清楚这笔交易的逻辑，将来复盘要靠它。

    stop_loss_pct / take_profit_pct 为相对入场价的百分比（如 3 表示 -3% / +3%），
    强烈建议至少给 stop_loss_pct —— 设了之后即使你睡着了后台也会替你止损。

    会被风控拒绝的情形：超过单币种仓位上限、持仓数已满、单笔金额过小、
    现金不足、或当日亏损已触发熔断。被拒绝不是错误，是系统在保护你；
    请读 rejection_reason 然后调整，而不是反复重试同一个单子。
    """
    s = sym(symbol)
    amt = float(quote_amount)
    if amt <= 0:
        return {"filled": False, "rejection_reason": "quote_amount 必须为正数"}
    if amt < MIN_NOTIONAL:
        return {
            "filled": False,
            "rejection_reason": f"下单金额 {amt:.2f} USDT 低于最小下单额 {MIN_NOTIONAL} USDT",
        }
    if not str(reason or "").strip():
        return {"filled": False, "rejection_reason": "reason 必填：请写下这笔交易的逻辑"}

    with db() as c:
        gate = risk_gate(c)
        if gate["halted"]:
            return {"filled": False, "rejection_reason": gate["halt_reason"], "risk": gate}

        equity = gate["equity"]
        cap = equity * MAX_POSITION_PCT
        pos = c.execute("SELECT * FROM positions WHERE symbol = ?", (s,)).fetchone()
        existing_mv = 0.0
        if pos:
            try:
                existing_mv = pos["qty"] * mark_price(s)
            except Exception:
                existing_mv = pos["qty"] * pos["avg_cost"]
        if existing_mv + amt > cap + 1e-9:
            room = max(0.0, cap - existing_mv)
            return {
                "filled": False,
                "rejection_reason": (
                    f"超出单币种仓位上限：{s} 最多占总权益 {MAX_POSITION_PCT*100:.0f}% "
                    f"（{cap:.2f} USDT）。当前该币已有 {existing_mv:.2f}，"
                    f"本次最多还能买 {room:.2f} USDT。"
                ),
                "max_allowed_quote_amount": round(room, 2),
                "risk": gate,
            }

        held = _positions(c)
        if not pos and len(held) >= MAX_POSITIONS:
            return {
                "filled": False,
                "rejection_reason": (
                    f"持仓数已达上限 {MAX_POSITIONS}，且 {s} 不在现有持仓中。"
                    f"请先减仓，或改为加仓已持有的品种。当前持仓：{[p['symbol'] for p in held]}"
                ),
                "risk": gate,
            }

        acc = c.execute("SELECT cash FROM account WHERE id = 1").fetchone()
        if float(acc["cash"]) < amt * 1.02:
            return {
                "filled": False,
                "rejection_reason": f"可用现金不足：{float(acc['cash']):.2f} USDT，本单约需 {amt*1.02:.2f} USDT",
            }

        _, ask = quote(s)
        if ask > 0 and amt / ask < 1e-8:
            return {"filled": False, "rejection_reason": "下单金额折算后数量过小，无法成交"}

        est_qty = amt / (ask * (1 + SLIPPAGE_BPS / 10000.0))
        try:
            fill = _execute(c, s, "BUY", est_qty, str(reason).strip())
        except ValueError as exc:
            return {"filled": False, "rejection_reason": str(exc)}

        if stop_loss_pct is not None:
            sl = fill["fill_price"] * (1 - abs(float(stop_loss_pct)) / 100.0)
            c.execute("UPDATE positions SET stop_loss = ? WHERE symbol = ?", (sl, s))
            fill["stop_loss_set"] = round(sl, 8)
        if take_profit_pct is not None:
            tp = fill["fill_price"] * (1 + abs(float(take_profit_pct)) / 100.0)
            c.execute("UPDATE positions SET take_profit = ? WHERE symbol = ?", (tp, s))
            fill["take_profit_set"] = round(tp, 8)

        fill["account_after"] = snapshot(c)
        return fill


@mcp.tool()
def sell(
    symbol: str,
    reason: str,
    quantity: float | None = None,
    pct: float | None = None,
) -> dict:
    """
    市价卖出。给 quantity（币的数量）或 pct（卖出持仓的百分比，100 = 全平）。
    两者都不给则默认全部卖出。reason 必填。
    """
    s = sym(symbol)
    if not str(reason or "").strip():
        return {"filled": False, "rejection_reason": "reason 必填：请写下平仓的逻辑"}
    with db() as c:
        pos = c.execute("SELECT * FROM positions WHERE symbol = ?", (s,)).fetchone()
        if not pos:
            return {"filled": False, "rejection_reason": f"没有 {s} 的持仓，无法卖出"}
        if quantity is not None:
            q = float(quantity)
        elif pct is not None:
            q = float(pos["qty"]) * max(0.0, min(float(pct), 100.0)) / 100.0
        else:
            q = float(pos["qty"])
        if q <= 0:
            return {"filled": False, "rejection_reason": "卖出数量为 0"}
        if q < float(pos["qty"]) - 1e-12:
            pass  # 部分平仓，保留原有的止损止盈
        else:
            q = float(pos["qty"])
            c.execute("UPDATE positions SET stop_loss = NULL, take_profit = NULL WHERE symbol = ?", (s,))
        try:
            fill = _execute(c, s, "SELL", q, str(reason).strip())
        except ValueError as exc:
            return {"filled": False, "rejection_reason": str(exc)}
        fill["account_after"] = snapshot(c)
        return fill


@mcp.tool()
def close_all(reason: str) -> dict:
    """一键平掉所有持仓（不含真实资金，仅模拟盘清仓）。用于回避系统性风险。"""
    results = []
    with db() as c:
        syms_held = [p["symbol"] for p in _positions(c)]
    for s in syms_held:
        results.append(sell(s, reason=f"[close_all] {reason}"))
    return {"closed": len(results), "results": results}


@mcp.tool()
def set_stop_loss(symbol: str, price: float | None = None, pct: float | None = None) -> dict:
    """
    设置/移动止损。给 price（绝对价格）或 pct（相对当前现价的百分比，
    如 5 表示比现价低 5%）。后台巡视线程会在触发时自动平仓。
    """
    s = sym(symbol)
    with db() as c:
        pos = c.execute("SELECT * FROM positions WHERE symbol = ?", (s,)).fetchone()
        if not pos:
            return {"ok": False, "error": f"没有 {s} 的持仓"}
        if price is not None:
            p = float(price)
        elif pct is not None:
            p = mark_price(s) * (1 - abs(float(pct)) / 100.0)
        else:
            return {"ok": False, "error": "必须提供 price 或 pct"}
        c.execute("UPDATE positions SET stop_loss = ? WHERE symbol = ?", (p, s))
        return {"ok": True, "symbol": s, "stop_loss": round(p, 8), "current_price": round(mark_price(s), 8)}


@mcp.tool()
def set_take_profit(symbol: str, price: float | None = None, pct: float | None = None) -> dict:
    """设置/移动止盈。语义同 set_stop_loss。"""
    s = sym(symbol)
    with db() as c:
        pos = c.execute("SELECT * FROM positions WHERE symbol = ?", (s,)).fetchone()
        if not pos:
            return {"ok": False, "error": f"没有 {s} 的持仓"}
        if price is not None:
            p = float(price)
        elif pct is not None:
            p = mark_price(s) * (1 + abs(float(pct)) / 100.0)
        else:
            return {"ok": False, "error": "必须提供 price 或 pct"}
        c.execute("UPDATE positions SET take_profit = ? WHERE symbol = ?", (p, s))
        return {"ok": True, "symbol": s, "take_profit": round(p, 8), "current_price": round(mark_price(s), 8)}


@mcp.tool()
def get_events(since_id: int = 0, limit: int = 50) -> list[dict]:
    """
    后台事件流。特别是 STOP_LOSS / TAKE_PROFIT —— 表示在你两次思考之间，
    某笔持仓已经被后台自动平掉了。醒来后先看这个，别对已平掉的仓位做误判。
    """
    n = max(1, min(int(limit), 500))
    with db() as c:
        rows = c.execute(
            "SELECT * FROM meta WHERE 0"  # placeholder, events kept in trades
        ).fetchall() if False else []
        rows = c.execute(
            "SELECT * FROM trades WHERE id > ? ORDER BY id ASC LIMIT ?", (int(since_id), n)
        ).fetchall()
    return [
        {
            "id": r["id"], "ts": r["ts"], "symbol": r["symbol"], "side": r["side"],
            "qty": round(r["qty"], 8), "price": round(r["price"], 8),
            "realized_pnl": round(r["realized_pnl"], 2), "reason": r["reason"],
        }
        for r in rows
    ]


@mcp.tool()
def snapshot_equity() -> dict:
    """把当前权益打一个采样点进曲线。心跳结束时调用一次，让面板曲线连续。"""
    with db() as c:
        snap = snapshot(c)
        c.execute(
            "INSERT INTO equity (ts, cash, equity) VALUES (?,?,?)",
            (now(), snap["cash"], snap["equity"]),
        )
        return {"ok": True, "ts": now(), "equity": snap["equity"], "cash": snap["cash"]}


@mcp.tool()
def reset_account(confirm: str) -> dict:
    """危险操作：清空全部持仓与成交记录，现金重置。仅当 confirm 恰为 RESET 且
    服务端显式开启了 ALLOW_RESET 时才生效。通常这是给人用的，不是你用的。"""
    if not ALLOW_RESET:
        return {"ok": False, "error": "服务端未开启 ALLOW_RESET，此操作被拒绝"}
    if confirm != "RESET":
        return {"ok": False, "error": "confirm 参数必须恰为 RESET"}
    with db() as c:
        c.execute("DELETE FROM positions")
        c.execute("DELETE FROM trades")
        c.execute("DELETE FROM equity")
        c.execute("DELETE FROM meta")
        c.execute("UPDATE account SET cash = ?, initial_cash = ? WHERE id = 1", (INITIAL_CASH, INITIAL_CASH))
        return {"ok": True, "reset_to": INITIAL_CASH, "ts": now()}


# ==========================================================================
# 后台止损巡视线程
# ==========================================================================
def _stop_watcher() -> None:
    while True:
        try:
            time.sleep(STOP_WATCH_INTERVAL)
            with db() as c:
                for p in _positions(c):
                    s = p["symbol"]
                    try:
                        mark = mark_price(s)
                    except Exception:
                        continue
                    sl, tp = p["stop_loss"], p["take_profit"]
                    if sl is not None and mark <= sl:
                        try:
                            _execute(c, s, "SELL", float(p["qty"]),
                                     f"[AUTO] 触发止损 {sl:.6f}（现价 {mark:.6f}）")
                            c.execute("DELETE FROM positions WHERE symbol = ?", (s,))
                        except Exception:
                            pass
                    elif tp is not None and mark >= tp:
                        try:
                            _execute(c, s, "SELL", float(p["qty"]),
                                     f"[AUTO] 触发止盈 {tp:.6f}（现价 {mark:.6f}）")
                            c.execute("DELETE FROM positions WHERE symbol = ?", (s,))
                        except Exception:
                            pass
        except Exception:
            pass  # 巡视线程永远不能死




# ==========================================================================
# 睡眠配额
#
# ★ 这一节为什么不在 brain 里实现，而放在这个容器里：
#
#   睡眠账本必须放在 Agent 改不到的地方，否则「一天最少睡 6.5 小时」形同虚设。
#   而 brain 容器里没有任何地方是它改不到的 —— 实测 run_shell 只锁了 cwd、
#   没有 chroot，shell 子进程与 brain 同 uid，/data/logs 还是 rw 挂载。
#   换句话说 brain 能写的地方它都能写。
#
#   data/db 是唯一例外：它压根没挂进大脑容器。风控参数在这儿，睡眠配额也在这儿。
#   这不是保密，是物理隔离 —— 它连文件都看不到。
#
#   顺带也是成本闸门：一天最多能跑多少轮心跳，由这几个数封顶。
# ==========================================================================

def _meta_get(c: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _sleep_rows(c: sqlite3.Connection) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM sleep_log ORDER BY id").fetchall()


def _sleep_snapshot(c: sqlite3.Connection, now_ts: float | None = None) -> dict:
    """把睡眠账本算成一份快照。纯函数式读取，不改任何状态。"""
    now_ts = now_ts if now_ts is not None else time.time()
    raw = _meta_get(c, "sleep_first_seen")
    first_seen = float(raw) if raw else now_ts

    rows = _sleep_rows(c)
    open_row = None
    for r in rows:
        if r["ended_at"] is None:
            open_row = r            # 理论上只会有一条
    sleeping = open_row is not None

    # 需求按「首见以来的时长」折算：刚部署时不该立刻欠 6.5 小时，
    # 跑满 24 小时之后才要求整额。
    elapsed = min(SLEEP_WINDOW_HOURS * 3600.0, max(0.0, now_ts - first_seen))
    required = SLEEP_MIN_HOURS_PER_DAY * (elapsed / (SLEEP_WINDOW_HOURS * 3600.0))

    # 已睡：滚动 24 小时窗口内的累计睡眠（含正在进行的那段）
    window_start = now_ts - SLEEP_WINDOW_HOURS * 3600.0
    slept = 0.0
    for r in rows:
        s = max(r["started_at"], window_start)
        e = min(r["ended_at"] if r["ended_at"] is not None else now_ts, now_ts)
        if e > s:
            slept += (e - s) / 3600.0

    debt = round(max(0.0, required - slept), 2)

    if sleeping:
        awake_span = 0.0
        sleeping_since = open_row["started_at"]
        sleep_hours = round((now_ts - open_row["started_at"]) / 3600.0, 2)
    else:
        ends = [r["ended_at"] for r in rows if r["ended_at"] is not None]
        last_end = max(ends) if ends else first_seen
        awake_span = round((now_ts - last_end) / 3600.0, 2)
        sleeping_since = None
        sleep_hours = 0.0

    return {
        "sleeping": sleeping,
        "sleeping_since": (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(sleeping_since))
            if sleeping_since else None
        ),
        "sleep_hours_this_nap": sleep_hours,
        "slept_last_24h_hours": round(slept, 2),
        "required_hours": round(required, 2),
        "debt_hours": debt,
        "awake_span_hours": awake_span,
        "min_hours_per_day": SLEEP_MIN_HOURS_PER_DAY,
        "max_single_hours": SLEEP_MAX_SINGLE_HOURS,
        "max_awake_hours": SLEEP_MAX_AWAKE_HOURS,
        "must_sleep_now": (not sleeping) and awake_span >= SLEEP_MAX_AWAKE_HOURS and debt > 0,
        "must_wake_now": sleeping and sleep_hours >= SLEEP_MAX_SINGLE_HOURS,
    }


@mcp.tool()
def sleep_state() -> dict:
    """
    查看你的睡眠账本：现在是不是在睡、今天还欠多少睡眠、已经连续清醒多久。

    配额是硬性的（一天最少 6.5 小时、单次最长 11 小时、连续清醒 17.5 小时必须去睡），
    由系统强制执行，你无法修改。每次心跳开始时看一眼，心里有数。
    """
    with db() as c:
        return _sleep_snapshot(c)


@mcp.tool()
def sleep_start(reason: str) -> dict:
    """
    进入睡眠。

    睡眠期间定时唤醒会被系统静默，只有你自己设的价格阈值、或人类设的阈值
    能把你叫醒 —— 所以睡前务必确认该设的止损都设好了。
    """
    with db() as c:
        # 幂等。先查后插有个 TOCTOU 窗口，并发的两次调用会插出两行
        # ended_at IS NULL —— _sleep_snapshot 只认最后一条，所以账算得对，
        # 但账本会脏，而这是"权威账本"，脏一次就得手工修。
        # BEGIN IMMEDIATE 先拿写锁：第二次调用会在锁上排队，等它进来时
        # 第一行已经在了，于是走"已经在睡"那条分支。
        c.execute("BEGIN IMMEDIATE")
        snap = _sleep_snapshot(c)
        if snap["sleeping"]:
            return {"ok": False, "message": "你已经在睡眠中，不需要再睡一次。", **snap}
        c.execute(
            "INSERT INTO sleep_log (started_at, reason) VALUES (?, ?)",
            (time.time(), str(reason or "")[:500]),
        )
        c.commit()
        snap = _sleep_snapshot(c)
        return {"ok": True, "message": "已进入睡眠。", **snap}


@mcp.tool()
def sleep_end(reason: str) -> dict:
    """
    主动结束睡眠（自然醒）。睡眠期间被价格叫醒不需要调这个 —— 处理完那一轮
    你会自动回到睡眠状态，直到自己认为该醒为止。
    """
    with db() as c:
        # 同上，先拿写锁再查，免得并发的两次 sleep_end 抢同一行。
        c.execute("BEGIN IMMEDIATE")
        snap = _sleep_snapshot(c)
        if not snap["sleeping"]:
            return {"ok": False, "message": "你现在不在睡眠状态。", **snap}
        row = c.execute(
            "SELECT * FROM sleep_log WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        ended = time.time()
        c.execute(
            "UPDATE sleep_log SET ended_at = ?, end_reason = ? WHERE id = ?",
            (ended, str(reason or "")[:500], row["id"]),
        )
        c.commit()
        napped = round((ended - row["started_at"]) / 3600.0, 2)
        snap = _sleep_snapshot(c)
        note = None
        if snap["debt_hours"] > 0:
            note = (
                f"这一觉睡了 {napped} 小时，但滚动 24 小时内还欠 "
                f"{snap['debt_hours']} 小时——欠着的时候连续清醒超过 "
                f"{SLEEP_MAX_AWAKE_HOURS} 小时会被强制送回睡眠。"
            )
        return {"ok": True, "slept_hours": napped, "message": "已醒来。", "note": note, **snap}


if __name__ == "__main__":
    init_db()
    threading.Thread(target=_stop_watcher, name="stop-watcher", daemon=True).start()
    mcp.run(transport="streamable-http")
