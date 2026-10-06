"""子代理池：把「上下文重、判断轻」的活儿外包给便宜模型。

为什么需要它
------------
主 Agent 的每一轮心跳都要把「系统提示词 + 全部工具定义 + 所有历史消息」重发一遍，
所以它每多看一条数据、每多调一次工具，成本都是滚雪球式的（平方级）。
而它值钱的地方在于**判断**，不在于**搬运数据**。

子代理在各自的独立上下文里把脏活干完（「扫 20 个币找异动」「把这段 300 根 K 线
读成一句结论」），主 Agent 只收到一小段浓缩结论 —— 主上下文完全不膨胀。

模型白名单
----------
用哪些模型由**人类**在 config/subagent_models.json 里决定，主 Agent 只能从中挑选，
且每个模型的 use_for / avoid_for 会写进工具描述里，让它是"看懂差别后选择"而非瞎猜。
JSON schema 里用 enum 锁死候选，模型幻觉出一个白名单外的名字会被直接拒绝。

并发
----
主 Agent 可以在同一轮里同时发出最多 SUBAGENT_MAX_CONCURRENCY 个 delegate 调用，
它们并行执行。信号量在这里兜底，即便上层调度变了也不会超发。

安全边界（刻意收窄）
--------------------
子代理只拿到**只读**工具：行情（market__*）+ 读文件（fs_read / fs_list）。
它不能下单、不能写文件、不能执行 shell、也不能再派子代理（禁止递归）。
换句话说，它最多只能"看"，一切"动手"都留在主 Agent 手里。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .config import Config
from .events import EventBus
from .mcp_hub import MCPHub
from .tools_local import LocalTools
from .util import flatten_content

log = logging.getLogger("trader.subagent")

# 子代理能用什么：行情（只读）+ 读文件。其余一律拒绝。
ALLOWED_MCP_PREFIXES = ("market__",)
ALLOWED_LOCAL = ("fs_read", "fs_list")

# 写进 delegate 工具描述里的那份「模型目录」，每个字段最多留多少字。
#
# 为什么要截断：delegate 是所有工具里最肥的一个，而它的定义**每一步 LLM 调用都要重发**。
# 目录的原文来自 config/subagent_models.json（人类维护），里面的 use_for / avoid_for
# 写得很细 —— 那些细节是给**人类**看的，主 Agent 挑模型只需要知道每条的头一句。
# 经验是：人类写这种说明会把最重要的结论放在最前面（「最快最省的批量活儿」「⛔ 不许
# 用于任何文字类产出」），所以截断保住了要害，丢掉的是举例。
#
# 改这个数之前先想清楚：调到很小会让"按描述挑模型"退化成瞎猜。人类的写法约定是
# **重要的说在前面**，而完整原文在面板上（SubAgent.status() 里给了全文）。
CATALOG_FIELD_CAP = 48
# 面板上一条 subagent_end 最多显示多少字符（**只影响面板和复盘，不影响回灌给模型的结论**）。
# 原来这里是写死的 `answer[:1200]`，硬截而且不出声 —— 线上 9 条里最长那条正好 1200。
PANEL_ANSWER_CHARS = 1200

SUBAGENT_SYSTEM = """你是一个被上级交易 Agent 临时委派的**研究子代理**。

你在一台隔离服务器上运行，可以调用只读行情工具（`market__*`）和读文件工具
（`fs_read` / `fs_list`）。你**不能**下单、不能写文件、不能执行命令 —— 这些不在你权限内，
也不要尝试。

你的唯一职责是：**高质量地完成被交付的具体任务，并返回一份紧凑的结论。**

工作方式：
1. 先想清楚需要哪些数据，然后**一次性**把需要的工具调完（同一轮里可以并发发多个调用），
   不要反复试探、不要一个一个试。
2. 该算的数（均线、波动率、涨跌幅、排名、相关性）自己算准，不要含糊其辞。
3. 涉及数字的判断必须给出**具体数值**，禁止"略有上涨""表现较好"这种废话。
4. 如果任务描述里已经直接给了数据，就直接分析，不要再去调工具取一遍。
5. 如果发现**任务前提本身有问题**（例如判定标准在当前市况下失效、要求的指标
   在当前数据里根本不存在），**主动指出来** —— 这比硬着头皮交一份排名有价值得多。

输出要求（非常关键）：
- 用中文回答，**总长度控制在 600 字以内**。
- 用短句和紧凑的列表，不要写前言、不要写"好的我来分析"、不要重复任务描述。
- 直接给结论：筛出来的结果、关键数字、以及**为什么**。
- 前提有问题的，把风险提示写在结论里。
- 如果任务无法完成（数据取不到、条件互相矛盾），一句话说清原因即可。

你的上级只看得到你最终那一段话，看不到你中间调了什么工具。所以
**结论必须自包含**：重要的数字要写进结论里，否则等于没查。"""


@dataclass
class ModelEntry:
    """白名单里的一个模型。

    密钥【不】写在本文件里，只写它所在的环境变量名（api_key_env）。
    真正的 key 留在 .env（600，root）中，由 compose 注入成 brain 的进程环境变量。
    这样白名单文件可以公开可读，而 Agent 的 run_shell（清空了环境变量）拿不到 key。
    """
    id: str
    label: str = ""
    base_url: str = ""
    api_key_env: str = ""
    max_tokens: int = 8192
    temperature: float = 0.2
    use_for: str = ""
    avoid_for: str = ""

    def resolved_key(self, cfg: Config) -> str:
        """密钥回落链：api_key_env 指定的环境变量 > SUBAGENT_API_KEY > LLM_API_KEY。

        最后一级让「主模型同端点」的模型可以直接复用主 key，不用额外配置。
        """
        if self.api_key_env:
            val = (os.environ.get(self.api_key_env) or "").strip()
            if val:
                return val
            log.warning("模型 %s 指定的环境变量 %s 为空或不存在", self.id, self.api_key_env)
        if cfg.subagent_api_key.strip():
            return cfg.subagent_api_key.strip()
        if self.base_url.rstrip("/") == cfg.llm_base_url.rstrip("/"):
            return cfg.llm_api_key.strip()
        return ""

    def key_source(self, cfg: Config) -> str:
        """给日志/状态用：key 是从哪儿来的（绝不回显 key 本身）。"""
        if self.api_key_env and (os.environ.get(self.api_key_env) or "").strip():
            return f"env:{self.api_key_env}"
        if cfg.subagent_api_key.strip():
            return "env:SUBAGENT_API_KEY"
        if self.base_url.rstrip("/") == cfg.llm_base_url.rstrip("/") and cfg.llm_api_key.strip():
            return "env:LLM_API_KEY"
        return "缺失"


@dataclass
class SubAgentStats:
    active: int = 0
    calls: int = 0
    errors: int = 0
    tokens_used: int = 0
    tokens_by_model: dict[str, int] = field(default_factory=dict)
    calls_by_model: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None
    tools_supported: bool = True


class SubAgent:
    def __init__(self, cfg: Config, hub: MCPHub, local: LocalTools, bus: EventBus) -> None:
        self.cfg = cfg
        self.hub = hub
        self.local = local
        self.bus = bus

        self.stats = SubAgentStats()
        self._sem = asyncio.Semaphore(max(1, cfg.subagent_max_concurrency))
        self._clients: dict[str, AsyncOpenAI] = {}
        self._beat_calls = 0
        self._seq = 0

        self._models: list[ModelEntry] = []
        self._default_id: str = ""
        self._models_mtime: float = -1.0

    # ================================================================== 白名单
    def refresh_models(self, force: bool = False) -> list[ModelEntry]:
        """读取白名单。按 mtime 缓存，所以人类改完 JSON 立即可见，不用重启。"""
        path = pathlib.Path(self.cfg.subagent_models_file)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            if self._models and not force:
                return self._models
            self._models, self._default_id = self._from_env_fallback()
            return self._models

        if not force and mtime == self._models_mtime and self._models:
            return self._models

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("子代理白名单解析失败 %s: %s", path, exc)
            self.bus.emit("subagent_models_error", error=f"{type(exc).__name__}: {exc}", path=str(path))
            self._models_mtime = mtime
            return self._models

        entries: list[ModelEntry] = []
        for raw in (data.get("models") or []):
            if not isinstance(raw, dict):
                continue
            mid = str(raw.get("id") or "").strip()
            if not mid:
                continue
            if raw.get("api_key"):
                # 明文密钥写在白名单里会被 Agent 的 shell 读到（config 是公开可读的）
                log.error("模型 %s 里写了明文 api_key —— 这会被 Agent 读到。"
                          "请改成密钥的环境变量名 api_key_env，并把 key 放进 .env。", mid)
                self.bus.emit("subagent_models_error",
                              error=f"模型 {mid} 使用了明文 api_key，已忽略。请改用 api_key_env。",
                              path=str(path))
            base = str(raw.get("base_url") or self.cfg.llm_base_url).strip()
            entries.append(ModelEntry(
                id=mid,
                label=str(raw.get("label") or mid).strip(),
                base_url=base,
                api_key_env=str(raw.get("api_key_env") or "").strip(),
                max_tokens=int(raw.get("max_tokens") or self.cfg.subagent_max_tokens),
                temperature=float(raw.get("temperature", self.cfg.subagent_temperature)),
                use_for=str(raw.get("use_for") or "").strip(),
                avoid_for=str(raw.get("avoid_for") or "").strip(),
            ))

        self._models = entries
        wanted = str(data.get("default") or "").strip()
        self._default_id = wanted if any(m.id == wanted for m in entries) else (entries[0].id if entries else "")
        self._models_mtime = mtime
        log.info("子代理白名单已加载：%d 个模型，默认 %s", len(entries), self._default_id or "(无)")
        return self._models

    def _from_env_fallback(self) -> tuple[list[ModelEntry], str]:
        """兼容老的单模型环境变量写法（SUBAGENT_BASE_URL / _MODEL）。"""
        base = self.cfg.subagent_base_url.strip()
        mid = self.cfg.subagent_model.strip()
        if not base or not mid:
            return [], ""
        if any(m in (base + mid).lower() for m in ("placeholder", "replace_me", "your-")):
            return [], ""
        entry = ModelEntry(
            id=mid, label=mid, base_url=base,
            api_key_env="SUBAGENT_API_KEY",
            max_tokens=self.cfg.subagent_max_tokens,
            temperature=self.cfg.subagent_temperature,
            use_for="（由环境变量配置，未提供擅长说明）",
        )
        return [entry], mid

    def find(self, model_id: str | None) -> ModelEntry | None:
        models = self.refresh_models()
        if not models:
            return None
        if model_id:
            for m in models:
                if m.id == model_id:
                    return m
            return None  # 明确指定了但不在白名单 —— 调用方应拒绝，而不是悄悄换成别的
        for m in models:
            if m.id == self._default_id:
                return m
        return models[0]

    @property
    def enabled(self) -> bool:
        return bool(self.refresh_models())

    # ================================================================== 状态
    def status(self) -> dict[str, Any]:
        models = self.refresh_models()
        s = self.stats
        return {
            "enabled": bool(models),
            "default_model": self._default_id or None,
            "max_concurrency": self.cfg.subagent_max_concurrency,
            "max_per_beat": self.cfg.subagent_max_per_beat,
            "active": s.active,
            "calls": s.calls,
            "per_beat_used": self._beat_calls,
            "tokens_used": s.tokens_used,
            "tokens_by_model": dict(s.tokens_by_model),
            "errors": s.errors,
            "last_error": s.last_error,
            "tools_supported": s.tools_supported,
            "models": [
                {
                    "id": m.id, "label": m.label,
                    "use_for": m.use_for, "avoid_for": m.avoid_for,
                    "key_source": m.key_source(self.cfg),
                }
                for m in models
            ],
        }

    def reset_beat(self) -> None:
        """心跳开始时重置单轮配额。"""
        self._beat_calls = 0

    # ================================================================== 工具表
    def schemas(self) -> list[dict]:
        """只暴露一个入口工具。子代理内部用什么工具，主 Agent 不需要操心。

        这里每个字都要重发，而且一步一发 —— 所以描述只留三件事：什么时候用（判据）、
        什么时候别用（边界）、去哪挑模型。**"为什么值得"那一段故意不在这里说**：
        base_prompt 里已经说了一遍，工具描述里再说一遍就是为同一句话付两遍钱。
        """
        models = self.refresh_models()
        if not models:
            return []

        ids = [m.id for m in models]
        catalog = "\n".join(f"- {self._catalog_line(m)}" for m in models)

        return [{
            "type": "function",
            "function": {
                "name": "delegate",
                "description": (
                    "把一项**具体、自包含**的任务交给更便宜的子代理，它只带回一份紧凑结论。\n\n"
                    "**预判某个子问题要 ≥3 次工具调用才能回答时用它**（例如同时看 3 个以上标的、"
                    "读大段 K 线找形态、搜索后逐条抓网页）；1~2 次调用能拿到答案的自己调更快，"
                    "因为你自己调的每一次返回都会在后续每一步里重复计费。\n\n"
                    "**绝不外包**：开仓／平仓的最终决定。**也别派**：要写文件、跑命令的事 ——"
                    "它没有这些权限。\n\n"
                    f"**可以并行**：同一轮里可以同时发多个（同时最多 {self.cfg.subagent_max_concurrency} 个），"
                    "互不依赖的方向一次发完，别排队等。\n\n"
                    f"可选模型（只能从这些里挑）：\n{catalog}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "任务描述，具体到能独立执行：考察对象、时间周期与指标、判定标准。"
                                "要它参考的数据直接写进来。"
                            ),
                        },
                        "want": {
                            "type": "string",
                            "description": (
                                "要它返回什么，格式写清楚。例如「按异动排序的前 3 名，"
                                "每个给代码、关键数值、一句理由，总长 400 字内」。"
                            ),
                        },
                        "model": {
                            "type": "string",
                            "enum": ids,
                            "description": (
                                f"按任务性质挑（上面列了各自的适合／不适合）。"
                                f"不填则用默认的 {self._default_id}。"
                            ),
                        },
                    },
                    "required": ["task", "want"],
                },
            },
        }]

    @staticmethod
    def _catalog_line(m: ModelEntry) -> str:
        """白名单里的一条 → 描述里的一行。字段按 CATALOG_FIELD_CAP 截断。"""
        def clip(s: str) -> str:
            s = " ".join(str(s or "").split())
            return s if len(s) <= CATALOG_FIELD_CAP else s[:CATALOG_FIELD_CAP] + "…"

        bits = [f"`{m.id}`"]
        if m.use_for:
            bits.append(f"适合：{clip(m.use_for)}")
        if m.avoid_for and m.avoid_for not in ("无", "-", "none"):
            bits.append(f"不适合：{clip(m.avoid_for)}")
        return "；".join(bits)

    def _subagent_tools(self) -> list[dict]:
        """子代理可用的工具：行情全部 + 只读文件。刻意不含任何有副作用的工具。"""
        tools: list[dict] = []
        for t in self.hub.openai_tools():
            name = t.get("function", {}).get("name", "")
            if any(name.startswith(p) for p in ALLOWED_MCP_PREFIXES):
                tools.append(t)
        for t in self.local.schemas():
            if t.get("function", {}).get("name") in ALLOWED_LOCAL:
                tools.append(t)
        return tools

    async def _run_tool(self, name: str, args: dict) -> str:
        # 即便模型幻觉出一个越权工具名，这里也会挡回去
        if name in ALLOWED_LOCAL:
            return await self.local.call(name, args)
        if any(name.startswith(p) for p in ALLOWED_MCP_PREFIXES):
            return await self.hub.call(name, args)
        return f"[被拒绝] 子代理无权调用 {name}（只允许只读行情与读文件）"

    # ================================================================== 主流程
    def _client(self, entry: ModelEntry) -> AsyncOpenAI:
        key = f"{entry.base_url}|{entry.resolved_key(self.cfg)}"
        if key not in self._clients:
            self._clients[key] = AsyncOpenAI(
                base_url=entry.base_url,
                api_key=entry.resolved_key(self.cfg) or "EMPTY",
                timeout=self.cfg.subagent_timeout,
                max_retries=1,
            )
        return self._clients[key]

    async def call(self, args: dict) -> str:
        task = str(args.get("task") or "").strip()
        want = str(args.get("want") or "").strip()
        requested = str(args.get("model") or "").strip() or None

        if not task:
            return "[参数错误] task 不能为空"

        models = self.refresh_models()
        if not models:
            return (
                "[未启用] 目前没有可用的子代理模型（白名单为空或配置有误）。"
                "这项任务请你亲自完成，或让人类去 config/subagent_models.json 里加模型。"
            )

        entry = self.find(requested)
        if entry is None:
            avail = ", ".join(m.id for m in models)
            # 明确拒绝而不是悄悄换模型 —— 这样主 Agent 能学到白名单的边界
            return (
                f"[被拒绝] 模型 `{requested}` 不在允许清单里。可选：{avail}。"
                "请从中挑一个重试（或留空使用默认模型）。"
            )

        if not entry.base_url:
            return f"[配置错误] 模型 {entry.id} 没有 base_url，请让人类补齐白名单。"
        if not entry.resolved_key(self.cfg):
            hint = f"（条目指定的环境变量 {entry.api_key_env} 里没有值）" if entry.api_key_env else \
                   "（条目没写 api_key_env，且 SUBAGENT_API_KEY / LLM_API_KEY 都不可用）"
            return (
                f"[配置错误] 模型 {entry.id} 拿不到 API key {hint}。"
                "请让人类把密钥写进 .env 并重启 brain。"
            )

        if self._beat_calls >= self.cfg.subagent_max_per_beat:
            return (
                f"[配额用尽] 本轮心跳已经派发了 {self._beat_calls} 次子代理任务"
                f"（上限 {self.cfg.subagent_max_per_beat}）。"
                "剩下的活请你自己干，或者留到下一轮。"
            )

        self._seq += 1
        job = f"d{self._seq}"
        self._beat_calls += 1
        self.stats.calls += 1
        self.stats.calls_by_model[entry.id] = self.stats.calls_by_model.get(entry.id, 0) + 1

        queued = self.stats.active
        t0 = time.time()
        self.bus.emit(
            "subagent_start", job=job, model=entry.id, model_label=entry.label,
            task=task[:600], want=want[:300],
            queued=queued, active=self.stats.active + 1,
        )

        async with self._sem:
            self.stats.active += 1
            try:
                answer = await self._run(entry, task, want, job)
                self.stats.last_error = None
                ok = True
                err = None
            except Exception as exc:
                self.stats.errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("子代理[%s]执行失败: %s", job, exc)
                ok = False
                err = self.stats.last_error
                answer = (
                    f"[子代理失败] {err}\n"
                    "这项任务请你亲自完成，或稍后重试；不要反复重试同一个调用。"
                )
            finally:
                self.stats.active -= 1

        duration = round(time.time() - t0, 1)
        cap = self.cfg.subagent_max_result_chars
        # 先记下来"给模型的那份被截了没有" —— 下一行会把 answer 改掉，
        # 改完再判断就问不出原来的事实了。
        cut_for_model = len(answer) > cap
        if cut_for_model:
            answer = answer[:cap] + "\n…（子代理结论已截断）"

        # 事件里那份还要再小一档（面板用）。**截了要说** —— 不然读的人分不清
        # "子代理就说了这么多"和"面板只留了这么多"。原来这里是 `answer[:1200]`，
        # 硬截且不出声；线上 9 条 subagent_end 里最长那条正好 1200，就是它。
        panel_cut = len(answer) > PANEL_ANSWER_CHARS
        shown = (answer[:PANEL_ANSWER_CHARS] + "\n…（面板只留前 1200 字符，完整结论已回灌给模型）"
                 if panel_cut else answer)

        self.bus.emit(
            "subagent_end", job=job, model=entry.id, ok=ok, error=err,
            duration=duration, answer=shown,
            truncated=bool(cut_for_model or panel_cut),
        )
        log.info("子代理[%s/%s]%s，耗时 %ss，返回 %d 字符",
                 job, entry.id, "完成" if ok else "失败", duration, len(answer))
        return answer

    async def _run(self, entry: ModelEntry, task: str, want: str, job: str) -> str:
        tools = self._subagent_tools() if self.stats.tools_supported else []
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SUBAGENT_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"# 任务\n{task}\n\n"
                    f"# 需要你返回什么\n{want or '给出你的结论与关键数字。'}\n\n"
                    "现在开始。需要数据就用工具取（可以并发发多个调用），取完直接给结论。"
                ),
            },
        ]

        answer = ""
        for step in range(1, self.cfg.subagent_max_steps + 1):
            reply = await self._chat(entry, messages, tools, job)
            used = (reply.get("usage") or {}).get("total_tokens") or 0
            self.stats.tokens_used += used
            self.stats.tokens_by_model[entry.id] = self.stats.tokens_by_model.get(entry.id, 0) + used

            text = (reply.get("content") or "").strip()
            tool_calls = reply["tool_calls"]
            if text:
                answer = text
            if not tool_calls:
                break

            messages.append({
                "role": "assistant",
                "content": reply.get("content") or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls
                ],
                # DeepSeek 思考模式要求把 reasoning_content 原样带回，
                # 否则下一次请求会被拒。非思考型模型这里是空串，不会带上。
                **({"reasoning_content": reply["reasoning"]} if reply.get("reasoning") else {}),
            })

            # 子代理自己的工具调用也并发执行（只读，无副作用）
            async def one(tc: dict) -> tuple[str, str]:
                name = tc["name"]
                try:
                    targs = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else (tc["arguments"] or {})
                    if not isinstance(targs, dict):
                        targs = {}
                except json.JSONDecodeError:
                    targs = {}
                self.bus.emit("subagent_tool", job=job, model=entry.id, tool=name,
                              args=json.dumps(targs, ensure_ascii=False)[:200])
                try:
                    res = await self._run_tool(name, targs)
                except Exception as exc:
                    res = f"[工具执行失败] {type(exc).__name__}: {exc}"
                body = str(res)
                if len(body) > self.cfg.max_tool_result_chars:
                    body = body[: self.cfg.max_tool_result_chars] + "\n…（截断）"
                return tc["id"], body

            if len(tool_calls) == 1:
                cid, body = await one(tool_calls[0])
                messages.append({"role": "tool", "tool_call_id": cid, "content": body})
            else:
                for cid, body in await asyncio.gather(*(one(tc) for tc in tool_calls)):
                    messages.append({"role": "tool", "tool_call_id": cid, "content": body})
        else:
            answer = answer or f"（达到子代理最大步数 {self.cfg.subagent_max_steps}，未收敛）"

        return answer or "(子代理没有给出内容)"

    async def _chat(self, entry: ModelEntry, messages: list[dict], tools: list[dict],
                    job: str) -> dict[str, Any]:
        """调用子代理模型。若该端点不支持 tools，自动降级为纯文本模式。"""
        kwargs: dict[str, Any] = {
            "model": entry.id,
            "messages": messages,
            "temperature": entry.temperature,
            "max_tokens": entry.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        client = self._client(entry)
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            msg = str(exc)
            if tools and "tool" in msg.lower() and ("support" in msg.lower() or "unsupported" in msg.lower()):
                self.stats.tools_supported = False
                log.warning("子代理端点不支持 tool calling，降级为纯文本模式：%s", msg[:200])
                self.bus.emit("subagent_tool", job=job, model=entry.id, tool="(降级)",
                              args="端点不支持工具调用，改为纯文本模式")
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                resp = await client.chat.completions.create(**kwargs)
            else:
                raise

        msg = resp.choices[0].message
        usage = getattr(resp, "usage", None)

        # 思考型模型（DeepSeek-R 系、Gemini Thinking 等）会把思维链放在非标准字段里。
        # 关键：带 tool_calls 回传时，某些厂商（DeepSeek）【要求把这个字段原样带回】，
        # 否则报 400 "reasoning_content must be passed back"。
        extra = getattr(msg, "model_extra", None) or {}
        reasoning = ""
        for key in ("reasoning_content", "reasoning", "thinking"):
            val = extra.get(key)
            if isinstance(val, str) and val.strip():
                reasoning = val
                break

        return {
            "content": flatten_content(msg.content),
            "reasoning": reasoning,
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}
                for tc in (msg.tool_calls or [])
            ],
            "usage": {"total_tokens": getattr(usage, "total_tokens", None)} if usage else None,
        }
