"""并发子代理 + 模型选择 + 权限边界的综合测试。"""
import asyncio
import json
import time

from trader.config import cfg
from trader.events import EventBus
from trader.mcp_hub import MCPHub, ServerCfg
from trader.subagent import SubAgent
from trader.tools_local import LocalTools

WATCH = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "XRPUSDT",
         "ADAUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT"]


async def main() -> int:
    bus = EventBus("/tmp/sub_events.jsonl")
    hub = MCPHub([ServerCfg("market", cfg.market_mcp_url),
                  ServerCfg("paper", cfg.paper_mcp_url)])
    await hub.start()
    sub = SubAgent(cfg, hub, LocalTools(cfg), bus)

    models = sub.refresh_models()
    print("=" * 74)
    print(f"白名单模型 {len(models)} 个，默认 {sub.status()['default_model']}")
    for m in models:
        print(f"  · {m.id:<22} {m.label}")
        print(f"      适合  : {m.use_for[:70]}")
        if m.avoid_for and m.avoid_for not in ("无", "-"):
            print(f"      不适合: {m.avoid_for[:70]}")
    print(f"并发上限 {cfg.subagent_max_concurrency}   单轮上限 {cfg.subagent_max_per_beat}")

    granted = [t["function"]["name"] for t in sub._subagent_tools()]
    print(f"\n授予子代理 {len(granted)} 个工具")
    leaked = [n for n in ("paper__buy", "paper__sell", "fs_write", "run_shell", "delegate")
              if n in granted]
    print(f"越权泄露: {leaked or '无 ✓'}")

    # ---- 越权模型名应被拒绝 ----
    print("\n" + "=" * 74)
    print("测试 A：主 Agent 指定白名单外的模型 → 应被拒绝")
    r = await sub.call({"task": "随便看看 BTC", "want": "一句话", "model": "gpt-9-ultra"})
    print(f"  {r[:200]}")
    ok_a = r.startswith("[被拒绝]")

    # ---- 并发三个任务 ----
    print("\n" + "=" * 74)
    print("测试 B：并发派发 3 个任务（应同时执行，总耗时约等于最慢那个）")
    sub.reset_beat()
    jobs = [
        ("deepseek-v4.1-flash",
         f"考察 {'、'.join(WATCH[:5])} 这 5 个交易对在 4 小时周期的量能异动。",
         "按量能倍数（volume.recent_5_vs_20_ratio）降序给出前 3 名，"
         "每行：代码 | 现价 | 倍数 | 区间位置% 。250 字内。"),
        ("glm-5.3-fast",
         f"考察 {'、'.join(WATCH[5:])} 这 5 个交易对在 1 小时周期的动量状态。",
         "挑出 RSI 最极端（最超买和最超卖）的各 1 个，给出代码 | RSI 值 | 24h涨跌。200 字内。"),
        ("qwen3.8-27b",
         "读取全局市场情绪：恐慌贪婪指数，以及 BTC/ETH/SOL/BNB 的 24 小时涨跌幅。",
         "用两句话概括当前情绪面，带上具体数字。150 字内。"),
    ]
    t0 = time.time()
    results = await asyncio.gather(*(sub.call({"task": t, "want": w, "model": m})
                                     for m, t, w in jobs))
    elapsed = time.time() - t0

    for (m, _t, _w), res in zip(jobs, results):
        print(f"\n--- [{m}] ---")
        print(res[:700])

    s = sub.status()
    print("\n" + "=" * 74)
    print(f"总耗时 {elapsed:.1f}s（串行本应更久）")
    print(f"调用 {s['calls']} 次，成功 {s['calls'] - s['errors']}，失败 {s['errors']}")
    print(f"token 总计 {s['tokens_used']}")
    for mid, tk in (s.get("tokens_by_model") or {}).items():
        print(f"    {mid:<22} {tk}")
    print(f"本轮配额使用 {s['per_beat_used']}/{s['max_per_beat']}")

    # ---- 配额耗尽 ----
    print("\n" + "=" * 74)
    print("测试 C：把本轮的配额用光，再派一次 → 应被拒绝")
    for _ in range(max(0, cfg.subagent_max_per_beat - sub._beat_calls)):
        sub._beat_calls += 1
    r = await sub.call({"task": "再看一眼", "want": "一句话"})
    print(f"  {r[:180]}")
    ok_c = r.startswith("[配额用尽]")

    print("\n" + "=" * 74)
    print(f"测试 A（越权模型被拒）: {'通过 ✓' if ok_a else '失败 ✗'}")
    print(f"测试 C（配额护栏）    : {'通过 ✓' if ok_c else '失败 ✗'}")
    await hub.stop()
    return 0 if (ok_a and ok_c) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
