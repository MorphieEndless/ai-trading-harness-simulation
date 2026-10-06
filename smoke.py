"""端到端冒烟测试：验证行情取价、模拟盘成交、以及风控闸门是否真的会拦。

在 trader 网络里跑，直接对容器发 MCP 请求。
"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MARKET = "http://mcp-market:8081/mcp"
PAPER = "http://mcp-paper:8082/mcp"


async def call(url: str, tool: str, args: dict | None = None) -> str:
    async with streamablehttp_client(url) as res:
        read, write = res[0], res[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            out = await session.call_tool(tool, arguments=args or {})
            return "\n".join(getattr(b, "text", "") for b in out.content)


def show(label: str, raw: str, keys: list[str] | None = None) -> dict | list | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  {label}: {raw[:300]}")
        return None
    if isinstance(data, dict) and keys:
        brief = {k: data.get(k) for k in keys if k in data}
        print(f"  {label}: {json.dumps(brief, ensure_ascii=False)}")
    else:
        print(f"  {label}: {json.dumps(data, ensure_ascii=False)[:400]}")
    return data


async def main() -> int:
    fails: list[str] = []
    print("\n【1】行情只读工具")
    p = await call(MARKET, "get_price", {"symbol": "BTCUSDT"})
    price = show("get_price", p, ["symbol", "price"])
    if not price or not price.get("price"):
        fails.append("取价失败")

    ov = await call(MARKET, "get_market_overview", {"symbols": ["BTCUSDT", "ETHUSDT"]})
    show("get_market_overview", ov, ["count", "strongest_24h"])

    ts = await call(MARKET, "get_technical_snapshot", {"symbol": "BTCUSDT", "interval": "1h"})
    snap = show("get_technical_snapshot", ts, ["last_close", "trend", "momentum"])
    if not snap:
        fails.append("技术快照失败")

    print("\n【2】模拟盘初始状态")
    limits = await call(PAPER, "get_risk_limits")
    show("get_risk_limits", limits, ["max_open_positions", "max_position_pct_of_equity", "min_order_notional"])
    acc = await call(PAPER, "get_account")
    a = show("get_account", acc, ["cash", "equity", "open_positions"])
    if not a or abs(a.get("equity", 0) - 10000) > 0.01:
        fails.append(f"初始权益不是 10000（实为 {a.get('equity') if a else '?'}）")

    print("\n【3】正常买入（应成功）")
    buy = await call(PAPER, "buy", {
        "symbol": "BTCUSDT", "quote_amount": 500,
        "reason": "冒烟测试：验证成交链路", "stop_loss_pct": 3,
    })
    b = show("buy", buy, ["filled", "qty", "fill_price", "notional", "fee", "stop_loss_set", "rejection_reason"])
    if not b or not b.get("filled"):
        fails.append(f"正常买入被拒：{b}")

    print("\n【4】风控闸门（以下三项都应该被拒绝）")
    big = await call(PAPER, "buy", {"symbol": "ETHUSDT", "quote_amount": 99999, "reason": "测试仓位上限"})
    r1 = show("超仓位上限", big, ["filled", "rejection_reason"])
    if r1 and r1.get("filled"):
        fails.append("仓位上限未生效！")

    tiny = await call(PAPER, "buy", {"symbol": "SOLUSDT", "quote_amount": 1, "reason": "测试最小额"})
    r2 = show("低于最小下单额", tiny, ["filled", "rejection_reason"])
    if r2 and r2.get("filled"):
        fails.append("最小下单额未生效！")

    noreason = await call(PAPER, "buy", {"symbol": "BNBUSDT", "quote_amount": 50, "reason": ""})
    r3 = show("空 reason", noreason, ["filled", "rejection_reason"])
    if r3 and r3.get("filled"):
        fails.append("reason 校验未生效！")

    print("\n【5】持仓与盈亏")
    acc2 = await call(PAPER, "get_account")
    show("get_account", acc2, ["cash", "market_value", "equity", "unrealized_pnl", "open_positions"])

    print("\n【6】全部卖出（应成功，且有少量成本损耗）")
    sell = await call(PAPER, "sell", {"symbol": "BTCUSDT", "pct": 100, "reason": "冒烟测试：平仓"})
    s = show("sell", sell, ["filled", "qty", "fill_price", "realized_pnl", "rejection_reason"])
    if not s or not s.get("filled"):
        fails.append(f"卖出失败：{s}")
    elif s.get("realized_pnl", 0) > 0.01:
        fails.append(f"往返成本应为负但算出正收益：{s.get('realized_pnl')}")

    print("\n【7】事件流与权益快照")
    await call(PAPER, "snapshot_equity")
    ev = await call(PAPER, "get_events", {"since_id": 0, "limit": 10})
    show("get_events", ev)
    curve = await call(PAPER, "get_equity_curve", {"limit": 10})
    show("get_equity_curve", curve)

    print("\n【8】绩效统计")
    perf = await call(PAPER, "get_performance")
    show("get_performance", perf, ["closed_trades", "win_rate_pct", "max_drawdown_pct"])

    print("\n【9】超范围卖出（应被拒）")
    over = await call(PAPER, "sell", {"symbol": "BTCUSDT", "quantity": 999, "reason": "测试超卖"})
    r4 = show("卖出不存在的持仓", over, ["filled", "rejection_reason"])
    if r4 and r4.get("filled"):
        fails.append("超卖未拦截！")

    print("\n" + "=" * 60)
    if fails:
        print("测试未通过：")
        for f in fails:
            print("  ✗", f)
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
