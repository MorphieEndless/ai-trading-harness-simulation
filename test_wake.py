#!/usr/bin/env python3
"""唤醒策略与睡眠的纯逻辑测试。不联网、不花钱。

守的是这几条口径（都是设计时明确讨论过的，不是随手加的）：

  1. 兜底的语义是「最长沉默」，不是固定节拍 —— 中间发生过别的唤醒，
     那次兜底就该被跳过。这是那套"跳过"规则的正确形态。
  2. 人类的值是上界，Agent 只能往短里改。否则"兜底"可以被它单方面作废。
  3. 睡眠期间定时唤醒必须静默 —— 省 token 全靠这一条。
  4. 睡眠中只有自唤醒和（开关允许的）人类阈值能把它叫起来。
  5. 被叫醒不影响睡眠债：人可以被吵醒两次，但一天照样得睡够。
  6. 强制入睡 / 强制醒来两条硬闸必须真的会响，否则配额形同虚设。
  7. 每小时上限 —— 防"行情疯起来就疯狂醒"，这条不响的话成本会比固定心跳更高。

跑法：bash runtests.sh wake
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def _find_wake() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "brain" / "trader" / "wake.py",
                 here / "trader" / "wake.py",
                 pathlib.Path("/t/trader/wake.py")):
        if cand.is_file():
            return cand
    sys.exit("找不到 wake.py")


spec = importlib.util.spec_from_file_location("wake_under_test", _find_wake())
wake_mod = importlib.util.module_from_spec(spec)
# 必须先注册进 sys.modules：dataclass 内部会去 sys.modules 里按 __module__ 找类，
# 找不到就炸（'NoneType' object has no attribute '__dict__'）。
sys.modules["wake_under_test"] = wake_mod
spec.loader.exec_module(wake_mod)          # wake.py 没有相对 import，可以直接加载


# ---------------------------------------------------------------- 替身
class FakeCfg:
    watchlist = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    workspace_dir = "/tmp"


class FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, kind, **data):
        self.events.append({"kind": kind, **data})


class FakeHub:
    """睡眠账本在 mcp-paper，所以这里模拟它的返回。"""

    def __init__(self, sleep=None):
        self.calls = []
        self.prices: dict[str, float] = {}
        self.sleep = sleep or {
            "sleeping": False, "debt_hours": 0.0, "awake_span_hours": 1.0,
            "slept_last_24h_hours": 0.0, "required_hours": 0.0,
            "min_hours_per_day": 6.5, "max_single_hours": 11,
            "max_awake_hours": 17.5, "sleep_hours_this_nap": 0.0,
            "must_sleep_now": False, "must_wake_now": False,
        }

    async def call(self, name, args=None):
        self.calls.append((name, args or {}))
        if name == "paper__sleep_state":
            return json.dumps(self.sleep)
        if name == "market__get_prices":
            return json.dumps([{"symbol": k, "price": v} for k, v in self.prices.items()])
        if name == "paper__sleep_start":
            self.sleep = dict(self.sleep, sleeping=True, ok=True,
                              sleeping_since="2026-09-27 02:00",
                              sleep_hours_this_nap=0.0)
            return json.dumps(self.sleep)
        if name == "paper__sleep_end":
            self.sleep = dict(self.sleep, sleeping=False, ok=True, slept_hours=7.0,
                              debt_hours=0.0, slept_last_24h_hours=7.0)
            return json.dumps(self.sleep)
        if name == "paper__get_positions":
            return json.dumps([])
        return json.dumps([])


def make_ctl(td: str, hub=None, booted: bool = True):
    bus = FakeBus()
    ctl = wake_mod.WakeController(FakeCfg(), hub or FakeHub(), bus, asyncio.Event())
    # 覆盖两个落点，避免碰真实的 /data 与工作区
    ctl.human = wake_mod.JsonPolicy(os.path.join(td, "human.json"))
    ctl.agent = wake_mod.JsonPolicy(os.path.join(td, "agent.json"))
    # 默认当作「已经启动过」—— 否则每一段的第一个 decide() 都会命中
    # "启动先醒一次"，把真正要考的那条口径盖掉。第 5b 段显式传 booted=False。
    ctl._boot_done = booted
    return ctl, bus


def awake(**over):
    d = {"sleeping": False, "debt_hours": 1.0, "awake_span_hours": 2.0,
         "slept_last_24h_hours": 3.0, "required_hours": 4.0,
         "min_hours_per_day": 6.5, "max_single_hours": 11,
         "max_awake_hours": 17.5, "sleep_hours_this_nap": 0.0,
         "must_sleep_now": False, "must_wake_now": False}
    d.update(over)
    return d


def asleep(**over):
    d = {"sleeping": True, "sleeping_since": "2026-09-27 02:00",
         "debt_hours": 3.0, "awake_span_hours": 0.0,
         "slept_last_24h_hours": 1.0, "required_hours": 4.0,
         "min_hours_per_day": 6.5, "max_single_hours": 11,
         "max_awake_hours": 17.5, "sleep_hours_this_nap": 1.0,
         "must_sleep_now": False, "must_wake_now": False}
    d.update(over)
    return d


T0 = 1_000_000.0


def main() -> int:
    print("=" * 68)
    print("唤醒策略与睡眠 · 纯逻辑测试")
    print("=" * 68)

    with tempfile.TemporaryDirectory() as td:
        # ==================================================== 1 兜底语义
        print("\n[1] 兜底的语义是「最长沉默」，不是固定节拍")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 3600, "price_trigger_enabled": False})
        ctl._sleep = awake()
        ctl.last_beat_at = T0
        d = ctl.decide(T0 + 3599)
        check("距上次唤醒 3599s（< 3600）→ 不醒", not d.run, f"实际 {d.run} {d.reason}")
        d = ctl.decide(T0 + 3601)
        check("距上次唤醒 3601s（> 3600）→ 兜底唤醒",
              d.run and d.trigger == "schedule", f"实际 {d.trigger}")

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 3600, "price_trigger_enabled": False})
        ctl._sleep = awake()
        ctl.last_beat_at = T0
        ctl.last_beat_at = T0 + 32 * 60          # T+32min 发生过一次自唤醒
        d = ctl.decide(T0 + 60 * 60)
        check("T+32min 自唤醒过之后，T+60min 那次兜底被跳过",
              not d.run, f"实际 run={d.run} {d.reason}")
        d = ctl.decide(T0 + 32 * 60 + 3601)
        check("T+92min（距上次唤醒满 1 小时）→ 兜底重新生效",
              d.run and d.trigger == "schedule", f"实际 {d.trigger}")

        # ==================================================== 2 钳位
        print("\n[2] 人类的值是上界，Agent 只能往短里改")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400})
        check("人类 90 分钟", ctl.effective()["interval_seconds"] == 5400)
        ctl.agent.write({"interval_seconds": 30 * 60})
        check("Agent 改成 30 分钟 → 生效",
              ctl.effective()["interval_seconds"] == 1800)
        ctl.agent.write({"interval_seconds": 6 * 3600})
        check("Agent 想改成 6 小时 → 被钳到人类上界 90 分钟",
              ctl.effective()["interval_seconds"] == 5400,
              f"实际 {ctl.effective()['interval_seconds']}")
        ctl.agent.write({"interval_seconds": 10})
        check(f"Agent 想改成 10 秒 → 被抬到下限 {wake_mod.MIN_INTERVAL}s",
              ctl.effective()["interval_seconds"] == wake_mod.MIN_INTERVAL)

        # ==================================================== 3 睡眠静默
        print("\n[3] 睡眠期间定时唤醒必须静默（省 token 全靠这条）")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 600, "price_trigger_enabled": True,
                         "trigger_during_sleep": True})
        ctl._sleep = asleep()
        ctl.last_beat_at = T0
        d = ctl.decide(T0 + 99999)
        check("睡着了 + 早过了兜底间隔 → 依然不醒",
              not d.run and d.sleep_quiet, f"实际 run={d.run} {d.reason}")

        # ==================================================== 4 睡眠中的叫醒
        print("\n[4] 睡眠中谁能把它叫起来")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 600, "price_trigger_enabled": True,
                         "trigger_during_sleep": True})
        ctl._sleep = asleep()
        ctl.request_wake("price_agent", "SOLUSDT 波动 3%")
        d = ctl.decide(T0)
        check("自唤醒能穿透睡眠", d.run and d.trigger == "price_agent",
              f"实际 {d.trigger}")

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 600, "price_trigger_enabled": True,
                         "trigger_during_sleep": False})
        ctl._sleep = asleep()
        ctl.request_wake("price_human", "BTCUSDT 波动 1%")
        d = ctl.decide(T0)
        check("人类阈值 + 睡眠中不打扰=关 → 不叫醒", not d.run, f"实际 run={d.run}")

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 600, "price_trigger_enabled": True,
                         "trigger_during_sleep": True})
        ctl._sleep = asleep()
        ctl.request_wake("price_human", "BTCUSDT 波动 1%")
        d = ctl.decide(T0)
        check("人类阈值 + 允许叫醒 → 叫醒", d.run and d.trigger == "price_human")

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 600})
        ctl._sleep = asleep()
        ctl.request_wake("human_manual", "人类点了立即唤醒")
        d = ctl.decide(T0)
        check("人类手动叫醒 → 睡眠中也能穿透",
              d.run and d.trigger == "human_manual")

        # ==================================================== 5 硬闸
        print("\n[5] 两条硬闸（配额靠它们才成立）")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 600})
        ctl._sleep = awake(must_sleep_now=True, awake_span_hours=17.6, debt_hours=2.0)
        d = ctl.decide(T0)
        check("连续清醒 17.6h 且欠着睡眠 → 强制入睡",
              d.force_sleep and not d.run, f"实际 {d}")
        check("强制入睡那一步 run=False —— 它是省钱的不是花钱的", not d.run)

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 600})
        ctl._sleep = asleep(must_wake_now=True, sleep_hours_this_nap=11.1)
        d = ctl.decide(T0)
        check("单次睡眠 11.1h → 强制叫醒（否则自唤醒会变成永远睡着）",
              d.run and d.trigger == "sleep_max", f"实际 {d.trigger}")

        # ==================================================== 5b 启动先醒
        print("\n[5b] 启动后先醒一次（否则新部署要等一整个兜底间隔才见动静）")
        ctl, _ = make_ctl(td, booted=False)
        ctl.human.write({"interval_seconds": 5400})
        ctl._sleep = awake()
        ctl.last_beat_at = T0
        d = ctl.decide(T0 + 1)
        check("刚启动、离兜底还很远 → 依然先醒一次",
              d.run and d.trigger == "startup", f"实际 {d.trigger}")
        ctl.consume(d)
        d = ctl.decide(T0 + 2)
        check("这一轮之后不再重复（只一次）", not d.run, f"实际 {d.trigger} {d.reason}")

        ctl, _ = make_ctl(td, booted=False)
        ctl.human.write({"interval_seconds": 5400})
        ctl._sleep = asleep()
        d = ctl.decide(T0 + 1)
        check("启动时正在睡 → 不为一个标记把它吵醒",
              not d.run and d.sleep_quiet, f"实际 {d}")

        # ==================================================== 6 价格规则
        print("\n[6] 价格规则命中")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "default_pct": 2.0, "cooldown_seconds": 0})
        ctl._sleep = awake()
        ctl.refs = {"BTCUSDT": 100000.0}
        ctl.on_prices({"BTCUSDT": 100100.0})
        check("波动 0.1%（阈值 2%）→ 不触发", not ctl.has_pending())
        ctl.on_prices({"BTCUSDT": 102500.0})
        check("波动 +2.5%（阈值 2%）→ 触发", ctl.has_pending())
        check("触发源记为 price_human",
              ctl.pending[0]["source"] == "price_human", str(ctl.pending[0]))

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "cooldown_seconds": 0})
        ctl.agent.write({"thresholds": {"SOLUSDT": {"pct": 3.0}}})
        ctl._sleep = awake()
        ctl.refs = {"SOLUSDT": 120.0}
        ctl.on_prices({"SOLUSDT": 124.0})
        check("Agent 自设阈值也生效，且源记为 price_agent",
              ctl.has_pending() and ctl.pending[0]["source"] == "price_agent")

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "cooldown_seconds": 0})
        ctl.agent.write({"thresholds": {"BTCUSDT": {"above": 86000}}})
        ctl._sleep = awake()
        ctl.on_prices({"BTCUSDT": 85900.0})
        check("价格线 above 未触及 → 不触发", not ctl.has_pending())
        ctl.on_prices({"BTCUSDT": 86100.0})
        check("站上 86000 → 触发", ctl.has_pending())

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "cooldown_seconds": 0})
        ctl.agent.write({"thresholds": {"ETHUSDT": {"below": 2600}}})
        ctl._sleep = awake()
        ctl.on_prices({"ETHUSDT": 2580.0})
        check("跌破 below → 触发", ctl.has_pending())

        # ==================================================== 7 参考价重置
        print("\n[7] 参考价以「上一轮结束」为基准，所以不会连着响")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "default_pct": 1.0, "cooldown_seconds": 0})
        ctl._sleep = awake()
        ctl.refs = {"BTCUSDT": 100000.0}
        ctl.on_prices({"BTCUSDT": 102000.0})
        check("第一次越界 → 触发", ctl.has_pending())
        ctl.note_beat_done({"BTCUSDT": 102000.0})     # 心跳结束，参考价前移
        ctl.pending.clear()
        ctl.on_prices({"BTCUSDT": 102010.0})
        check("参考价前移后，同一个价位不再重复触发",
              not ctl.has_pending(), str(list(ctl.pending)))

        # ==================================================== 7b 一批多命中
        print("\n[7b] 一批里多个币同时越界 → 一次触发，命中的全列进 reason")
        ctl, bus = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "cooldown_seconds": 300, "max_triggers_per_hour": 10,
                         "thresholds": {"BTCUSDT": {"pct": 1.0},
                                        "ETHUSDT": {"pct": 1.0},
                                        "SOLUSDT": {"pct": 1.0}}})
        ctl._sleep = awake()
        ctl.refs = {"BTCUSDT": 100000.0, "ETHUSDT": 2600.0, "SOLUSDT": 120.0}
        ctl.on_prices({"BTCUSDT": 103000.0, "ETHUSDT": 2700.0, "SOLUSDT": 120.1})
        check("3 个币同时越界只登记 1 次触发（配额没被多花）",
              len(ctl.pending) == 1, str(list(ctl.pending)))
        reason = ctl.pending[0]["reason"] if ctl.has_pending() else ""
        check("BTC 在 reason 里", "BTCUSDT" in reason, reason)
        check("ETH 也在 reason 里（旧口径会把它丢掉）", "ETHUSDT" in reason, reason)
        check("没越界的 SOL 不出现在 reason 里", "SOLUSDT" not in reason, reason)
        evs = [e for e in bus.events if e["kind"] == "price_trigger"]
        check("price_trigger 事件带 hits 明细", bool(evs) and len(evs[-1].get("hits", [])) == 2,
              str(evs[-1:] if evs else None))
        check("带 count=2", bool(evs) and evs[-1].get("count") == 2,
              str(evs[-1:] if evs else None))
        ctl.on_prices({"BTCUSDT": 103000.0, "ETHUSDT": 2700.0})
        check("冷却期内不重复登记（这正是不聚合就会丢数据的地方）",
              len(ctl.pending) == 1, str(list(ctl.pending)))

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "cooldown_seconds": 0,
                         "thresholds": {"BTCUSDT": {"pct": 1.0}}})
        ctl.agent.write({"thresholds": {"SOLUSDT": {"pct": 1.0}}})
        ctl._sleep = awake()
        ctl.refs = {"BTCUSDT": 100000.0, "SOLUSDT": 120.0}
        ctl.on_prices({"BTCUSDT": 103000.0, "SOLUSDT": 125.0})
        check("人设的线和它自己押的线同时被踩 → 源记 price_human（人类优先）",
              ctl.has_pending() and ctl.pending[0]["source"] == "price_human",
              str(ctl.pending[0] if ctl.pending else None))
        check("两条都在 reason 里（跨源也不丢）",
              "BTCUSDT" in ctl.pending[0].get("reason", "")
              and "SOLUSDT" in ctl.pending[0].get("reason", ""),
              ctl.pending[0].get("reason", ""))

        # ==================================================== 8 上限与冷却
        print("\n[8] 每小时上限与冷却（防「行情疯起来就疯狂醒」）")
        ctl, bus = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "default_pct": 1.0, "cooldown_seconds": 0,
                         "max_triggers_per_hour": 3})
        ctl._sleep = awake()
        n = sum(1 for i in range(6) if ctl.request_wake("price_human", f"第 {i} 次"))
        check("上限 3/h：连发 6 次只登记 3 次", n == 3, f"实际 {n}")
        check("超限会发 wake_capped 事件（面板上看得见）",
              any(e["kind"] == "wake_capped" for e in bus.events))

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "cooldown_seconds": 300})
        ctl._sleep = awake()
        ctl.request_wake("price_human", "第一次")
        check("冷却 300s 内第二次被拒",
              not ctl.request_wake("price_human", "冷却期内的第二次"))

        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": False})
        ctl._sleep = awake()
        ctl.on_prices({"BTCUSDT": 999999.0})
        check("price_trigger_enabled=false → 价格触发整体关闭",
              not ctl.has_pending())

        # ==================================================== 9 面板改写
        print("\n[9] 面板改写要做白名单 + 钳位（那是网络入口）")
        ctl, _ = make_ctl(td)
        ctl.update_human({"interval_seconds": 10, "poll_seconds": 1,
                          "max_triggers_per_hour": -5, "cooldown_seconds": 0})
        p = ctl.human.read()
        check(f"interval 10s → 抬到 {wake_mod.MIN_INTERVAL}s",
              p["interval_seconds"] == wake_mod.MIN_INTERVAL)
        check(f"poll 1s → 抬到 {wake_mod.MIN_POLL}s",
              p["poll_seconds"] == wake_mod.MIN_POLL)
        check("负数上限 → 夹到 0", p["max_triggers_per_hour"] == 0)
        ctl.update_human({"evil_field": "x", "interval_seconds": 7200})
        check("白名单外的字段被忽略", "evil_field" not in ctl.human.read())
        check("合法值正常生效（2 小时）",
              ctl.human.read()["interval_seconds"] == 7200)

        # ==================================================== 10 Agent 侧
        print("\n[10] Agent 改自己那份策略时的钳位")
        ctl, _ = make_ctl(td)
        ctl.human.write({"interval_seconds": 5400})
        msg = ctl._tool_set_policy({"interval_minutes": 600})
        check("Agent 想设 600 分钟 → 被钳到人类上界并说明理由",
              ctl.agent.read().get("interval_seconds") == 5400
              and "超过了人类设的上界" in msg, msg[:80])
        ctl._tool_set_policy({"interval_minutes": 1})
        check("Agent 想设 1 分钟 → 抬到下限",
              ctl.agent.read()["interval_seconds"] == wake_mod.MIN_INTERVAL)
        ctl._tool_set_policy({"thresholds": [
            {"symbol": "solusdt", "pct": 2.5},
            {"symbol": "BTCUSDT", "above": 86000},
            {"symbol": "", "pct": 1},
        ]})
        tbl = ctl.agent.read()["thresholds"]
        check("小写符号被规整成大写", "SOLUSDT" in tbl, str(tbl))
        check("空符号被丢掉", "" not in tbl)
        check("规则表里能读出 Agent 那两条",
              len([r for r in ctl.rules() if r.source == "agent"]) == 2)
        ctl._tool_set_policy({"clear": True})
        check("clear=true 清空 Agent 侧",
              not ctl.agent.read().get("thresholds")
              and "interval_seconds" not in ctl.agent.read())

        # ==================================================== 11 睡眠记账
        print("\n[11] 睡眠记账")
        hub = FakeHub()
        ctl, _ = make_ctl(td, hub)
        ctl.human.write({"interval_seconds": 5400})

        async def flow():
            await ctl.refresh_sleep()
            a = (ctl.sleep or {}).get("sleeping")
            await ctl.enter_sleep("盘面没什么可看的")
            b = (ctl.sleep or {}).get("sleeping")
            return a, b

        s1, s2 = asyncio.run(flow())
        check("初始不是睡眠态", s1 is False)
        check("enter_sleep 之后是睡眠态", s2 is True)
        check("enter_sleep 真的调了 mcp-paper（账本在那边，不在 brain）",
              any(c[0] == "paper__sleep_start" for c in hub.calls))

        # ==================================================== 12 降级
        print("\n[12] 睡眠账本读不到时必须降级，不能弄挂调度")
        class BrokenHub:
            async def call(self, name, args=None):
                raise RuntimeError("mcp-paper 挂了")

        ctl, _ = make_ctl(td, BrokenHub())
        ctl.human.write({"interval_seconds": 600})
        ctl._sleep = awake()
        ctl.last_beat_at = T0
        d = ctl.decide(T0 + 700)
        check("账本读不到时按清醒处理，兜底照常工作",
              d.run and d.trigger == "schedule", f"实际 {d}")
        asyncio.run(ctl.refresh_sleep())
        check("刷新失败被标记（面板上要能看出账本不可达）", ctl._sleep_ok is False)

        # ==================================================== 12b 参考价初始化
        print("\n[12b] 进程刚启动、参考价为空时，pct 规则必须还能判定")
        hub = FakeHub()
        hub.prices = {"BTCUSDT": 84000.0, "ETHUSDT": 2680.0, "SOLUSDT": 121.0}
        ctl, bus = make_ctl(td, hub)
        ctl.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                         "cooldown_seconds": 0, "thresholds": {"BTCUSDT": {"pct": 1.0}}})
        ctl._sleep = asleep()
        ctl.refs = {}
        mon = wake_mod.PriceMonitor(ctl)
        asyncio.run(mon._tick())
        check("第一次取价就把参考价补上了（原来会一直是空的）",
              ctl.refs.get("BTCUSDT") == 84000.0, str(ctl.refs))
        check("初始化那一轮不当成触发（基准就是现价，没什么可报的）",
              not ctl.has_pending(), str(list(ctl.pending)))
        check("发了一条 wake_refs_seeded，面板上看得见",
              any(e["kind"] == "wake_refs_seeded" for e in bus.events),
              str([e["kind"] for e in bus.events]))

        hub.prices = {"BTCUSDT": 85260.0, "ETHUSDT": 2680.0, "SOLUSDT": 121.0}
        asyncio.run(mon._tick())
        check("参考价补上之后，+1.5% 在睡眠中也能把它叫醒",
              ctl.has_pending() and ctl.pending[0]["source"] == "price_human",
              str(list(ctl.pending)))

        # 反证：refs 为空时，同一批价格什么都不会发生 —— 这就是原来的行为。
        # 这个组合最难发现（重启时它正好在睡 → 永远没有第一轮心跳来填 refs）。
        ctl2, _ = make_ctl(td, hub)
        ctl2.human.write({"interval_seconds": 5400, "price_trigger_enabled": True,
                          "cooldown_seconds": 0, "thresholds": {"BTCUSDT": {"pct": 1.0}}})
        ctl2.refs = {}
        ctl2.on_prices({"BTCUSDT": 85260.0})      # 绕过 _tick，模拟"基准一直是空的"
        check("反证：refs 为空时静默无反应（`Rule.hit` 里那句 `if self.pct and ref`）",
              not ctl2.has_pending(), str(list(ctl2.pending)))

        # ==================================================== 13 工具表
        print("\n[13] 工具表与 schema")
        ctl, _ = make_ctl(td)
        names = [t["function"]["name"] for t in ctl.schemas()]
        check("三个工具都在：sleep / wake_up / set_wake_policy",
              set(names) == {"sleep", "wake_up", "set_wake_policy"}, str(names))
        sch = {t["function"]["name"]: t for t in ctl.schemas()}
        check("sleep 的 reason 是必填",
              "reason" in sch["sleep"]["function"]["parameters"]["required"])
        check("set_wake_policy 支持 thresholds 数组",
              "thresholds" in sch["set_wake_policy"]["function"]["parameters"]["properties"])
        check("sleep 的说明里点名了「睡前设止损」",
              "止损" in sch["sleep"]["function"]["description"])

    print("\n" + "=" * 68)
    if FAILS:
        print(f"  失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"    · {f}")
        print("=" * 68)
        return 1
    print("  全部通过")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
