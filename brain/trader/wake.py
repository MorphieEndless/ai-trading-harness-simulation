"""唤醒策略与睡眠：这个项目里"什么时候该醒"的唯一决策点。

# 三类唤醒

| 触发源        | 谁设的          | 落在哪                                   |
|---------------|-----------------|------------------------------------------|
| `schedule`    | 定时兜底        | 人类设上界，Agent 只能往短里改            |
| `price_human` | 人类            | 面板 / config/wake_policy.json（只读挂载）|
| `price_agent` | Agent 自己      | 工作区 wake_policy.json                  |

# 定时唤醒的语义：间隔兜底，不是固定节拍

    「距上一次*任何类型*的唤醒超过 interval 秒，就必定醒一次。」

不是"每 interval 秒醒一次"。第 32 分钟自唤醒了一次，第 60 分钟那次就不会发生 ——
因为那时离上次唤醒才 28 分钟。**保证的是"最长沉默不超过 interval"**，这才是兜底。

这样写还有个好处：连着几次自唤醒、或者三类混合发生，都不需要额外判断。
另一种写法（维护"窗口内是否发生过别的唤醒"）要多存一份状态，还容易在
"自唤醒正好卡在边界"上出错。

# 睡眠

配额在 mcp-paper 的 SQLite 里（见那个文件里的注释：brain 容器里没有任何地方
是 Agent 改不到的，`data/db` 是唯一例外）。

**睡眠期间定时唤醒全部静默** —— 这是省 token 的主要来源。6.5 小时的睡眠里
零次 LLM 调用。只有两种情况能把它从床上拽起来：

  · 它自己睡前押的价格阈值（自唤醒）—— 这是它自己的选择，认账
  · 人类设的价格阈值 —— 人的判断，人负责（面板可关）

被叫醒**不影响睡眠债**。半夜爬起来处理两次，早上照样得把 6.5 小时睡够。
所以"自唤醒能叫醒睡梦中的操盘手"和"一天必须睡满"这两条能同时成立。

止损不靠它守：`mcp-paper` 的后台巡视线程每 30 秒跑一次，跟它醒不醒没关系。
这是它敢睡的底气，也是"睡前必须把止损设好"这条提醒的意义。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("trader.wake")

# 兜底间隔的钳位：再勤快也别 5 分钟以内醒一次，那是烧钱不是盯盘
MIN_INTERVAL = 300
# 面板策略文件：brain 可写、Agent 的 fs_* 够不到（run_shell 那条路后面单独修）
PANEL_POLICY_PATH = "/data/logs/wake_policy.json"
# Agent 自己的策略：在工作区里，它爱怎么写怎么写
AGENT_POLICY_REL = "wake_policy.json"
# 价格监控的轮询下限，防止有人把面板填成 1 秒
MIN_POLL = 15
# 规则条数上限，防手滑填出一百条
MAX_RULES = 24


@dataclass
class Rule:
    """一条价格触发规则。pct / above / below 至少有一个。"""

    symbol: str
    source: str                      # human | agent
    pct: float | None = None
    above: float | None = None
    below: float | None = None

    def hit(self, price: float, ref: float | None) -> str | None:
        if self.pct and ref:
            move = (price - ref) / ref * 100.0
            if abs(move) >= self.pct:
                return (f"{self.symbol} 现价 {price:g}，相对上次看盘的 {ref:g} "
                        f"波动 {move:+.2f}%（阈值 {self.pct:g}%）")
        if self.above is not None and price >= self.above:
            return f"{self.symbol} 现价 {price:g}，站上了 {self.above:g}"
        if self.below is not None and price <= self.below:
            return f"{self.symbol} 现价 {price:g}，跌破了 {self.below:g}"
        return None


@dataclass
class Decision:
    """调度器该干什么。"""

    run: bool = False               # 要不要跑一轮心跳（花 token）
    trigger: str = ""               # schedule / price_human / price_agent / ...
    reason: str = ""
    force_sleep: bool = False       # 不跑 LLM，直接标记入睡
    sleep_quiet: bool = False       # 正睡着，静默


def _now_str(ts: float | None) -> str | None:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else None


class JsonPolicy:
    """一份 JSON 策略文件，按 mtime 热加载（和 persona.md 同一套路数）。"""

    def __init__(self, path: str, seed_path: str | None = None) -> None:
        self.path = path
        self.seed_path = seed_path
        self._mtime: float = -1.0
        self._data: dict[str, Any] = {}

    def read(self) -> dict[str, Any]:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            # 面板那份还不存在：用仓库里的种子初始化一次，之后以面板那份为准
            if self.seed_path and os.path.isfile(self.seed_path):
                try:
                    with open(self.seed_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.write(data)
                    log.info("唤醒策略已从种子初始化：%s", self.path)
                except (OSError, json.JSONDecodeError) as exc:
                    log.warning("读取种子策略失败：%s", exc)
            self._mtime = -1.0
            self._data = self._read_direct()
            return self._data
        if mtime == self._mtime:
            return self._data
        self._mtime = mtime
        self._data = self._read_direct()
        return self._data

    def _read_direct(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def write(self, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._mtime = -1.0


class WakeController:
    def __init__(self, cfg, hub, bus, wake_event: asyncio.Event) -> None:
        self.cfg = cfg
        self.hub = hub
        self.bus = bus
        self.wake_event = wake_event

        self.human = JsonPolicy(
            PANEL_POLICY_PATH,
            seed_path="/app/config/wake_policy.json",
        )
        self.agent = JsonPolicy(os.path.join(cfg.workspace_dir, AGENT_POLICY_REL))

        # 参考价：上一次心跳结束时各币的价格。价格阈值拿它当基准，
        # 所以口径是"自上回你看盘以来，有没有发生值得醒一次的事"。
        self.refs: dict[str, float] = {}
        self.last_prices: dict[str, float] = {}
        self._last_prices_at: float = 0.0

        # 排队中的触发（价格类）。调度器取走后清空。
        self.pending: deque[dict] = deque(maxlen=8)
        self._price_trigger_times: deque[float] = deque(maxlen=64)
        self._cooldown_until: float = 0.0

        # 睡眠快照（由 mcp-paper 提供，异步刷新后缓存在这里供同步决策读）
        self._sleep: dict[str, Any] = {"sleeping": False, "debt_hours": 0.0,
                                       "awake_span_hours": 0.0}
        self._sleep_ok: bool = True

        self.last_beat_at: float = time.time()
        self.stats: dict[str, int] = {}
        self.last_trigger: dict[str, Any] = {}
        self.skipped_quiet: int = 0
        # 启动后先醒一次。理由不是"想看看"，是**必须核对状态**：
        # 进程重启期间 mcp-paper 的后台止损巡逻线程照常在跑，
        # 可能有仓位已经被平掉而它还不知道；而且重启前它到底沉默多久也无从得知。
        # 不做这一步的话，新部署要等一整个兜底间隔才有第一轮，
        # 面板上什么都看不到，调试没抓手 —— 这正是这次要修的那类问题。
        self._boot_done = False

    # ------------------------------------------------------------------ 策略
    def effective(self) -> dict[str, Any]:
        """人类的值 + Agent 的值 → 实际生效的一份。

        钳位规则：**人类的值是上界，Agent 只能往短里改。**
        否则 Agent 可以给自己放假到明天，"兜底"就没意义了。
        """
        h = self.human.read()
        a = self.agent.read()

        h_interval = _num(h, "interval_seconds", 5400)
        a_interval = a.get("interval_seconds")
        interval = h_interval
        if a_interval:
            interval = max(MIN_INTERVAL, min(int(a_interval), h_interval))

        return {
            "interval_seconds": interval,
            "human_interval_seconds": h_interval,
            "agent_interval_seconds": int(a_interval) if a_interval else None,
            "interval_clamped": bool(a_interval and int(a_interval) < MIN_INTERVAL),
            "price_trigger_enabled": bool(h.get("price_trigger_enabled", True)),
            "trigger_during_sleep": bool(h.get("trigger_during_sleep", True)),
            "max_triggers_per_hour": max(0, _num(h, "max_triggers_per_hour", 10)),
            "poll_seconds": max(MIN_POLL, _num(h, "poll_seconds", 30)),
            "cooldown_seconds": max(0, _num(h, "cooldown_seconds", 300)),
        }

    def rules(self) -> list[Rule]:
        h = self.human.read()
        a = self.agent.read()
        out: list[Rule] = []

        for source, store in (("human", h), ("agent", a)):
            table = store.get("thresholds") or {}
            if not isinstance(table, dict):
                continue
            for sym, spec in list(table.items())[:MAX_RULES]:
                if isinstance(spec, (int, float)):
                    spec = {"pct": float(spec)}
                if not isinstance(spec, dict):
                    continue
                out.append(Rule(
                    symbol=str(sym).upper(),
                    source=source,
                    pct=_opt_float(spec.get("pct")),
                    above=_opt_float(spec.get("above")),
                    below=_opt_float(spec.get("below")),
                ))

        # 人类的 default_pct 兜住"没单独配的币"。只在焦点列表里生效，
        # 免得它去监控一堆没人看的对。
        dpct = _opt_float(h.get("default_pct"))
        if dpct:
            configured = {r.symbol for r in out if r.source == "human"}
            for sym in self.cfg.watchlist:
                if sym not in configured:
                    out.append(Rule(symbol=sym, source="human", pct=dpct))
        return out

    def watched_symbols(self) -> list[str]:
        syms = {r.symbol for r in self.rules()}
        syms |= set(self.refs)
        return sorted(syms)[:40]

    def prices_age_seconds(self) -> float | None:
        """面板用：上一次取到价是几秒前。None = 这个进程还没取到过。"""
        if not self._last_prices_at:
            return None
        return round(time.time() - self._last_prices_at, 1)

    # ------------------------------------------------------------------ 睡眠
    async def refresh_sleep(self) -> dict[str, Any]:
        """从 mcp-paper 拉睡眠账本（那边是权威，因为这边改得到）。"""
        try:
            raw = await self.hub.call("paper__sleep_state")
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict) and "sleeping" in data:
                self._sleep = data
                self._sleep_ok = True
        except Exception as exc:
            # 账本读不到不能弄挂调度 —— 按"清醒"处理，并在面板上标出来。
            self._sleep_ok = False
            log.warning("读取睡眠账本失败：%s", exc)
        return self._sleep

    @property
    def sleep(self) -> dict[str, Any]:
        return self._sleep

    async def enter_sleep(self, reason: str) -> dict[str, Any]:
        try:
            raw = await self.hub.call("paper__sleep_start", {"reason": reason})
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict) and data.get("ok"):
                self._sleep = data
                self.bus.emit("sleep_start", reason=reason, **self._sleep_fields())
                log.info("进入睡眠：%s", reason)
            else:
                await self.refresh_sleep()
        except Exception as exc:
            log.warning("进入睡眠失败：%s", exc)
        return self._sleep

    async def leave_sleep(self, reason: str) -> dict[str, Any]:
        try:
            raw = await self.hub.call("paper__sleep_end", {"reason": reason})
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict) and data.get("ok"):
                self.bus.emit("sleep_end", reason=reason,
                              slept_hours=data.get("slept_hours"),
                              **self._sleep_fields())
                log.info("醒来：%s（睡了 %s 小时）", reason, data.get("slept_hours"))
            await self.refresh_sleep()
        except Exception as exc:
            log.warning("结束睡眠失败：%s", exc)
        return self._sleep

    def _sleep_fields(self) -> dict[str, Any]:
        s = self._sleep or {}
        return {
            "debt_hours": s.get("debt_hours"),
            "slept_last_24h_hours": s.get("slept_last_24h_hours"),
            "awake_span_hours": s.get("awake_span_hours"),
        }

    # ------------------------------------------------------------------ 触发
    def request_wake(self, source: str, reason: str, **extra: Any) -> bool:
        """登记一次触发并叫醒调度器。返回是否真的登记成功（受冷却/上限约束）。"""
        now = time.time()

        if source in ("price_human", "price_agent"):
            eff = self.effective()
            # 冷却：防暴涨时每 30 秒醒一次
            if now < self._cooldown_until:
                return False
            # 每小时上限：**这条是防"行情疯了就疯狂醒"的关键闸门。**
            # 不设它的话，最贵的时候恰好是行情最剧烈的时候，成本会比固定心跳还高。
            recent = [t for t in self._price_trigger_times if now - t < 3600]
            self._price_trigger_times = deque(recent, maxlen=64)
            if len(recent) >= eff["max_triggers_per_hour"]:
                self.bus.emit("wake_capped", source=source, reason=reason,
                              used=len(recent), cap=eff["max_triggers_per_hour"])
                return False
            self._price_trigger_times.append(now)
            self._cooldown_until = now + eff["cooldown_seconds"]

        item = {"source": source, "reason": reason, "at": now, **extra}
        self.pending.append(item)
        self.wake_event.set()
        self.bus.emit("wake_request", **item)
        return True

    def has_pending(self) -> bool:
        return bool(self.pending)

    def _peek(self) -> dict | None:
        return self.pending[0] if self.pending else None

    def consume(self, d: Decision) -> None:
        """调度器决定跑这一轮了：把对应的排队项取走。"""
        if self.pending:
            self.pending.popleft()
        self.stats[d.trigger] = self.stats.get(d.trigger, 0) + 1
        self.last_trigger = {
            "source": d.trigger, "reason": d.reason, "at": _now_str(time.time()),
        }

    def clear_pending(self) -> None:
        self.pending.clear()

    # ------------------------------------------------------------------ 决策
    def decide(self, now: float | None = None) -> Decision:
        now = now if now is not None else time.time()
        eff = self.effective()
        sl = self._sleep or {}

        # 启动后先醒一次（只一次）。睡着的话就算了 —— 不值得为一个标记把它吵醒，
        # 那不正好违反"睡眠期间静默"。等它自己醒来的那一轮再核账。
        if not self._boot_done:
            self._boot_done = True
            if not sl.get("sleeping"):
                return Decision(
                    run=True, trigger="startup",
                    reason=("进程刚启动，先醒一次核对状态 —— 你不在的这段时间里，"
                            "后台止损巡逻线程照常在跑，可能有仓位已经被平掉了。"),
                )

        if sl.get("sleeping"):
            # 睡太久 → 必须叫醒（单次上限）。这一步不能省，
            # 否则"自唤醒能叫醒他"会变成"永远睡着"。
            if sl.get("must_wake_now"):
                return Decision(
                    run=True, trigger="sleep_max",
                    reason=(f"这一觉已经睡了 {sl.get('sleep_hours_this_nap')} 小时，"
                            f"到达单次上限 {sl.get('max_single_hours')} 小时，该醒了。"),
                )
            p = self._peek()
            if p:
                if p["source"] == "price_agent":
                    return Decision(run=True, trigger="price_agent", reason=p["reason"])
                if p["source"] == "price_human" and eff["trigger_during_sleep"]:
                    return Decision(run=True, trigger="price_human", reason=p["reason"])
                if p["source"] == "human_manual":
                    return Decision(run=True, trigger="human_manual", reason=p["reason"])
                # 睡眠期间定时唤醒一律静默 —— 省 token 就在这一行
                self.pending.popleft()
                self.skipped_quiet += 1
            return Decision(run=False, sleep_quiet=True,
                            reason="睡眠中，定时唤醒静默")

        # 连续清醒到头了 → 强制入睡（不跑 LLM，省钱）
        if sl.get("must_sleep_now"):
            return Decision(
                run=False, force_sleep=True,
                reason=(f"连续清醒 {sl.get('awake_span_hours')} 小时，"
                        f"超过上限 {sl.get('max_awake_hours')} 小时，强制入睡"),
            )

        # 价格触发排队中，优先于兜底
        p = self._peek()
        if p and eff["price_trigger_enabled"]:
            return Decision(run=True, trigger=p["source"], reason=p["reason"])
        if p:
            self.pending.popleft()

        # 间隔兜底
        idle = now - self.last_beat_at
        if idle >= eff["interval_seconds"]:
            minutes = int(idle // 60)
            return Decision(
                run=True, trigger="schedule",
                reason=(f"距上次唤醒已 {minutes} 分钟，超过兜底间隔 "
                        f"{eff['interval_seconds'] // 60} 分钟。"),
            )
        return Decision(run=False, reason="未到兜底时间")

    def fallback_deadline(self) -> float:
        """下一次兜底唤醒的时间戳。面板的倒计时读它。"""
        return self.last_beat_at + self.effective()["interval_seconds"]

    def next_check_seconds(self, now: float | None = None) -> float:
        """距离"下一次有可能该醒"还有多久。用于决定 idler 睡多长。"""
        now = now if now is not None else time.time()
        eff = self.effective()
        sl = self._sleep or {}
        if sl.get("sleeping"):
            if sl.get("sleep_hours_this_nap") is not None:
                left = ((sl.get("max_single_hours") or 11) -
                        (sl.get("sleep_hours_this_nap") or 0)) * 3600
                return max(5.0, min(left, 600.0))
            return 600.0
        return max(5.0, min(eff["interval_seconds"] - (now - self.last_beat_at), 600.0))

    # ------------------------------------------------------------------ 心跳钩子
    def note_beat_done(self, prices: dict[str, float] | None = None) -> None:
        """一轮结束：更新参考价、记时间。参考价重置是价格触发"不连续响"的关键。"""
        self.last_beat_at = time.time()
        if prices:
            self.refs.update(prices)
            self.last_prices.update(prices)
            self._last_prices_at = time.time()

    def on_prices(self, prices: dict[str, float]) -> None:
        """价格监控每次取完价都走这里。只登记，不决策。

        同一批里可能好几个币一起越界。原来看见第一条就 `return`，
        注释写的是"剩下的下一轮再说" —— 其实**下一轮轮不到**：
        第一次登记就把冷却推到 15 分钟以后，第二次直接 False。
        所以那句话的实效等于"剩下的被丢掉"。

        现在的口径：**一批算一次触发，把命中的全列进 reason。**
        小时上限和冷却一个没松（配额还是只走一次），
        但醒过来的那一轮能看到全景，不用靠猜为什么醒。
        """
        self.last_prices = dict(prices)
        self._last_prices_at = time.time()
        eff = self.effective()
        if not eff["price_trigger_enabled"]:
            return

        hits: list[dict[str, Any]] = []
        for rule in self.rules():
            price = prices.get(rule.symbol)
            if not price:
                continue
            why = rule.hit(price, self.refs.get(rule.symbol))
            if not why:
                continue
            hits.append({
                "symbol": rule.symbol,
                "price": price,
                "ref": self.refs.get(rule.symbol),
                "source": "human" if rule.source == "human" else "agent",
                "why": why,
                "rule": f"pct={rule.pct} above={rule.above} below={rule.below}",
            })
        if not hits:
            return

        # 触发源取"人类优先"：人类的线被踩了，比它自己押的线被踩了更该在面板上显眼。
        primary = ("price_human" if any(h["source"] == "human" for h in hits)
                   else "price_agent")
        reason = (hits[0]["why"] if len(hits) == 1
                  else f"{len(hits)} 个价格阈值同时触发：" + "；".join(h["why"] for h in hits))

        ok = self.request_wake(
            primary, reason,
            symbol=hits[0]["symbol"], price=hits[0]["price"], ref=hits[0]["ref"],
            hits=[{"symbol": h["symbol"], "source": h["source"], "why": h["why"]}
                  for h in hits],
        )
        if ok:
            self.bus.emit("price_trigger", source=primary,
                          symbol=hits[0]["symbol"], price=hits[0]["price"],
                          ref=hits[0]["ref"], rule=hits[0]["rule"],
                          count=len(hits), hits=hits)

    # ------------------------------------------------------------------ 面板
    def status(self) -> dict[str, Any]:
        eff = self.effective()
        now = time.time()
        return {
            "policy": eff,
            "human": self.human.read(),
            "agent": self.agent.read(),
            "rules": [
                {"symbol": r.symbol, "source": r.source, "pct": r.pct,
                 "above": r.above, "below": r.below}
                for r in self.rules()
            ],
            "sleep": self._sleep,
            "sleep_ledger_ok": self._sleep_ok,
            "refs": {k: round(v, 6) for k, v in self.refs.items()},
            "last_prices": {k: round(v, 6) for k, v in self.last_prices.items()},
            "last_prices_at": _now_str(self._last_prices_at),
            # 面板要判断"这价是新的还是三分钟前的"。给秒数，前端自己决定要不要标灰。
            "prices_age_seconds": self.prices_age_seconds(),
            "stats": dict(self.stats),
            "last_trigger": self.last_trigger,
            "pending": len(self.pending),
            "skipped_quiet": self.skipped_quiet,
            "price_triggers_last_hour": len(
                [t for t in self._price_trigger_times if now - t < 3600]),
            "cooldown_left": max(0, int(self._cooldown_until - now)),
            "next_fallback_at": _now_str(self.last_beat_at + eff["interval_seconds"]),
            "policy_path": PANEL_POLICY_PATH,
        }

    def update_human(self, patch: dict[str, Any]) -> dict[str, Any]:
        """人类从面板改策略。只接受白名单字段，数值做钳位 —— 面板是网络入口。"""
        cur = dict(self.human.read())
        for key, cast in (("interval_seconds", int),
                          ("max_triggers_per_hour", int),
                          ("poll_seconds", int),
                          ("cooldown_seconds", int)):
            if key in patch and patch[key] is not None:
                cur[key] = cast(patch[key])
        for key in ("price_trigger_enabled", "trigger_during_sleep"):
            if key in patch and patch[key] is not None:
                cur[key] = bool(patch[key])
        if patch.get("default_pct") is not None:
            val = _opt_float(patch["default_pct"])
            if val is not None and val > 0:
                cur["default_pct"] = val
        if isinstance(patch.get("thresholds"), dict):
            cur["thresholds"] = {
                str(k).upper(): v for k, v in list(patch["thresholds"].items())[:MAX_RULES]
            }
        if patch.get("reset_thresholds"):
            cur["thresholds"] = {}

        cur["interval_seconds"] = max(MIN_INTERVAL, int(cur.get("interval_seconds") or 5400))
        cur["poll_seconds"] = max(MIN_POLL, int(cur.get("poll_seconds") or 30))
        cur["max_triggers_per_hour"] = max(0, int(cur.get("max_triggers_per_hour") or 10))
        cur["cooldown_seconds"] = max(0, int(cur.get("cooldown_seconds") or 300))

        self.human.write(cur)
        self.bus.emit("wake_policy_changed", by="human", policy=cur)
        return cur

    # ------------------------------------------------------------------ Agent 工具
    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "sleep",
                    "description": (
                        "去睡觉。睡眠期间定时唤醒会被系统静默，不会有人叫你起来看盘 ——\n"
                        "只有两种情况能把你从床上拽起来：你自己睡前押的价格阈值（自唤醒）、\n"
                        "或者人类设的阈值。\n\n"
                        "**睡前务必确认持仓的止损都设好了。** 睡眠期间市场照常波动，\n"
                        "替你守夜的是模拟盘的后台止损巡视线程（每 30 秒一次），\n"
                        "它只认你已经设好的止损价 —— 没设止损的仓位在那段时间里是完全裸奔的。\n\n"
                        "配额是硬性的：一天最少睡 6.5 小时，单次最长 11 小时，\n"
                        "连续清醒超过 17.5 小时会被系统强制送回睡眠。这些你改不了。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "为什么现在睡。写给自己看的一句话。",
                            },
                        },
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wake_up",
                    "description": (
                        "主动结束睡眠（自然醒）。睡眠期间被价格叫醒**不需要**调这个 —— "
                        "处理完那一轮你会自动回到睡眠状态，直到你自己认为该醒了。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string", "description": "为什么醒。"},
                        },
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_wake_policy",
                    "description": (
                        "设置你自己那侧的唤醒策略：兜底间隔 + 自唤醒的价格阈值。\n\n"
                        "**兜底间隔**：距上次唤醒超过这么久就必定醒一次（保证最长沉默）。\n"
                        f"你只能把它改得比人类设的上界更短（下限 {MIN_INTERVAL} 秒）；"
                        "想更长得请人类去面板改。\n\n"
                        "**自唤醒阈值**：给某个币设一个相对波动百分比（相对你上次看盘的价格），"
                        "或者一条价格线（站上 / 跌破）。触发了系统会唤醒你 ——"
                        "**包括睡眠中**。这是你睡觉时唯一能自己争取到的叫醒服务，"
                        "睡前押好它比事后后悔有用。\n\n"
                        "调用是覆盖式的：本工具写入的内容会整体替换你上次设的阈值集合，"
                        "所以要把想要的规则一次列全。传 clear=true 清空全部自唤醒规则。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "interval_minutes": {
                                "type": "integer",
                                "description": "兜底间隔（分钟）。只能短于人类的上界。",
                            },
                            "thresholds": {
                                "type": "array",
                                "description": "自唤醒规则列表（覆盖式写入）",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "symbol": {"type": "string", "description": "交易对，如 SOLUSDT"},
                                        "pct": {
                                            "type": "number",
                                            "description": "相对上次看盘价的波动百分比，涨跌都算。",
                                        },
                                        "above": {
                                            "type": "number",
                                            "description": "价格站上这个数就叫醒我。",
                                        },
                                        "below": {
                                            "type": "number",
                                            "description": "价格跌破这个数就叫醒我。",
                                        },
                                    },
                                    "required": ["symbol"],
                                },
                            },
                            "clear": {
                                "type": "boolean",
                                "description": "清空你设的全部自唤醒规则（重置）",
                            },
                        },
                    },
                },
            },
        ]

    async def call_tool(self, name: str, args: dict[str, Any], agent=None) -> str:
        if name == "sleep":
            return await self._tool_sleep(str(args.get("reason") or "").strip(), agent)
        if name == "wake_up":
            return await self._tool_wake_up(str(args.get("reason") or "").strip())
        if name == "set_wake_policy":
            return self._tool_set_policy(args)
        return f"[未知唤醒工具] {name}"

    async def _tool_sleep(self, reason: str, agent=None) -> str:
        await self.refresh_sleep()
        if (self._sleep or {}).get("sleeping"):
            return (f"[已经是睡眠状态] 从 "
                    f"{(self._sleep or {}).get('sleeping_since')} 开始，"
                    f"已睡 {(self._sleep or {}).get('sleep_hours_this_nap')} 小时。")

        # 睡前体检：只警告不拦（Operator 定的）
        warn = ""
        if agent is not None:
            try:
                raw = await agent.hub.call("paper__get_positions")
                pos = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(pos, dict):
                    pos = pos.get("positions") or []
                naked = [p.get("symbol") for p in (pos or [])
                         if isinstance(p, dict) and not p.get("stop_loss")]
                if naked:
                    warn = (f"\n\n⚠️ 警告：{('、'.join(naked))} 还没有止损。"
                            "睡眠期间市场照常波动，而这几个仓位没人守 ——"
                            "后台巡视线程只认你设好的止损价。要不要先设了再睡。")
            except Exception as exc:
                log.warning("睡前检查持仓失败：%s", exc)

        await self.enter_sleep(reason or "（未说明理由）")
        sl = self._sleep or {}
        eff = self.effective()
        return (
            f"已进入睡眠。定时唤醒将被静默，最长沉默 {eff['interval_seconds'] // 60} 分钟，"
            "但那指的是清醒时；睡眠期间只有价格触发能叫你。\n"
            f"睡眠配额：一天最少 {sl.get('min_hours_per_day')} 小时，单次最长 "
            f"{sl.get('max_single_hours')} 小时，连续清醒上限 "
            f"{sl.get('max_awake_hours')} 小时。\n"
            f"当前：滚动 24 小时已睡 {sl.get('slept_last_24h_hours')} 小时，"
            f"还欠 {sl.get('debt_hours')} 小时。" + warn
        )

    async def _tool_wake_up(self, reason: str) -> str:
        await self.refresh_sleep()
        if not (self._sleep or {}).get("sleeping"):
            return "[当前不是睡眠状态] 没必要醒 —— 你本来就醒着。"
        data = await self.leave_sleep(reason or "自然醒")
        sl = self._sleep or {}
        msg = (f"醒了，这一觉睡了 {data.get('slept_hours')} 小时。"
               f"滚动 24 小时累计 {sl.get('slept_last_24h_hours')} 小时"
               f"（要求 {sl.get('required_hours')} 小时）。")
        if (sl.get("debt_hours") or 0) > 0:
            msg += (f"\n还欠 {sl.get('debt_hours')} 小时 —— 欠着的时候，"
                    f"连续清醒超过 {sl.get('max_awake_hours')} 小时会被强制送回睡眠。")
        return msg

    def _tool_set_policy(self, args: dict[str, Any]) -> str:
        cur = dict(self.agent.read())
        eff = self.effective()

        if args.get("clear"):
            cur.pop("thresholds", None)
            cur.pop("interval_seconds", None)
            self.agent.write(cur)
            self.bus.emit("wake_policy_changed", by="agent", policy=cur)
            return "已清空你的自唤醒规则与自定间隔，回到人类设的值。"

        notes: list[str] = []

        if args.get("interval_minutes") is not None:
            try:
                want = int(args["interval_minutes"]) * 60
            except (TypeError, ValueError):
                return "[参数错误] interval_minutes 要是整数分钟。"
            upper = eff["human_interval_seconds"]
            if want > upper:
                notes.append(
                    f"你要的 {want // 60} 分钟超过了人类设的上界 {upper // 60} 分钟，"
                    f"已按上界处理 —— 兜底不能比人类允许的更松。")
                want = upper
            if want < MIN_INTERVAL:
                notes.append(f"下限是 {MIN_INTERVAL // 60} 分钟，已抬到下限。")
                want = MIN_INTERVAL
            cur["interval_seconds"] = want

        if args.get("thresholds") is not None:
            raw = args["thresholds"]
            if not isinstance(raw, list):
                return "[参数错误] thresholds 要是一个数组。"
            table: dict[str, Any] = {}
            for item in raw[:MAX_RULES]:
                if not isinstance(item, dict):
                    continue
                sym = str(item.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                spec: dict[str, float] = {}
                for k in ("pct", "above", "below"):
                    v = _opt_float(item.get(k))
                    if v is not None:
                        spec[k] = v
                if spec:
                    table[sym] = spec
            cur["thresholds"] = table

        self.agent.write(cur)
        self.bus.emit("wake_policy_changed", by="agent", policy=cur)

        eff = self.effective()
        lines = [
            f"你的唤醒策略已更新（写在 {AGENT_POLICY_REL}）。",
            f"兜底间隔：{eff['interval_seconds'] // 60} 分钟"
            f"（人类上界 {eff['human_interval_seconds'] // 60} 分钟）。",
        ]
        mine = [r for r in self.rules() if r.source == "agent"]
        if mine:
            lines.append("自唤醒规则：")
            for r in mine:
                bits = []
                if r.pct:
                    bits.append(f"波动 ±{r.pct:g}%")
                if r.above:
                    bits.append(f"站上 {r.above:g}")
                if r.below:
                    bits.append(f"跌破 {r.below:g}")
                lines.append(f"  · {r.symbol}：{' / '.join(bits)}")
        else:
            lines.append("自唤醒规则：无（睡眠期间不会被价格叫醒）")
        if notes:
            lines.append("")
            lines.extend(notes)
        return "\n".join(lines)


def _opt_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None       # 挡掉 NaN


def _num(d: dict, key: str, default: float, cast=int):
    """从策略 dict 里取一个数。

    **不要写成 `d.get(key) or default`** —— 那个 `or` 会把合法的 0 吞掉，
    于是"冷却设成 0（关闭冷却）"会静默变成 300 秒。这个坑是测试抓出来的。
    """
    v = d.get(key)
    if v is None or v == "":
        return cast(default)
    try:
        return cast(v)
    except (TypeError, ValueError):
        return cast(default)


class PriceMonitor:
    """后台价格监控。每 poll_seconds 取一次价，命中规则就登记一次唤醒。

    **它不花任何 token** —— 内网 HTTP 调 market__get_prices，不经过 LLM。
    所以哪怕它 30 秒跑一次，成本也是零。
    """

    def __init__(self, ctl: WakeController) -> None:
        self.ctl = ctl

    async def run(self) -> None:
        # 启动后稍等，让 MCP 连接先稳定
        await asyncio.sleep(15)
        while True:
            try:
                await asyncio.sleep(self.ctl.effective()["poll_seconds"])
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("价格监控异常：%s: %s", type(exc).__name__, exc)
                await asyncio.sleep(30)

    async def fetch(self, syms: list[str]) -> dict[str, float]:
        """批量取价。被监控循环和「心跳结束时刷新参考价」共用一个实现。"""
        if not syms:
            return {}
        raw = await self.ctl.hub.call("market__get_prices", {"symbols": syms})
        data = json.loads(raw) if isinstance(raw, str) else raw
        items = data if isinstance(data, list) else (data or {}).get("prices") or []
        prices: dict[str, float] = {}
        for item in items:
            if isinstance(item, dict) and item.get("symbol"):
                try:
                    prices[str(item["symbol"]).upper()] = float(item["price"])
                except (TypeError, ValueError):
                    continue
        return prices

    async def snapshot(self) -> dict[str, float]:
        """取一次当前价，不改任何状态。心跳结束时拿它刷新参考价。"""
        try:
            return await self.fetch(self.ctl.watched_symbols())
        except Exception as exc:
            log.warning("心跳结束取价失败：%s", exc)
            return {}

    async def _tick(self) -> None:
        ctl = self.ctl
        # 注意这里**不再**因为价格触发开关关着就提前返回。
        #
        # 原来第一行就是 `if not price_trigger_enabled: return`，语义上没错 ——
        # 开关关了就不该判阈值。但这样一来 `last_prices` 会一直空着，
        # 而面板上的「实时行情」正是读它（2026-09-27 加的）。
        #
        # 所以现在分开：**取价永远做，判定交给 on_prices**（它自己会看那个开关）。
        # 代价是零 —— `market__get_prices` 是批量的，一次 HTTP 拿 40 个币，
        # 而且这条链路不经过 LLM，一个 token 都不花。
        prices = await self.fetch(ctl.watched_symbols())
        if not prices:
            return
        # 参考价只由「上一轮心跳结束」写入（note_beat_done）。所以进程一起床它是空的，
        # 而 `Rule.hit` 里那句 `if self.pct and ref:` 会**安静地**跳过所有 pct 规则。
        # 于是有一个很难发现的组合：
        #
        #     重启时它正好在睡  →  没有心跳，refs 永远是空的
        #                      →  人类的 ±1% 和它自己押的百分比阈值**全部形同虚设**，
        #                         只剩价格线（above/below）还活着。
        #
        # 2026-09-27 部署后才看出来（面板上 refs 显示 {}，而 sleep 是 true）。
        # 所以第一次取到价就把它当基准，语义是「从进程启动那一刻起算」——
        # 比"什么都不算"好，也比"假装它上次看盘时就是这个价"诚实。
        # 顺带：这一轮仍然要走 on_prices，因为 above/below 是绝对价格线，
        # 该不该触发跟基准无关。
        if not ctl.refs:
            ctl.refs.update(prices)
            ctl.bus.emit("wake_refs_seeded",
                         reason="进程刚启动，参考价按当前价初始化（pct 规则此前无法判定）",
                         prices=prices)
        ctl.on_prices(prices)
