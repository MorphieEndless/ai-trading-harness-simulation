"""心跳 Agent：一轮"唤醒 → 观察 → 决策 → 记录"的完整实现。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter
from typing import Any

from .config import Config
from .events import EventBus
from .llm import LLM, LLMError
from .mcp_hub import MCPHub
from .narrator import Narrator
from .prompts import beat_context_template, build_system_prompt
from .subagent import SubAgent
from .tools_local import LocalTools

log = logging.getLogger("trader.agent")

# 面板上一条 tool_result 最多显示多少字符（**只影响面板，不影响回灌给模型的结果**）。
# 上面那句"结果已截断"是给模型的，这一句是给人的，两件事，别混。
PANEL_RESULT_CHARS = 1500

STATE_FILE = "notes/STATE.md"

# 可以在同一轮里并发执行的工具（只读，互不影响）。
# 其余一切（下单、改止损、写文件、跑 shell）都严格串行 —— 避免两个 buy 互相踩踏，
# 也避免读到"另一个操作改了一半"的状态。
PARALLEL_SAFE_PREFIXES = ("market__", "exa__", "paper__get_")
PARALLEL_SAFE_EXACT = {"delegate", "fs_read", "fs_list"}


def is_parallel_safe(name: str) -> bool:
    if name in PARALLEL_SAFE_EXACT:
        return True
    return any(name.startswith(p) for p in PARALLEL_SAFE_PREFIXES)


def is_fetch_tool(name: str) -> bool:
    """「会往上下文里灌原始数据」的调用 —— 行情与搜索。

    只有这一类调用值得用运行时成本提醒去干预。读笔记、写文件、打权益点
    是每轮必做的琐务，派给子代理也没用；一旦把它们的次数也算进提醒阈值，
    提醒就会每轮都误报（实测：Agent 两轮就学会了无视它）。
    """
    return name.startswith("market__") or name.startswith("exa__")


class Agent:
    def __init__(self, cfg: Config, hub: MCPHub, local: LocalTools, llm: LLM, bus: EventBus,
                 wake=None) -> None:
        self.cfg = cfg
        self.hub = hub
        self.local = local
        self.llm = llm
        self.bus = bus
        # 唤醒策略/睡眠配额的持有者。工具 sleep / wake_up / set_wake_policy
        # 由它实现 —— 那三个工具改的是调度状态，不属于文件系统，所以不放 LocalTools。
        self.wake = wake
        self.subagent = SubAgent(cfg, hub, local, bus)
        # 叙事层：只读事件流、只吐事件流。见 narrator.py 顶部那三行注释。
        self.narrator = Narrator(cfg)

        self._lock = asyncio.Lock()
        self.beats = 0
        self.skipped = 0
        self.paused = False
        self.last_beat_at: str | None = None
        self.last_error: str | None = None
        self.tokens_used = 0
        self.pending: list[str] = []
        self._next_beat_ts: float | None = None
        self._last_trade_id = 0
        # 叙事层的游标：下次该从哪条事件开始取材。
        # 起点设成"当前 seq"是有意的 —— 否则重启后第一次叙事会把历史上
        # 所有事件都重写一遍。时间上的节奏由 narrator 自己从手账里恢复。
        self._last_narrated_id: int = bus.seq
        # 本轮心跳的成本计数（用于运行时提醒）
        self._beat_tool_calls = 0
        # 其中真正往上下文里灌数据的那些调用（行情／搜索）——提醒只看这个数
        self._beat_fetch_calls = 0
        self._beat_fetch_tools: Counter[str] = Counter()
        self._beat_fetch_symbols: set[str] = set()
        self._beat_delegates = 0
        self._nudged = False
        # 人格热加载：记住文件 mtime，人类改完立即生效
        self._persona_mtime: float = -1.0
        self._persona_cache: str = ""

    # ------------------------------------------------------------------ 人格
    def load_persona(self) -> str:
        """读 config/persona.md。按 mtime 缓存，所以人类改完不用重启。"""
        path = self.cfg.persona_file
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if self._persona_cache:
                self._persona_cache = ""
                self._persona_mtime = -1.0
            return ""
        if mtime == self._persona_mtime:
            return self._persona_cache
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
        except OSError as exc:
            log.warning("读取人格文件失败: %s", exc)
            return self._persona_cache
        self._persona_mtime = mtime
        self._persona_cache = text
        self.bus.emit("persona_loaded", chars=len(text), source=os.path.basename(path))
        log.info("人格文件已加载（%d 字符）", len(text))
        return text

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        return {
            "agent_name": self.cfg.agent_name,
            "paused": self.paused,
            "beats": self.beats,
            "skipped": self.skipped,
            "last_beat_at": self.last_beat_at,
            "next_beat_at": (
                time.strftime("%H:%M:%S", time.localtime(self._next_beat_ts))
                if self._next_beat_ts else None
            ),
            "next_beat_seconds": (
                max(0, int(self._next_beat_ts - time.time())) if self._next_beat_ts else None
            ),
            "last_error": self.last_error,
            "tokens_used": self.tokens_used,
            "llm_configured": self.cfg.llm_configured,
            "llm_model": self.cfg.llm_model or None,
            "enable_shell": self.cfg.enable_shell,
            "pending_instructions": len(self.pending),
            # 上一轮心跳的解剖：工具调用 / 其中取数 / 子代理委派 / 是否发过成本提醒
            "last_beat": {
                "tool_calls": self._beat_tool_calls,
                "fetch_calls": self._beat_fetch_calls,
                "fetch_tools": dict(self._beat_fetch_tools),
                "symbols_seen": sorted(self._beat_fetch_symbols),
                "delegates": self._beat_delegates,
                "nudged": self._nudged,
            },
            "mcp": self.hub.server_status(),
            "subagent": self.subagent.status(),
            "tool_count": (
                len(self.hub.tool_names())
                + len(self.local.schemas())
                + len(self.subagent.schemas())
            ),
            "persona": {
                "loaded": bool(self.load_persona()),
                "chars": len(self.load_persona()),
                "file": os.path.basename(self.cfg.persona_file),
            },
            "narrator": self.narrator.status(),
            "wake": self.wake.status() if self.wake else None,
        }

    def set_next_beat(self, ts: float | None) -> None:
        self._next_beat_ts = ts

    def instruct(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.pending.append(text)
            self.bus.emit("human_instruction", text=text)

    # ------------------------------------------------------------------ 上下文
    async def _safe_call(self, name: str, args: dict | None = None, fallback: Any = None) -> Any:
        try:
            raw = await self.hub.call(name, args or {})
            if not isinstance(raw, str):
                return raw
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                # 工具返回了空结果或非 JSON 文本，安静降级即可，不算错误
                return fallback
        except Exception as exc:
            log.warning("预取 %s 失败: %s", name, exc)
            return fallback

    async def build_context(self) -> str:
        account = await self._safe_call("paper__get_account", fallback={"error": "账本暂不可达"})
        events = await self._safe_call("paper__get_events", {"since_id": self._last_trade_id, "limit": 20}, fallback=[])
        if isinstance(events, list) and events:
            self._last_trade_id = max(self._last_trade_id, max(e.get("id", 0) for e in events))
            # 账本里出现了「不是它刚才下的单」—— 只可能是后台止损/止盈巡视线程干的。
            # 这类成交必须补成正式事件，否则：
            #   · 面板上只看到它事后在散文里提一句，看不到一笔真实的平仓
            #   · 叙事层收不到 trade 事件，那篇"我睡着时被打掉了"就得等定时窗口才写得出来
            # 而"睡着期间被止损"恰恰是最该立刻记下来的那种事。
            for e in events:
                if e.get("side") != "SELL":
                    continue
                self.bus.emit(
                    "trade", symbol=e.get("symbol"), side="SELL",
                    qty=e.get("qty"), price=e.get("price"), fill_price=e.get("price"),
                    notional=round((e.get("qty") or 0) * (e.get("price") or 0), 2),
                    fee=None, realized_pnl=e.get("realized_pnl"),
                    reason=e.get("reason"), auto=True,
                )

        notes_dir = os.path.join(self.cfg.workspace_dir, "notes")
        try:
            note_files = sorted(os.listdir(notes_dir))[:20] if os.path.isdir(notes_dir) else []
        except OSError:
            note_files = []

        state_md = ""
        state_path = os.path.join(self.cfg.workspace_dir, STATE_FILE)
        if os.path.isfile(state_path):
            try:
                with open(state_path, "r", encoding="utf-8", errors="replace") as f:
                    state_md = f.read(self.cfg.journal_excerpt_chars)
            except OSError:
                pass

        parts = [
            "## 当前账户\n```json\n" + json.dumps(account, ensure_ascii=False, indent=1)[:4000] + "\n```",
            "## 你睡着期间发生的成交\n```json\n"
            + json.dumps(events, ensure_ascii=False, indent=1)[:2000] + "\n```",
            "## 你的笔记目录\n" + (", ".join(note_files) if note_files else "(空，你还没写过任何笔记)"),
        ]
        if state_md.strip():
            parts.append(
                "## 你上次留下的 STATE.md（你的短期记忆）\n\n"
                + state_md
                + "\n\n（这段可能已经过时——请用最新行情与账本校验之后再相信它）"
            )
        parts.append(self._wake_block())
        return "\n\n".join(parts)

    def _wake_block(self) -> str:
        """把「这轮为什么醒」和睡眠读数摆在他眼前。

        这正是 README 里最值钱的那条经验：**静态提示词说服不了模型，
        把实时读数摆在它眼前才行**。睡眠配额是硬约束，但如果它自己不知道
        还欠多少小时，就会做出"再睡一会儿"这种跟系统硬碰硬的决定。
        """
        if self.wake is None:
            return ""
        trigger_names = {
            "schedule": "定时兜底（距上次唤醒已经隔了足够的间隔，不是有事发生）",
            "price_human": "人类设的价格阈值被触发了",
            "price_agent": "你自己睡前押的自唤醒阈值被触发了",
            "human_manual": "人类在面板上手动叫醒了你",
            "human_instruction": "人类给你留了话",
            "sleep_max": "单次睡眠到达上限，系统把你叫醒了",
            "startup": "进程刚重启，系统让你先醒一次核对状态",
            "schedule_first": "进程刚重启，走一次定时兜底",
        }
        lines = ["## 这一轮是怎么开始的"]
        trig = getattr(self, "_beat_trigger", "") or ""
        lines.append(f"- 唤醒来源：**{trigger_names.get(trig, trig or '未记录')}**")
        note = getattr(self, "_beat_wake_note", None)
        if note:
            lines.append(f"- 具体原因：{note}")

        try:
            sl = self.wake.sleep or {}
        except Exception:
            sl = {}
        if sl.get("sleeping"):
            hours = sl.get("sleep_hours_this_nap")
            if trig in ("price_agent", "price_human", "human_manual", "human_instruction"):
                lines.append(
                    f"- **你正处在睡眠中，是被叫醒的。**这一觉从 "
                    f"{sl.get('sleeping_since')} 开始，已经睡了 {hours} 小时。"
                    "处理完这一轮之后**你会自动回到睡眠**，除非你明确调用 `wake_up`。"
                )
            else:
                lines.append(f"- 你在睡眠中（已睡 {hours} 小时）。")
        else:
            lines.append(
                f"- 睡眠账本：滚动 24 小时已睡 {sl.get('slept_last_24h_hours')} 小时"
                f"（要求 {sl.get('required_hours')} 小时），还欠 **{sl.get('debt_hours')} 小时**；"
                f"已连续清醒 {sl.get('awake_span_hours')} 小时，"
                f"上限 {sl.get('max_awake_hours')} 小时。"
            )
            if (sl.get("debt_hours") or 0) > 0:
                lines.append(
                    "- 欠着睡眠的时候，连续清醒超过上限会被系统强制送回睡眠（不问你）。"
                    "盘面没什么可看的时候，把该设的止损设好、把自唤醒阈值押好，"
                    "然后去睡 —— 睡眠期间定时唤醒是静默的，不会白烧钱。"
                )

        eff = self.wake.effective()
        lines.append(
            f"- 兜底间隔 {eff['interval_seconds'] // 60} 分钟"
            f"（人类上界 {eff['human_interval_seconds'] // 60} 分钟）。"
            "它的语义是「最长沉默」：距上次唤醒超过这么久就必定醒一次，"
            "期间只要有任何别的唤醒发生，这次兜底就自然被跳过。"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ 执行工具
    def _cost_nudge(self, batch: list[dict], step: int) -> str:
        """运行时成本提醒。返回 '' 表示这次不该提醒，返回文本则会被贴到取数结果末尾。

        提醒只看**取数次数**，不看工具调用总数。理由来自实测日志：
        一轮心跳里 5~9 次调用中有 4~7 次是每轮必做的琐务（读笔记、列目录、写笔记、
        打权益点、看账本），它们既不该外包、也躲不掉。用总数当阈值的结果是
        **每轮都误报**，而 Agent 两轮就学会了无视它，还会把它误读成"工具调用配额快满了"。
        """
        if not self.subagent.enabled or self._nudged or self._beat_delegates > 0:
            return ""
        if self._beat_fetch_calls < self.cfg.cost_nudge_after:
            return ""
        # 本批里必须有取数调用，提醒才挨着引发它的那条结果
        if not any(is_fetch_tool(c["name"]) for c in batch):
            return ""
        if step >= self.cfg.llm_max_steps:
            return ""

        self._nudged = True
        detail = "、".join(f"{n} ×{c}" for n, c in self._beat_fetch_tools.most_common())
        missing = [s for s in self.cfg.watchlist if s not in self._beat_fetch_symbols]
        tail = (
            f"关注列表里 {'、'.join(missing)} 还没看：要看就一次派出去。"
            if missing else
            "关注列表已经全覆盖了 —— 如果接下来不再取数，直接收尾即可。"
        )
        text = (
            f"\n\n[成本提示] 本轮你已经自己取了 {self._beat_fetch_calls} 次行情／搜索"
            f"（{detail}），这些原始返回会在后面每一步里重复计费。{tail}\n"
            "接下来如果还要取数（更多标的、更多周期、或搜完还要逐条抓网页），"
            "请一次 `delegate` 派给子代理，让它在自己的上下文里跑完、只回你一段结论 ——"
            "别继续自己一个个调。"
        )
        self.bus.emit("cost_nudge", calls=self._beat_fetch_calls, text=text.strip(),
                      tools=dict(self._beat_fetch_tools), missing=missing)
        return text

    async def _dispatch(self, name: str, args: dict) -> str:
        if name == "delegate":
            return await self.subagent.call(args)
        if name in ("sleep", "wake_up", "set_wake_policy"):
            if self.wake is None:
                return "[唤醒模块未启用] 换不了 —— 当前配置下调度由固定心跳驱动。"
            return await self.wake.call_tool(name, args, self)
        if name in ("fs_list", "fs_read", "fs_write", "fs_append", "run_shell"):
            return await self.local.call(name, args)
        return await self.hub.call(name, args)

    async def _run_one(self, call: dict, step: int) -> str:
        """执行一次工具调用，并把过程和结果发到事件流。返回回灌给模型的文本。"""
        name, args, _cid = call["name"], call["args"], call["id"]
        if name == "delegate":
            self._beat_delegates += 1
        # 事件里这串 args 只给面板看（真参数走的是变量 args，不受影响）。
        # 截断要**说自己在截断** —— 不然面板上就是半句话，读到的人会以为
        # Agent 的参数本来就长这样。2026-09-27 加：那天正对着一条
        # `fs_write {"content": "# 交易与风控纪律框架…约 0.3` 断在半句上。
        evt_args = json.dumps(args, ensure_ascii=False)
        if len(evt_args) > self.cfg.tool_call_args_chars:
            evt_args = evt_args[: self.cfg.tool_call_args_chars] + "…（参数已截断）"
        self.bus.emit("tool_call", tool=name, args=evt_args, step=step)

        t0 = time.time()
        try:
            result = await self._dispatch(name, args)
        except Exception as exc:
            result = f"[工具执行失败] {type(exc).__name__}: {exc}"
        elapsed = round(time.time() - t0, 3)

        body = str(result)
        truncated = len(body) > self.cfg.max_tool_result_chars
        if truncated:
            body = body[: self.cfg.max_tool_result_chars] + "\n…（结果已截断）"

        # 面板上看的是这一份：截到 PANEL_RESULT_CHARS，并且**说清**它被截过 ——
        # 不然读的人分不清"结果就这么短"和"面板只留了这么多"。
        # （原来这里是 `body[:1500]`，硬编码，而且截了不出声；
        #   结果一长，上面那句"结果已截断"自己也被截掉了。）
        shown = (body if len(body) <= PANEL_RESULT_CHARS
                 else body[:PANEL_RESULT_CHARS] + "\n…（面板只留前 1500 字符，完整结果已回灌给模型）")

        self.bus.emit(
            "tool_result", tool=name, result=shown,
            truncated=truncated, elapsed=elapsed, ok=not body.startswith("[工具"),
        )

        # 取数记账：只有行情／搜索的返回才会真的灌进上下文，
        # 覆盖情况按「返回体里出现了哪些关注标的」来判定 ——
        # 这样不依赖每个工具的入参形状（overview 这类批量工具一把全覆盖）。
        if is_fetch_tool(name):
            self._beat_fetch_calls += 1
            self._beat_fetch_tools[name] += 1
            for sym in self.cfg.watchlist:
                if sym and sym in body:
                    self._beat_fetch_symbols.add(sym)

        if name in ("paper__buy", "paper__sell", "paper__close_all"):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("filled"):
                    self.bus.emit(
                        "trade", symbol=parsed.get("symbol"), side=parsed.get("side"),
                        qty=parsed.get("qty"), price=parsed.get("fill_price"),
                        notional=parsed.get("notional"), fee=parsed.get("fee"),
                        realized_pnl=parsed.get("realized_pnl"), reason=parsed.get("reason"),
                    )
                elif isinstance(parsed, dict) and parsed.get("rejection_reason"):
                    self.bus.emit("trade_rejected", tool=name,
                                  reason=parsed.get("rejection_reason"))
            except (json.JSONDecodeError, TypeError):
                pass

        return body

    # ------------------------------------------------------------------ 心跳
    async def run_beat(self, trigger: str = "schedule", wake_note: str | None = None,
                       extra_instruction: str | None = None) -> None:
        if self._lock.locked():
            self.bus.emit("skipped", reason="上一轮心跳尚未结束")
            self.skipped += 1
            return
        async with self._lock:
            await self._beat(trigger, extra_instruction, wake_note)

    async def _beat(self, trigger: str, extra_instruction: str | None,
                    wake_note: str | None = None) -> None:
        started = time.time()
        self.beats += 1
        self.last_beat_at = time.strftime("%Y-%m-%d %H:%M:%S")
        # "这轮是谁把我叫醒的" 是评估唤醒策略唯一的一手依据，必须进事件流
        self._beat_trigger = trigger
        self._beat_wake_note = wake_note
        # 每轮重置成本计数器
        self._beat_tool_calls = 0
        self._beat_fetch_calls = 0
        self._beat_fetch_tools = Counter()
        self._beat_fetch_symbols = set()
        self._beat_delegates = 0
        self._nudged = False
        self.bus.emit("beat_start", trigger=trigger, beat=self.beats,
                      wake_note=wake_note)

        if not self.cfg.llm_configured:
            self.last_error = "模型未配置"
            self.bus.emit("error", message="模型未配置：请在 .env 填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
            self.bus.emit("beat_end", ok=False, duration=0)
            return

        try:
            snapshot = await self.build_context()
        except Exception as exc:
            snapshot = f"（构建上下文失败：{exc}）"

        pending_blocks = []
        # 人类留言的来源只有一个：instruct() 会同时 (a) 塞进 self.pending
        # 给这一轮的提示词用，(b) 往事件流发一条 human_instruction。
        # 所以叙事层不需要额外记一份 —— 它读事件流就够了。
        # （之前这里额外记了一份给叙事层，结果是同一条留言被算了两遍。）
        if extra_instruction:
            pending_blocks.append(f"## 人类刚刚给你的指令（优先级最高）\n{extra_instruction}")
        while self.pending:
            pending_blocks.append(f"## 人类留言\n{self.pending.pop(0)}")

        user_msg = beat_context_template().format(
            snapshot=snapshot,
            wake=self._wake_block(),
            pending=("\\n\\n".join(pending_blocks) + "\\n\\n") if pending_blocks else "",
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(self.cfg, self.load_persona())},
            {"role": "user", "content": user_msg},
        ]
        tools = (self.local.schemas() + self.subagent.schemas()
                 + (self.wake.schemas() if self.wake else []) + self.hub.openai_tools())
        self.subagent.reset_beat()

        ok = True
        final_text = ""
        try:
            for step in range(1, self.cfg.llm_max_steps + 1):
                reply = await self.llm.chat(messages, tools)
                if reply.get("usage"):
                    self.tokens_used += reply["usage"].get("total_tokens") or 0

                text = (reply["content"] or "").strip()
                tool_calls = reply["tool_calls"]

                # 思考链单独成一条事件：面板上可以单独过滤，也不污染最终结论
                reasoning = (reply.get("reasoning") or "").strip()
                if reasoning:
                    self.bus.emit("thinking", text=reasoning[:6000], step=step)

                if text:
                    final_text = text
                    self.bus.emit("agent_text", text=text, step=step)

                if not tool_calls:
                    break

                assistant_msg: dict[str, Any] = {"role": "assistant", "content": reply["content"] or ""}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls
                ]
                # 思考型模型（DeepSeek 系等）要求把思维链原样带回，否则下一次请求被拒。
                # 非思考型模型这里为空，不会污染请求体。
                if reply.get("reasoning"):
                    assistant_msg["reasoning_content"] = reply["reasoning"]
                messages.append(assistant_msg)

                # 这一轮的调用分两批：只读的并发跑，动账本的严格串行。
                # 并发只对无副作用的工具有意义（行情／搜索／子代理），
                # 下单和改止损必须保序，否则两个 buy 会互相踩踏。
                batch: list[dict] = []
                for tc in tool_calls:
                    raw_args = tc["arguments"]
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                        if not isinstance(args, dict):
                            args = {}
                    except json.JSONDecodeError:
                        args = {}
                    batch.append({"id": tc["id"], "name": tc["name"], "args": args})

                parallel = [c for c in batch if is_parallel_safe(c["name"])]
                serial = [c for c in batch if not is_parallel_safe(c["name"])]

                results: dict[str, str] = {}
                if parallel:
                    if len(parallel) > 1:
                        self.bus.emit(
                            "parallel_batch", step=step,
                            tools=[c["name"] for c in parallel],
                            models=[c["args"].get("model") for c in parallel if c["name"] == "delegate"] or None,
                        )
                    settled = await asyncio.gather(
                        *(self._run_one(c, step) for c in parallel), return_exceptions=True
                    )
                    for call, res in zip(parallel, settled):
                        if isinstance(res, BaseException):
                            results[call["id"]] = f"[工具执行失败] {type(res).__name__}: {res}"
                        else:
                            results[call["id"]] = res
                for call in serial:
                    results[call["id"]] = await self._run_one(call, step)

                # 回灌顺序必须与模型发出的顺序一致（tool_call_id 要一一对应）
                #
                # 这里顺手做一个「运行时成本提醒」：静态提示词说服不了一个
                # 正在顺手的模型，但把「你已经自己取了几次数」这个数字摆在它眼前可以。
                # 做法是往工具结果末尾追加一行，不新增消息 —— 零兼容风险。
                self._beat_tool_calls += len(batch)
                nudge = self._cost_nudge(batch, step)

                for call in batch:
                    content = results.get(call["id"], "[内部错误] 结果丢失")
                    # 提醒只贴在「引发它的那条取数结果」末尾；粘在写笔记、打权益点
                    # 这类琐务结果上会让它看起来毫无来由 —— Agent 直接忽略。
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": content + (nudge if (nudge and is_fetch_tool(call["name"])) else ""),
                    })
            else:
                self.bus.emit("error", message=f"达到单轮最大步数 {self.cfg.llm_max_steps}，已强制收尾")

        except LLMError as exc:
            ok = False
            self.last_error = str(exc)
            self.bus.emit("error", message=str(exc))
        except Exception as exc:
            ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.bus.emit("error", message=self.last_error)
            log.exception("心跳异常")
        else:
            self.last_error = None

        # 打一个权益采样点，让面板曲线连续
        try:
            await self.hub.call("paper__snapshot_equity")
        except Exception:
            pass

        duration = round(time.time() - started, 1)
        if final_text:
            self.bus.emit("beat_summary", text=final_text[:4000], duration=duration, ok=ok)
        self.bus.emit("beat_end", ok=ok, duration=duration, beat=self.beats, steps=None)

        # 叙事层留到最后：操盘已经收尾，所以它的耗时不计进 duration，
        # 也不可能影响任何已经做出的判断。
        await self._narrate()

    # ------------------------------------------------------------------ 叙事层
    async def _narrate(self) -> None:
        """叙事层在本项目里**唯一**的调用点。

        返回值直接交给 bus.emit，然后就没有然后了 —— emit 只写事件缓冲和 JSONL，
        代码里没有任何把事件 append 回 messages 的路径。

        换句话说：只要这三行不改，叙事就永远进不了操盘上下文。

        取材范围是「自上次叙事以来的全部事件」，而不是「本轮」。
        无事发生时叙事可以攒很久才写（见 NARRATOR_MAX_GAP），
        攒下来的这一整段会一起交给叙事层 —— 所以它写出来的是回顾，不是流水账。
        """
        if not self.narrator.enabled:
            return
        try:
            since = self._last_narrated_id
            events = [e for e in self.bus.history(4000) if e.get("id", 0) > since]
            if not events:
                return

            beats = sum(1 for e in events if e.get("kind") == "beat_start") or 1
            if not self.narrator.should_narrate(events, beats):
                return

            result = await self.narrator.narrate(events, self.beats)
            if result:
                self._last_narrated_id = max(e.get("id", 0) for e in events)
                self.bus.emit("narrative", **result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("叙事层调用点异常：%s: %s", type(exc).__name__, exc)
