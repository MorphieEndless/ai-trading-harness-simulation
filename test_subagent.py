"""子代理功能测试：真派一个批量筛选任务，看它能否自己取数、算出结论、压缩回传。

用法（在 trader 网络里跑，SUBAGENT_* 由 --env-file 注入）：
    docker run --rm --network ai-trader_trader --env-file /tmp/subtest.env \
        -v /opt/ai-trader/test_subagent.py:/t.py:ro \
        ai-trader/brain:latest python /t.py
"""
import asyncio
import json
import sys

from trader.config import cfg
from trader.events import EventBus
from trader.mcp_hub import MCPHub, ServerCfg
from trader.subagent import SubAgent
from trader.tools_local import LocalTools


async def main() -> int:
    bus = EventBus("/tmp/sub_events.jsonl")
    hub = MCPHub([
        ServerCfg("market", cfg.market_market if False else cfg.market_mcp_url),
        ServerCfg("paper", cfg.paper_mcp_url),
    ])
    await hub.start()

    local = LocalTools(cfg)
    sub = SubAgent(cfg, hub, local, bus)

    print("=" * 70)
    print("子代理启用状态 :", sub.enabled)
    print("子代理模型     :", cfg.subagent_model)
    print("子代理 base_url:", cfg.subagent_base_url)
    print("最大步数       :", cfg.subagent_max_steps)
    print("结论长度上限   :", cfg.subagent_max_result_chars)
    granted = [t["function"]["name"] for t in sub._subagent_tools()]
    print(f"授予子代理的工具（{len(granted)} 个）: {', '.join(granted) or '(无)'}")

    # 越权测试：确认它拿不到有副作用的工具
    forbidden = [n for n in ("paper__buy", "paper__sell", "fs_write", "run_shell", "delegate")
                 if n in granted]
    print("越权工具泄露   :", forbidden or "无 ✓")

    print("=" * 70)
    print("派发任务：批量筛选 6 个币的量价异动")
    print("=" * 70)

    result = await sub.call({
        "task": (
            "考察以下 6 个交易对在 **4 小时**周期上的量价异动："
            "BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, DOGEUSDT, XRPUSDT。\n"
            "需要用到两个判定指标（都在 get_technical_snapshot 的返回里）：\n"
            "· volume.recent_5_vs_20_ratio —— 近 5 根相对 20 根均量的倍数\n"
            "· range_position.where_in_range_pct —— 价格在近期区间中的位置百分比"
        ),
        "want": (
            "按「量能异动强度」排序的前 3 名。每个一行，格式：\n"
            "代码 | 当前价 | 量能倍数 | 区间位置% | 一句话理由\n"
            "总长 300 字以内。"
        ),
    })

    print("\n" + "=" * 70)
    print("子代理回传给主 Agent 的内容")
    print("=" * 70)
    print(result)
    print("\n" + "=" * 70)
    print("统计:", json.dumps(sub.status(), ensure_ascii=False))
    print(f"回传长度: {len(result)} 字符（上限 {cfg.subagent_max_result_chars}）")

    # 把子代理内部看到的东西打出来，证明它真的自己取了数据
    print("\n--- 子代理内部的活动记录 ---")
    try:
        with open("/tmp/sub_events.jsonl", encoding="utf-8") as f:
            for line in f:
                e = json.loads(line)
                d = e.get("data", {})
                if e["kind"] == "subagent_start":
                    print(f"  start: {str(d.get('task'))[:100]}")
                elif e["kind"] == "subagent_tool":
                    print(f"  tool : {d.get('tool')} {str(d.get('args'))[:90]}")
                elif e["kind"] == "subagent_end":
                    print(f"  end  : ok={d.get('ok')} {d.get('duration')}s")
    except OSError:
        pass

    await hub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
