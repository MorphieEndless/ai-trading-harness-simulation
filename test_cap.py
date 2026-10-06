"""验证并发上限：一次发 5 个 delegate，看同时在跑的最多几个。"""
import asyncio
import time

from trader.config import cfg
from trader.events import EventBus
from trader.mcp_hub import MCPHub, ServerCfg
from trader.subagent import SubAgent
from trader.tools_local import LocalTools

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT"]


async def main():
    bus = EventBus("/tmp/cap_events.jsonl")
    hub = MCPHub([ServerCfg("market", cfg.market_mcp_url),
                  ServerCfg("paper", cfg.paper_mcp_url)])
    await hub.start()
    sub = SubAgent(cfg, hub, LocalTools(cfg), bus)
    sub.refresh_models()
    sub.reset_beat()
    sub._beat_calls = 0

    print(f"配置的并发上限 = {cfg.subagent_max_concurrency}")
    print(f"配置的单轮上限 = {cfg.subagent_max_per_beat}")
    print(f"一次发 {len(PAIRS)} 个 delegate 调用（超过并发上限，看会不会排队）\n")

    peaks = []

    async def watch():
        for _ in range(200):
            peaks.append(sub.stats.active)
            await asyncio.sleep(0.1)

    t0 = time.time()
    watcher = asyncio.create_task(watch())
    jobs = [sub.call({"task": f"看一眼 {p} 的 1h 技术面，只要 RSI 和 ATR",
                      "want": f"{p}: RSI / ATR 一行",
                      "model": "glm-5.3-fast"}) for p in PAIRS]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    watcher.cancel()
    elapsed = time.time() - t0

    ok = sum(1 for r in results if isinstance(r, str) and not r.startswith("[子代理失败"))
    peak = max(peaks) if peaks else 0

    print("=" * 62)
    print(f"耗时 {elapsed:.1f}s   成功 {ok}/{len(PAIRS)}")
    print(f"观测到的最大并发 = {peak}（配置上限 {cfg.subagent_max_concurrency}）")
    print(f"本轮配额使用 = {sub._beat_calls}/{cfg.subagent_max_per_beat}")
    s = sub.status()
    print(f"token 总计 {s['tokens_used']}")
    for k, v in (s.get("tokens_by_model") or {}).items():
        print(f"   {k:<24} {v}")
    print(f"失败 {s['errors']} 次")

    print("\n各任务结果：")
    for p, r in zip(PAIRS, results):
        txt = str(r).replace("\n", " ")[:120]
        print(f"  {p:<10} {txt}")

    print()
    if peak <= cfg.subagent_max_concurrency:
        print(f"✓ 并发上限守住了（峰值 {peak} ≤ {cfg.subagent_max_concurrency}）")
        rc = 0
    else:
        print(f"✗ 并发超限！峰值 {peak}")
        rc = 1
    await hub.stop()
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
