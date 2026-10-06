"""验证运行时成本提醒（cost_nudge）的触发口径。

这个测试锁住一个踩过的坑：提醒曾经按「工具调用总数」计数，于是读笔记、
写笔记、打权益点这些躲不掉的琐务把阈值撑满，提醒每轮空响 ——
Agent 两轮就学会了无视它，还会把它误读成"工具调用配额快满了"。
现在的口径是：只看「取数次数」（market__* / exa__*）。

不需要网络与账本，构造一个空壳 Agent 直接调 _cost_nudge()。
"""
import json
import os
from collections import Counter

os.environ.setdefault("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT")
os.environ.setdefault("SUBAGENT_MODELS_FILE", "/nonexistent")   # 别真去读白名单

from trader.agent import Agent                                    # noqa: E402
from trader.config import cfg                                     # noqa: E402
from trader.events import EventBus                                # noqa: E402

WATCH = list(cfg.watchlist)
FAILS: list[str] = []


class FakeSub:
    def __init__(self, enabled=True):
        self.enabled = enabled


def fresh(enabled=True):
    a = Agent.__new__(Agent)          # 绕过 __init__：这个测试只碰提醒逻辑
    a.cfg = cfg
    a.subagent = FakeSub(enabled)
    a.bus = EventBus("/tmp/nudge_events.jsonl")
    a._beat_delegates = 0
    a._nudged = False
    a._beat_fetch_calls = 0
    a._beat_fetch_tools = Counter()
    a._beat_fetch_symbols = set()
    return a


def batch(*names):
    return [{"id": str(i), "name": n, "args": {}} for i, n in enumerate(names)]


def check(label, ok, extra=""):
    print(f"  {'✓' if ok else '✗'} {label}" + (f"   {extra}" if extra else ""))
    if not ok:
        FAILS.append(label)


TICK = 3   # 与 cfg.cost_nudge_after 默认值一致


def case_mundane_calls_never_nudge():
    """核心回归：9 次调用但全都是琐务（取数 0 次）→ 不许出声。"""
    a = fresh()
    a._beat_tool_calls = 9                        # 老口径下这里早就过阈值了
    text = a._cost_nudge(batch("fs_read", "fs_list", "paper__snapshot_equity"), step=3)
    check("纯琐务调用（9 次）不触发提醒", text == "", f"got={text[:60]!r}")


def case_below_threshold():
    a = fresh()
    a._beat_fetch_calls = TICK - 1
    a._beat_fetch_tools["market__get_price"] = TICK - 1
    text = a._cost_nudge(batch("market__get_price"), step=2)
    check(f"取数 {TICK - 1} 次（< {TICK}）不触发", text == "")


def case_fires_and_names_missing():
    a = fresh()
    a._beat_fetch_calls = 4
    a._beat_fetch_tools["market__get_technical_snapshot"] = 4
    a._beat_fetch_symbols = {WATCH[0]}
    text = a._cost_nudge(batch("market__get_technical_snapshot", "fs_read"), step=2)
    check("取数 4 次触发提醒", bool(text))
    check("提醒里带上了实际取数次数", "4" in text)
    check("提醒里带上了取数工具名", "market__get_technical_snapshot" in text)
    missing = [s for s in WATCH[1:]]
    check("点名了还没看过的标的", all(s in text for s in missing), f"missing={missing}")
    check("明确给出 delegate 出路", "delegate" in text)
    # 事件里要带上正文，否则事后无法复盘"到底说了什么"
    evs = [e for e in a.bus.history(50) if e["kind"] == "cost_nudge"]
    check("cost_nudge 事件带 text 字段", bool(evs) and evs[-1]["data"].get("text"))
    check("cost_nudge 事件带 missing 字段", bool(evs) and evs[-1]["data"].get("missing") == missing)


def case_all_covered():
    a = fresh()
    a._beat_fetch_calls = TICK
    a._beat_fetch_tools["market__get_market_overview"] = TICK
    a._beat_fetch_symbols = set(WATCH)
    text = a._cost_nudge(batch("market__get_market_overview"), step=2)
    check("全覆盖时提醒不再点名标的", bool(text) and "还没看" not in text)


def case_only_once_per_beat():
    a = fresh()
    a._beat_fetch_calls = 5
    a._beat_fetch_tools["market__get_price"] = 5
    a._cost_nudge(batch("market__get_price"), step=2)
    again = a._cost_nudge(batch("market__get_price"), step=3)
    check("同一轮只提醒一次", again == "")


def case_silent_when_delegated_or_off():
    a = fresh()
    a._beat_fetch_calls = 6
    a._beat_fetch_tools["market__get_price"] = 6
    a._beat_delegates = 1
    check("已经派过子代理 → 不提醒", a._cost_nudge(batch("market__get_price"), step=2) == "")

    b = fresh(enabled=False)
    b._beat_fetch_calls = 6
    check("子代理未配置 → 不提醒", b._cost_nudge(batch("market__get_price"), step=2) == "")


def case_no_fetch_in_this_batch():
    """阈值是在上一批跨过的，本批全是琐务 → 提醒要留到下一批取数时再贴。"""
    a = fresh()
    a._beat_fetch_calls = TICK
    a._beat_fetch_tools["market__get_price"] = TICK
    check("本批没有取数调用 → 不贴", a._cost_nudge(batch("fs_append", "paper__snapshot_equity"), step=3) == "")
    check("（提醒没有被消耗掉）仍可在下一批取数时发出",
          bool(a._cost_nudge(batch("market__get_price"), step=4)))


def case_last_step():
    a = fresh()
    a._beat_fetch_calls = 9
    a._beat_fetch_tools["market__get_price"] = 9
    check("已到最大步数 → 不提醒（没机会再改了）",
          a._cost_nudge(batch("market__get_price"), step=cfg.llm_max_steps) == "")


def main() -> int:
    print(f"关注列表 {WATCH}   cost_nudge_after={cfg.cost_nudge_after}")
    for fn in (case_mundane_calls_never_nudge, case_below_threshold, case_fires_and_names_missing,
               case_all_covered, case_only_once_per_beat, case_silent_when_delegated_or_off,
               case_no_fetch_in_this_batch, case_last_step):
        print(f"\n[{fn.__name__}]")
        fn()
    print()
    if FAILS:
        print("未通过：" + "；".join(FAILS))
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
