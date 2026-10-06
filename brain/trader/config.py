"""集中读取环境变量。所有配置都来自 .env，代码里不写任何默认密钥。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _s(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _b(key: str, default: bool = False) -> bool:
    return _s(key, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # --- 模型 ---
    llm_base_url: str = field(default_factory=lambda: _s("LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=lambda: _s("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _s("LLM_MODEL"))
    llm_temperature: float = field(default_factory=lambda: _f("LLM_TEMPERATURE", 0.3))
    llm_max_tokens: int = field(default_factory=lambda: _i("LLM_MAX_TOKENS", 4096))
    llm_max_steps: int = field(default_factory=lambda: _i("LLM_MAX_STEPS_PER_BEAT", 18))
    llm_timeout: float = field(default_factory=lambda: _f("LLM_TIMEOUT", 180))

    # --- 子代理（把上下文重的活儿外包给便宜模型）---
    subagent_base_url: str = field(default_factory=lambda: _s("SUBAGENT_BASE_URL"))
    subagent_api_key: str = field(default_factory=lambda: _s("SUBAGENT_API_KEY"))
    subagent_model: str = field(default_factory=lambda: _s("SUBAGENT_MODEL"))
    subagent_temperature: float = field(default_factory=lambda: _f("SUBAGENT_TEMPERATURE", 0.2))
    subagent_max_tokens: int = field(default_factory=lambda: _i("SUBAGENT_MAX_TOKENS", 8192))
    subagent_max_steps: int = field(default_factory=lambda: _i("SUBAGENT_MAX_STEPS", 8))
    subagent_timeout: float = field(default_factory=lambda: _f("SUBAGENT_TIMEOUT", 120))
    subagent_max_result_chars: int = field(default_factory=lambda: _i("SUBAGENT_MAX_RESULT_CHARS", 1500))
    # 模型白名单：人类维护，Agent 只能从这里挑
    subagent_models_file: str = field(default_factory=lambda: _s("SUBAGENT_MODELS_FILE", "/app/config/subagent_models.json"))
    # 同时最多跑几个子代理（主 Agent 一轮里可以并发派发多个）
    subagent_max_concurrency: int = field(default_factory=lambda: _i("SUBAGENT_MAX_CONCURRENCY", 3))
    # 单轮心跳里最多派发几次（成本护栏，与并发数是两个不同的轴）
    subagent_max_per_beat: int = field(default_factory=lambda: _i("SUBAGENT_MAX_PER_BEAT", 8))

    # --- 人格 / 角色扮演（人类设定，热加载）---
    persona_file: str = field(default_factory=lambda: _s("PERSONA_FILE", "/app/config/persona.md"))

    # --- 叙事层（把干巴日志重写成角色口吻）---
    #
    # 它是单向的：读事件流，吐事件流。输出永远不会回到操盘上下文。
    # 所以它配多贵的模型、写多长，都不会影响交易决策 —— 只影响你读日志的体验。
    #
    # 触发有两条，满足任一条就写：
    #   A. 有事        —— 成交 / 被风控拒绝 / Operator 留言 / 报错
    #   B. 太久没说了  —— 距上次叙事超过 NARRATOR_MAX_GAP 秒
    #
    # B 不是"兜底"。震荡行情里 Agent 可以一整天不动，而"为什么不动"恰恰是
    # 最该记下来的东西 —— 那是判断，不是空白。
    narrator_enabled: bool = field(default_factory=lambda: _b("NARRATOR_ENABLED", False))
    # 无事时最长憋多久写一篇（秒）。6h = 一天四篇左右。设 0 = 每轮都写。
    narrator_max_gap: int = field(default_factory=lambda: _i("NARRATOR_MAX_GAP", 6 * 3600))
    narrator_base_url: str = field(default_factory=lambda: _s("NARRATOR_BASE_URL"))
    narrator_api_key: str = field(default_factory=lambda: _s("NARRATOR_API_KEY"))
    narrator_model: str = field(default_factory=lambda: _s("NARRATOR_MODEL"))
    # 写东西比做判断需要更高的温度，所以这里和操盘是分开的两个数
    narrator_temperature: float = field(default_factory=lambda: _f("NARRATOR_TEMPERATURE", 0.85))
    # ⚠️ 默认给 8192 而不是 2000：叙事层用思考型模型时，
    # reasoning token 也算进 max_tokens。实测 max_tokens=2000 时思考吃掉 1922，
    # 正文只写了 94 字就被 finish_reason=length 截断。
    # 而 journal.md 是 append-only 的、又是跨重启恢复叙事节奏的依据 ——
    # 一篇被腰斩的日志会一辈子待在账本里。宁可贵一点。
    narrator_max_tokens: int = field(default_factory=lambda: _i("NARRATOR_MAX_TOKENS", 8192))
    # 思考型模型要留够时间。原来 120 秒是按非思考模型定的。
    narrator_timeout: float = field(default_factory=lambda: _f("NARRATOR_TIMEOUT", 300))
    narrator_min_chars: int = field(default_factory=lambda: _i("NARRATOR_MIN_CHARS", 150))
    # 注意：聚合窗口横跨多轮时，它是"整篇"的上限，不是"每轮"的上限。
    narrator_max_chars: int = field(default_factory=lambda: _i("NARRATOR_MAX_CHARS", 600))
    # 人物志：只给叙事层看，不进操盘提示词
    narrator_character_file: str = field(
        default_factory=lambda: _s("NARRATOR_CHARACTER_FILE", "/app/config/character.md"))
    # 手账落点：刻意放在 logs 而不是 workspace ——
    # 工作区是 Agent 自己 fs_read 得到的地方，放手账等于把叙事喂回操盘上下文。
    narrator_journal_file: str = field(
        default_factory=lambda: _s("NARRATOR_JOURNAL_FILE", "/data/logs/journal.md"))
    # 把手账末尾多少字符喂给叙事模型（保持语气连续性）。设 0 则完全不喂。
    narrator_journal_excerpt: int = field(
        default_factory=lambda: _i("NARRATOR_JOURNAL_EXCERPT", 1200))

    # --- MCP ---
    market_mcp_url: str = field(default_factory=lambda: _s("MARKET_MCP_URL", "http://mcp-market:8081/mcp"))
    paper_mcp_url: str = field(default_factory=lambda: _s("PAPER_MCP_URL", "http://mcp-paper:8082/mcp"))
    exa_mcp_url: str = field(default_factory=lambda: _s("EXA_MCP_URL"))

    # --- 调度 ---
    # 注意：这里**没有** HEARTBEAT_SECONDS 了。调度器早就不读它 ——
    # 唤醒节奏的唯一判据是唤醒策略（wake_policy，见 wake.py）。
    # 那个变量在 .env 里留了一阵子，状态接口还在报它，看起来像"还有一个心跳旋钮"，
    # 其实旋钮早断了。2026-09-27 清掉。
    active_hours: str = field(default_factory=lambda: _s("ACTIVE_HOURS"))

    # --- 身份 ---
    agent_name: str = field(default_factory=lambda: _s("AGENT_NAME", "Pulse"))
    watchlist: list[str] = field(default_factory=lambda: [
        s.strip().upper() for s in _s("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()
    ])

    # --- 路径 ---
    workspace_dir: str = field(default_factory=lambda: _s("WORKSPACE_DIR", "/data/workspace"))
    log_dir: str = field(default_factory=lambda: _s("LOG_DIR", "/data/logs"))
    static_dir: str = field(default_factory=lambda: _s("STATIC_DIR", "/app/static"))

    # --- 能力 ---
    enable_shell: bool = field(default_factory=lambda: _b("ENABLE_SHELL", True))
    shell_timeout: int = field(default_factory=lambda: _i("SHELL_TIMEOUT", 30))
    # run_shell 的文件系统沙箱（Landlock）。auto = 用得上就用，挂不上就降级并在
    # 工具返回值里留一行警告；require = 挂不上就拒绝执行；off = 完全关掉（应急）。
    # 详见 sandbox.py 顶部。
    shell_sandbox: str = field(default_factory=lambda: (_s("SHELL_SANDBOX", "auto") or "auto").lower())

    # --- WebUI ---
    web_port: int = field(default_factory=lambda: _i("WEB_PORT", 8000))
    webui_user: str = field(default_factory=lambda: _s("WEBUI_USER"))
    webui_password: str = field(default_factory=lambda: _s("WEBUI_PASSWORD"))

    # 公开只读页（2026-09-27 加）。无鉴权能看日志和工具调用，写操作和配置一律不给。
    # 关掉它就等于回到"要么全锁、要么全裸"的二值状态 —— 见 web.py 顶部那张路由表。
    public_log_page: bool = field(default_factory=lambda: _b("PUBLIC_LOG_PAGE", True))
    # 匿名 SSE 同时最多开几路。这是**全局**上限（不是按 IP）——
    # 面板挂在 Cloudflare 后面，nginx 看到的 remote_addr 是 CF 边缘的 IP，
    # 按 IP 限流在公网流量下会变成"所有人共用一个桶"。详见 nginx/*.conf 的注释。
    public_max_streams: int = field(default_factory=lambda: _i("PUBLIC_MAX_STREAMS", 40))
    # 公开的历史事件条数上限。面板自己一次要 150 条，这里留够余量但不许无限拉。
    public_events_max: int = field(default_factory=lambda: _i("PUBLIC_EVENTS_MAX", 400))

    # --- 上下文预算（直接决定每轮心跳的 token 成本）---
    max_tool_result_chars: int = field(default_factory=lambda: _i("MAX_TOOL_RESULT_CHARS", 2000))
    # 事件流里 tool_call 的 args 存多少字符（**只影响面板和复盘，不影响喂给模型的参数**）。
    # 原来是写死的 300 —— 实测 47 条带多行文本的参数里有 3 条被砍在半句话上，
    # 而面板恰恰是"看清楚它到底要干什么"的地方。多存几百字符的代价可以忽略
    # （一天多不到 1MB），换成能读完整件参数是划算的。
    tool_call_args_chars: int = field(default_factory=lambda: _i("TOOL_CALL_ARGS_CHARS", 1200))
    journal_excerpt_chars: int = field(default_factory=lambda: _i("JOURNAL_EXCERPT_CHARS", 3000))
    # 一轮心跳里「自己取数」（market__* / exa__*）超过这个次数、且还没用过子代理时，
    # 往那条取数结果的末尾追加一行「你已经自己取了N次」的运行时提醒。
    #
    # 实测：静态提示词说服不了 Agent，把实时计数摆在它眼前才行 —— 但计数的口径很关键。
    # 曾经按「工具调用总数」算，结果一轮心跳里 5~9 次调用中有 4~7 次是躲不掉的琐务
    # （读笔记、列目录、写笔记、打权益点、看账本），提醒于是**每轮都误报**，
    # Agent 两轮就学会了无视它，甚至会把它误读成"工具调用配额快满了"。
    # 改成只看取数次数之后，它只在真的在烧上下文时才出声。
    cost_nudge_after: int = field(default_factory=lambda: _i("COST_NUDGE_AFTER", 3))

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def narrator_configured(self) -> bool:
        """显式开关 + 有可用端点。没单独配端点就回落主 LLM。"""
        if not self.narrator_enabled:
            return False
        # 端点 + 模型齐了就成立 —— key 可以回落 LLM_API_KEY（同一端点上的便宜模型
        # 直接留空 NARRATOR_API_KEY 即可，省得把同一把钥匙抄两遍）。
        if self.narrator_base_url and self.narrator_model:
            return True
        return self.llm_configured

    @property
    def narrator_effective_model(self) -> str:
        return self.narrator_model or self.llm_model

    @property
    def subagent_configured(self) -> bool:
        """白名单文件存在且非空，才算配好了（Agent 才有 delegate 可用）。

        真实的判断在 SubAgent.load_models() 里做 —— 它能读到白名单文件、
        且至少有一个模型带齐了 base_url + 可用 api_key，才返回非空。
        这里只做一个廉价的预检，避免为了状态显示去读文件。
        """
        try:
            import json
            import pathlib

            p = pathlib.Path(self.subagent_models_file)
            if not p.is_file():
                # 兼容老的单模型环境变量写法
                return bool(self.subagent_base_url and self.subagent_model
                            and not any(m in (self.subagent_base_url + self.subagent_model).lower()
                                        for m in ("placeholder", "replace_me", "your-")))
            data = json.loads(p.read_text(encoding="utf-8"))
            return bool(data.get("models"))
        except Exception:
            return False

    def active_now(self, hour: int) -> bool:
        """ACTIVE_HOURS 形如 '8-23'；留空表示 7x24 全天候。"""
        raw = self.active_hours
        if not raw or "-" not in raw:
            return True
        try:
            lo, hi = (int(x) for x in raw.split("-", 1))
        except ValueError:
            return True
        return lo <= hour <= hi if lo <= hi else (hour >= lo or hour <= hi)


cfg = Config()
