#!/usr/bin/env python3
"""公开只读档的行为测试：匿名能碰到什么、碰不到什么、碰到的那一份削掉了哪些字段。

# 它为什么存在

2026-09-27 用户要把面板对外开放：「无鉴权能看到日志，但一个字也改不了、
配置也读不到」。这件事的风险不在"记得加鉴权"，而在**漏**，而漏有三种长相：

  1. 新加了一个端点，没人想"这要不要给游客" —— 如果白名单写成黑名单，默认就是漏。
  2. 事件往公开流里流的时候，顺带带一个配置字段（整份唤醒策略就夹在
     `wake_policy_changed` 里）。
  3. 公开页的 JS 里 fetch 了私有端点 —— 页面上什么都不显示，看着像后端坏了，
     而实际是前面那道闸在干活。

这三条都不是"写代码时小心点"能兜住的，所以这一份靠**枚举**来测：

  第 [1] 节把 `app.routes` 整个过一遍，除 PUBLIC_ROUTES 外一律必须 401。
  加路由的人会被它拦下来，而不是靠记性。
  第 [9] 节解析 nginx 那份配置，`auth_basic off` 的位置必须和同一份白名单对上 ——
  nginx 和应用层漂了，只有一种后果（更严），但"更严"表现出来就是游客打不开，
  那时候你会先怀疑后端，而不会怀疑那份一天都不看一眼的 conf。

跑法：bash runtests.sh public（在 brain 镜像里跑：要 fastapi + httpx）
"""
from __future__ import annotations

import asyncio
import ast
import base64
import contextlib
import pathlib
import re
import sys

FAILS: list[str] = []
SKIPPED: list[str] = []

# 匿名可达的**全部**路由。这是一份**字面清单**，故意不和 web.PUBLIC_ROUTES 共用：
# 共用的话，谁往那边加一条、这边就自动跟着绿，等于没有守。
# 想加就同时改两处 —— 那一刻你被迫想一遍"这真的要给游客吗"。
EXPECT_PUBLIC = {
    "/live",
    "/api/public/events",
    "/api/public/events/stream",
    "/api/public/market",
    "/api/public/state",
}

USER, PWD = "trader", "s3cret"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def _load_web():
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "brain" / "trader", here / "trader",
                 pathlib.Path("/t/trader"), pathlib.Path("/app/trader")):
        if (cand / "web.py").is_file():
            sys.path.insert(0, str(cand.parent))
            import trader.web
            return trader.web
    raise SystemExit("找不到 trader/web.py")


def auth() -> dict:
    raw = f"{USER}:{PWD}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


# ---------------------------------------------------------------------- 假件
class FakeCfg:
    def __init__(self, public: bool = True, cap: int = 4) -> None:
        self.webui_user = USER
        self.webui_password = PWD
        self.static_dir = _static_dir()
        self.watchlist = ["BTCUSDT", "ETHUSDT"]
        self.public_log_page = public
        self.public_max_streams = cap
        self.public_events_max = 400


def _static_dir() -> str:
    """真去供 static/ 里那两个文件（不是造一份假的）。

    runtests.sh 会把 brain/static **一起**拷进 stage，所以容器里能拿到真的
    index.html / live.html —— "公开页确实是从 live.html 供出去的"这件事
    才算真验过（不然测的只是一段 if isfile 的路径拼接）。
    """
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "static", here / "brain" / "static", pathlib.Path("/app/static")):
        if (cand / "live.html").is_file():
            return str(cand)
    return "/tmp"


class FakeWake:
    def __init__(self) -> None:
        self.last_prices = {"BTCUSDT": 84349.79, "ETHUSDT": 2693.41}
        self.refs = {"BTCUSDT": 84386.01, "ETHUSDT": 2694.76}
        self.update_calls = 0
        self.wake_requests = 0

    def watched_symbols(self):
        return ["BTCUSDT", "ETHUSDT"]

    def prices_age_seconds(self):
        return 3.0

    def effective(self):
        return {"poll_seconds": 30}

    async def refresh_sleep(self):
        return {}

    def status(self):
        # 真实 status() 里塞着整份策略（阈值/间隔/配额）—— 这就是最该被拦下来的东西
        return {"policy": {"interval_seconds": 5400, "thresholds": {"BTCUSDT": {"pct": 1.0}}},
                "human": {"pct": 1.0}, "sleep": {"sleeping": False}}

    def update_human(self, payload):
        self.update_calls += 1
        return payload

    def request_wake(self, source, reason, **extra):
        self.wake_requests += 1
        return True


class FakeAgent:
    def __init__(self) -> None:
        self.paused = False
        self.instructions: list[str] = []
        self.narrator = None
        self.beats = 7

    def status(self):
        # 一份**照着真实 status() 抄的**假数据：里面 llm_model / persona /
        # narrator / subagent / wake 全是配置。公开版一个都不许剩。
        return {
            "agent_name": "Hallu", "paused": self.paused, "beats": self.beats,
            "skipped": 1, "last_beat_at": "07:30:11", "next_beat_at": "08:00:00",
            "next_beat_seconds": 120, "last_error": None, "tokens_used": 123456,
            "llm_configured": True, "llm_model": "Gemini-3.8-Flash-Thinking/Antigravity",
            "enable_shell": True, "pending_instructions": 0,
            "last_beat": {"tool_calls": 9, "fetch_calls": 4, "fetch_tools": {"market__get_price": 4},
                          "symbols_seen": ["BTCUSDT"], "delegates": 1, "nudged": False},
            "mcp": {"market": {"connected": True, "tools": 9}},
            "subagent": {"enabled": True, "default_model": "glm-5.3-fast", "calls": 2},
            "tool_count": 34,
            "persona": {"loaded": True, "chars": 300, "file": "persona.md"},
            "narrator": {"enabled": True, "model": "Gemini-3.8-Flash-Thinking/Antigravity"},
            "wake": {"policy": {"interval_seconds": 5400}},
        }

    def instruct(self, text: str) -> None:
        self.instructions.append(text)

    def status_paused(self):
        return self.paused


class FakeBus:
    def __init__(self, events=None) -> None:
        self._events = events if events is not None else []
        self.subs: list = []
        self.seq = len(self._events)

    def history(self, limit: int = 300):
        return list(self._events)[-max(1, limit):]

    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue()
        self.subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        if q in self.subs:
            self.subs.remove(q)

    def emit(self, kind, **data):
        ev = {"id": len(self._events) + 1, "ts": "2026-09-27 07:30:11",
              "kind": kind, "data": data}
        self._events.append(ev)
        for q in list(self.subs):
            q.put_nowait(ev)
        return ev


def demo_events() -> list:
    """一份**故意带配置字段**的事件流。第 [4] 节拿它对账。"""
    return [
        {"id": 1, "ts": "2026-09-27 07:30:11", "kind": "thinking",
         "data": {"text": "## 盘面\n- BTC 站稳了", "step": 1}},
        {"id": 2, "ts": "2026-09-27 07:30:12", "kind": "tool_call",
         "data": {"tool": "paper__get_account", "args": '{"nothing": 1}', "step": 1}},
        {"id": 3, "ts": "2026-09-27 07:30:13", "kind": "wake_policy_changed",
         "data": {"by": "human", "policy": {"interval_seconds": 5400,
                                            "thresholds": {"BTCUSDT": {"pct": 1.0}}}}},
        {"id": 4, "ts": "2026-09-27 07:30:14", "kind": "system",
         "data": {"message": "系统启动中…", "model": "Gemini-3.8-Flash-Thinking/Antigravity",
                  "servers": {"market": {"ok": True}}}},
        # 2026-09-27 上线当天从**真实数据**里抓到的漏：沙箱自检这条系统事件带着
        # `.env` 的开关和安全姿态。第一版的 PUBLIC_STRIP 是按事件类型列的，
        # 只想到削 model / servers，于是这三个漏了出去。现在按字段名全局拒。
        {"id": 7, "ts": "2026-09-27 07:30:17", "kind": "system",
         "data": {"message": "沙箱自检", "landlock_abi": 2,
                  "shell_sandbox": "auto", "non_dumpable": True}},
        # 子代理模型白名单：id 和标签都来自 config/subagent_models.json
        {"id": 8, "ts": "2026-09-27 07:30:18", "kind": "subagent_start",
         "data": {"job": "j1", "model": "glm-5.3-fast", "model_label": "GLM 5.3 Fast",
                  "task": "查一下 SOL 的链上手续费", "want": "一段浓缩结论"}},
        {"id": 9, "ts": "2026-09-27 07:30:19", "kind": "parallel_batch",
         "data": {"tools": ["market__get_price"], "models": ["glm-5.3-fast"], "step": 3}},
        {"id": 5, "ts": "2026-09-27 07:30:15", "kind": "subagent_models_error",
         "data": {"error": "JSONDecodeError", "path": "/app/config/subagent_models.json"}},
        {"id": 6, "ts": "2026-09-27 07:30:16", "kind": "narrative",
         "data": {"text": "今天盘面很安静。", "finish_reason": "stop"}},
    ]


class FakeRuntime:
    def __init__(self, cfg: FakeCfg, events=None) -> None:
        self.cfg = cfg
        self.wake = FakeWake()
        self.agent = FakeAgent()
        self.bus = FakeBus(events if events is not None else demo_events())
        self.wake_event = asyncio.Event()
        self.hub = None

    async def hub_json(self, tool: str, args: dict | None = None):
        if tool == "paper__get_risk_limits":
            # 私有面板读的就是这一份。公开档必须让它**根本不出现在响应里**
            return {"max_open_positions": 5, "max_position_pct_of_equity": 25.0,
                    "max_daily_loss_pct": 5.0, "fee_bps_round_trip": 30}
        if tool == "paper__get_account":
            return {"equity": 10000.0, "cash": 10000.0, "open_positions": 0,
                    "positions": [], "total_return_pct": 0.0}
        if tool == "paper__get_performance":
            return {"closed_trades": 0, "win_rate_pct": None}
        return []


def _routes(app) -> list[tuple[str, str]]:
    out = []
    for r in app.routes:
        p = getattr(r, "path", None)
        if not p or p.startswith(("/openapi", "/docs", "/redoc")):
            continue
        for m in sorted((getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"}):
            out.append((p, m))
    return out


# ---------------------------------------------------------------------- [11] 用
#
# **可以公开**的字段名：**这份名单是手抄的、冻结的**，这正是它有用的原因。
# 新加一个字段而没分类 → [11] 会红，逼你想一遍"这要不要给游客看"。
# （如果它是由 `keys - SECRET` 算出来的，就永远不会红 —— 那就不是测试了。）
#
# 判据：这个字段让读者知道了"系统被设成什么样"吗？不。它说的是"刚刚发生了什么"。
BENIGN_FIELDS = {
    # 一轮心跳
    "beat", "trigger", "step", "steps", "duration", "elapsed", "ok", "used", "auto",
    # 内容
    "message", "text", "reason", "answer", "result", "args", "task", "want", "error",
    # 工具与子代理
    "tool", "tools", "job", "active", "queued", "calls", "count", "missing", "wake_note",
    # 交易
    "symbol", "side", "qty", "price", "fill_price", "notional", "fee", "realized_pnl",
    # 唤醒与睡眠（**状态**，不是配额 —— 配额是 min_hours_per_day 那几个，在 SECRET 里）
    "source", "by", "rule", "ref", "prices", "hits", "cap", "slept_hours",
    "slept_last_24h_hours", "debt_hours", "awake_span_hours", "truncated", "chars",
    # 读数字（不是配置）。`tokens` 是叙事这一篇花了多少 token ——
    # 注意别写成 `token`，那个词在 SECRET 里（当它是凭证的缩写时就要削）。
    "at", "tokens", "finish_reason",
}

# `**something` 的展开点。挖不到它的键，只能手抄 —— 所以**站点数量本身**也在守：
# 多出一处（源码里新写了一个 `bus.emit(..., **xxx)`）这一节就红，来把它的键列出来。
#
# 键手抄自各自的源头：
#   narrative       ← narrator.narrate() 的 return
#   wake_request    ← WakeController.request_wake(**extra)，extra 来自价格触发那条路
#   sleep_start/end ← reason + WakeController._sleep_fields()
DYNAMIC_SITES = {
    ("narrative", "agent.py"): {"beat", "duration", "model", "text", "tokens",
                                "finish_reason", "truncated"},
    ("wake_request", "wake.py"): {"at", "source", "reason", "symbol", "price", "ref", "hits"},
    ("sleep_start", "wake.py"): {"reason", "debt_hours", "slept_last_24h_hours",
                                 "awake_span_hours"},
    ("sleep_end", "wake.py"): {"reason", "slept_hours", "debt_hours",
                               "slept_last_24h_hours", "awake_span_hours"},
}


def _emitted_fields(trader_dir: pathlib.Path) -> tuple[set, set, set]:
    """用 ast 把 `bus.emit(...)` 的**事件类型**和**字段名**挖出来。

    为什么用 ast 不用正则：第一版正则把 `self._beat_delegates += 1` 里的
    `_beat_delegates =` 也当成了字段名（`+=` 里的那个等号），一下多出二十多个
    假名字 —— 而假名字的坏处是它们会进 BENIGN，把这份名单稀释成没用的东西。
    """
    keys: set = set()
    kinds: set = set()
    dynamic: set = set()
    for path in sorted(trader_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "emit"):
                continue
            kind = None
            if node.args and isinstance(node.args[0], ast.Constant):
                kind = node.args[0].value
                kinds.add(kind)
            for kw in node.keywords:
                if kw.arg is None:
                    dynamic.add((kind, path.name))     # **something
                else:
                    keys.add(kw.arg)
    return keys, kinds, dynamic


def _nginx_off_locations(conf: str) -> list[str]:
    """这份 nginx 配置里，哪几个 location 关掉了 auth_basic。"""
    out = []
    hits = list(re.finditer(r"location\s+([^{]+?)\s*\{", conf))
    for i, m in enumerate(hits):
        spec = m.group(1).strip().split()[-1]          # "= /live" / "^~ /api/public/"
        end = hits[i + 1].start() if i + 1 < len(hits) else len(conf)
        body = conf[m.end():end]
        if re.search(r"(?m)^\s*auth_basic\s+off\s*;", body):
            out.append(spec)
    return out


async def main() -> int:
    web = _load_web()
    import httpx

    print("=" * 68)
    print("公开只读档 · 匿名能碰到什么（假 runtime，不联网）")
    print("=" * 68)

    check("测试里的字面清单 = web.PUBLIC_ROUTES", set(web.PUBLIC_ROUTES) == EXPECT_PUBLIC,
          f"web 那份：{sorted(set(web.PUBLIC_ROUTES) ^ EXPECT_PUBLIC)} 不一致")

    cfg = FakeCfg()
    rt = FakeRuntime(cfg)
    app = web.create_app(rt)
    tr = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=tr, base_url="http://t",
                                 timeout=httpx.Timeout(5.0)) as c:
        # ---------------------------------------------------------------- [1]
        print("\n[1] 路由对账：匿名打过去，除白名单外一条都不许通")
        routes = _routes(app)
        check(f"枚举到 {len(routes)} 条路由", len(routes) >= 12, f"实际 {len(routes)}")
        leaked, public_hit = [], set()
        stream_probe_broken = False
        for path, method in routes:
            probe = path.replace("{action}", "pause").replace("{path}", "")
            if probe in EXPECT_PUBLIC:
                # 公开流这条**不能**用普通请求打：200 就是一条永不结束的响应，
                # httpx 会一直等 body（2026-09-27 就把这份测试挂死过一次）。
                # 所以只等响应头，拿到状态就放手。ASGI 直连那一套见 [8]。
                code = await _status_only(app, probe)
                if code is None:
                    stream_probe_broken = True
                    continue
            else:
                code = (await c.request(method, probe)).status_code
            if code == 401:
                continue
            if probe in EXPECT_PUBLIC:
                public_hit.add(probe)
            else:
                leaked.append(f"{method} {probe} → {code}")
        if stream_probe_broken:
            SKIPPED.append("[1] 公开流那一条的状态码（ASGI 直连没起来）")
        check("没有漏网的（除白名单外全部 401）", not leaked,
              "；".join(leaked) + " —— 这就是「忘了想」的样子")
        check("白名单里那几条真的能匿名进", public_hit == {p for p in EXPECT_PUBLIC
                                                          if p in {q for q, _ in routes}},
              f"实际进得去的：{sorted(public_hit)}")
        check("不存在的路径也不给 404（不给游客探接口清单）",
              (await c.get("/api/definitely-not-here")).status_code == 401)

        print("\n[2] 带凭证：面板照旧全通（这一档不许被改坏）")
        for path in ("/", "/api/state", "/api/events", "/api/wake", "/api/health"):
            r = await c.get(path, headers=auth())
            check(f"GET {path} → 200", r.status_code == 200, f"实际 {r.status_code}")
        live = await c.get("/live")
        check("匿名 GET /live → 200", live.status_code == 200, f"实际 {live.status_code}")
        live_file = pathlib.Path(cfg.static_dir) / "live.html"
        if not live_file.is_file():
            SKIPPED.append("[2] /live 的内容对账（stage 里没有 static/live.html）")
            print("  ~ 跳过：找不到 static/live.html，只验了状态码")
        else:
            text = live_file.read_text(encoding="utf-8")
            check("供出去的确实是 live.html（逐字相同，不是面板那份）",
                  live.text == text)
            # **正面**写这条，不用黑名单：公开页里出现的每一个 `api/…` 都必须是
            # api/public/ 开头。以后有人加一个 fetch("api/state") 想显示个什么，
            # 它会红 —— 而页面上表现出的只是"那一块永远空着"，肉眼看不出原因。
            #
            # 扫的是**全文**，不管它在代码里还是在注释里。第一版这里要求 `api/…`
            # 前面跟一个引号（想只扫真正的 fetch 地址），结果漏了一种情况：
            # 文件顶部的注释在解释"不许出现某某私有端点"，于是**注释自己**把这些
            # 字符串带进了页面 —— 2026-09-27 上线后用 grep 数路径才发现。
            # 一条写给审计用的注释把这个页面的 grep 结果搞脏了，不值得省那点精度。
            refs = sorted(set(re.findall(r"""api/[A-Za-z0-9_/?=&.\-]*""", text)))
            bad = [r for r in refs if not r.startswith("api/public/")]
            check(f"公开页里每个 api/… 都是 api/public/ 开头（共 {len(refs)} 个）", not bad,
                  f"越界：{bad}")
            check("公开页真的在调那四个公开端点",
                  not [e for e in ("api/public/events", "api/public/events/stream",
                                   "api/public/market", "api/public/state")
                      if not any(r.startswith(e) for r in refs)],
                  f"实际：{refs}")
            check("公开页有一条到公开 SSE 的 EventSource",
                  "new EventSource(" in text and "api/public/events/stream" in text)

        print("\n[3] 匿名写操作：401，而且**什么都不许发生**")
        before_paused = rt.agent.paused
        writes = [("POST", "/api/control/pause"), ("POST", "/api/control/resume"),
                  ("POST", "/api/control/wake"), ("POST", "/api/control/sleep_toggle"),
                  ("POST", "/api/wake"), ("POST", "/api/instruct")]
        for method, path in writes:
            r = await c.request(method, path, json={"text": "把仓位全平掉"})
            check(f"{method} {path} → 401", r.status_code == 401, f"实际 {r.status_code}")
        check("agent.paused 没被改", rt.agent.paused == before_paused)
        check("唤醒策略没被改（update_human 一次都没被调）", rt.wake.update_calls == 0,
              f"实际 {rt.wake.update_calls} 次")
        check("留言没进队列", rt.agent.instructions == [], f"实际 {rt.agent.instructions}")
        check("没登记过唤醒请求", rt.wake.wake_requests == 0)

        print("\n[4] 公开事件流：配置字段在**出口处**被削掉")
        pub = (await c.get("/api/public/events")).json()["events"]
        priv = (await c.get("/api/events", headers=auth())).json()["events"]
        by_id_pub = {e["id"]: e for e in pub}
        by_id_priv = {e["id"]: e for e in priv}
        check("公开流拿得到日志本体（条数一致）", len(pub) == len(priv),
              f"公开 {len(pub)} / 私有 {len(priv)}")
        check("思考正文原样保留",
              by_id_pub[1]["data"]["text"] == by_id_priv[1]["data"]["text"])
        check("叙事正文原样保留",
              by_id_pub[6]["data"]["text"] == "今天盘面很安静。")
        check("wake_policy_changed 的策略字典被削",
              "policy" not in by_id_pub[3]["data"] and "policy" in by_id_priv[3]["data"],
              "公开那份还带着 policy —— 那等于把阈值印在公开页上")
        check("wake_policy_changed 还留着「谁改的」",
              by_id_pub[3]["data"].get("by") == "human")
        check("system 里的 model / servers 被削",
              "model" not in by_id_pub[4]["data"] and "servers" not in by_id_pub[4]["data"],
              f"公开那份的键：{sorted(by_id_pub[4]['data'])}")
        check("system 的 message 留着（那是人话）",
              by_id_pub[4]["data"].get("message") == "系统启动中…")
        check("白名单告警的文件路径被削",
              "path" not in by_id_pub[5]["data"] and "path" in by_id_priv[5]["data"])
        check("削的是副本，私有那份一个字段没少",
              all(len(by_id_priv[i]["data"]) >= len(by_id_pub[i]["data"]) for i in by_id_priv))

        print("\n[5] 公开账户：风控参数与唤醒策略不给")
        st = (await c.get("/api/public/state")).json()
        full = (await c.get("/api/state", headers=auth())).json()
        check("私有那份**有**风控参数（对照）", "risk_limits" in full
              and full["risk_limits"].get("max_open_positions") == 5)
        check("公开那份没有 risk_limits 这个键", "risk_limits" not in st,
              f"公开那份的键：{sorted(st)}")
        for key in ("equity", "cash", "positions"):
            check(f"账户读数字还在（{key}）", key in (st.get("account") or {}))
        check("权益曲线/绩效/成交都在",
              all(k in st for k in ("equity_curve", "performance", "trades")))
        pstatus = st.get("status") or {}
        for banned in ("llm_model", "persona", "narrator", "wake", "subagent",
                       "enable_shell", "last_error"):
            check(f"status 里没有 {banned}", banned not in pstatus)
        check("status 里留的是「它在干嘛」（心跳数、token、上一轮解剖）",
              pstatus.get("beats") == 7 and "tokens_used" in pstatus
              and "tool_calls" in (pstatus.get("last_beat") or {}))

        print("\n[6] 公开行情：切掉的是阈值基准价，不是行情")
        pm = (await c.get("/api/public/market")).json()
        fm = (await c.get("/api/market", headers=auth())).json()
        check("私有那份带着 ref / dev_from_ref_pct（对照）",
              fm["symbols"][0].get("ref") == 84386.01
              and fm["symbols"][0].get("dev_from_ref_pct") is not None)
        check("公开那份两个键都没了",
              all("ref" not in r and "dev_from_ref_pct" not in r for r in pm["symbols"]),
              f"第一行剩的键：{sorted(pm['symbols'][0])}")
        check("现价还在、来源还是 30 秒轮询那份",
              pm["symbols"][0]["price"] == 84349.79 and pm["symbols"][0]["source"] == "poll")
        check("24h 那几列也在（行情不是配置）", "change_pct_24h" in pm["symbols"][0])

    # ------------------------------------------------------------------ [7]
    print("\n[7] 公开流的并发闸门（纯逻辑：上限 / 回收 / 幂等 / 过期）")
    lim = web.PublicStreamLimiter(cap=2)
    a, b = lim.open(), lim.open()
    check("前两路放行", a is not None and b is not None)
    check("第三路被拒", lim.open() is None, f"active={lim.active}")
    lim.close(a)
    check("关掉一路就能再进一路", lim.open() is not None)
    before = lim.active
    lim.close(a)
    check("close 是幂等的（同一条租约关两次，不会顺手把别人的额度也放掉）",
          lim.active == before, f"active {before} → {lim.active}")
    lim.close(999999)
    check("关一个不存在的 token 不炸、也不掉额度", lim.active == before)
    check("租约过期会自己回收（响应体一字节没发就断的那种情况）", _expire_probe(web))

    # ------------------------------------------------------------------ [8]
    print("\n[8] 真打一次：满了回 503，松开了又能进")

    cfg2 = FakeCfg(cap=1)
    rt2 = FakeRuntime(cfg2)
    app2 = web.create_app(rt2)

    async def _probe():
        # 第一路：手工把 ASGI 调用推起来，**停在响应头之后**（不读完）
        task1, st1 = await _drive(app2, "/api/public/events/stream")
        if st1 is None:
            return "skip", "ASGI 直连没起来（3 秒内没等到响应头）"
        if st1 != 200:
            return "fail", f"第一路就没通：{st1}"
        # 第二路：满了 → 503（这条是 JSON，用普通请求打不会挂）
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                     base_url="http://t",
                                     timeout=httpx.Timeout(3.0)) as c2:
            r2 = await c2.get("/api/public/events/stream")
            if r2.status_code != 503:
                return "fail", f"第二路应该 503，实际 {r2.status_code}"
        # 拔掉第一路（就当访客关了标签页）
        task1.cancel()
        with contextlib.suppress(BaseException):
            await task1
        await asyncio.sleep(0.1)
        # 额度必须还回来 —— 这一条才是这个闸门真正的考点：
        # 不回收的话，攒够 cap 次意外断开，公开页就再也打不开了（重启才修得好）。
        task2, st2 = await _drive(app2, "/api/public/events/stream")
        if st2 is None:
            return "skip", "第二路的状态没等到"
        if st2 != 200:
            return "fail", f"松开了应该能重进，实际 {st2}"
        task2.cancel()
        with contextlib.suppress(BaseException):
            await task2
        return "ok", ""

    kind, msg = await _probe()
    if kind == "skip":
        SKIPPED.append("[8] 公开流并发（ASGI 直连没起来）")
        print(f"  ~ 跳过：{msg}")
    else:
        check("满了回 503、断开之后额度会还回来", kind == "ok", msg)

    # ------------------------------------------------------------------ [9]
    print("\n[9] 总开关关掉之后：一页都不留（但面板照旧）")
    cfg_off = FakeCfg(public=False)
    rt_off = FakeRuntime(cfg_off)
    app_off = web.create_app(rt_off)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_off),
                                 base_url="http://t", timeout=httpx.Timeout(5.0)) as c3:
        for path in sorted(EXPECT_PUBLIC):
            r = await c3.get(path)
            check(f"PUBLIC_LOG_PAGE=false 时 {path} → 401", r.status_code == 401,
                  f"实际 {r.status_code}")
        check("面板不受影响（带凭证照样 200）",
              (await c3.get("/api/state", headers=auth())).status_code == 200)

    # ------------------------------------------------------------------ [10]
    print("\n[10] nginx 对账：auth_basic off 的位置 = 同一份白名单")
    here = pathlib.Path(__file__).resolve().parent
    confs = sorted((here / "nginx").glob("*.conf")) if (here / "nginx").is_dir() else []
    if not confs:
        SKIPPED.append("[10] nginx 对账（本地没有 nginx/ 目录）")
        print("  ~ 跳过：找不到 nginx/*.conf（工作区那侧才有）")
    else:
        conf = confs[0].read_text(encoding="utf-8")
        offs = _nginx_off_locations(conf)
        check("只有两处 auth_basic off", sorted(offs) == ["/api/public/", "/live"],
              f"实际 {sorted(offs)}")
        check("恰好等于 web.PUBLIC_PAGE + PUBLIC_PREFIX",
              set(offs) == {web.PUBLIC_PAGE, web.PUBLIC_PREFIX},
              f"web: {web.PUBLIC_PAGE} / {web.PUBLIC_PREFIX}")
        check("私有 location 仍然要凭证",
              not re.search(r"location\s+/\s*\{[^}]*auth_basic\s+off", conf, re.S))
        check("公开流那条关掉了 proxy_buffering（不然日志会被攒住）",
              re.search(r"location\s+\^~\s+/api/public/\s*\{[^}]*proxy_buffering\s+off",
                        conf, re.S) is not None)
        check("检测器本身是活的（拿一份改坏的配置试）",
              _nginx_off_locations("location = /oops { auth_basic off; }") == ["/oops"])

    # ------------------------------------------------------------------ [11]
    print("\n[11] 字段名清点：源码里能发出去的每个字段，都必须被分类过")
    # 这一节是**上线当天从真实数据里漏过一回**之后加的：公开流里漏了
    # `shell_sandbox` / `landlock_abi` / `non_dumpable`（`沙箱自检` 那条 system 事件）。
    # 原因是第一版的 PUBLIC_STRIP 按**事件类型**手写，只想到 system 的 model 和 servers。
    #
    # 教训不是"下次小心点"，是：**枚举事件类型这件事永远会输** —— 泄漏是按字段名发生的，
    # 而事件类型会一直长。所以现在是两道闸：
    #   ① 出口按字段名全局拒（`web.PUBLIC_SECRET_KEYS`）—— 加新事件不用改它；
    #   ② 就是这一节 —— 从源码里挖出每个 `bus.emit(...)` 的字段名，
    #      逐个要求"已被分类"。**没分类过就红**，于是"这要不要给游客看"
    #      这个问题在加字段的那一刻被强制问出来，而不是等上线后有人去读日志。
    trader_dir = pathlib.Path(web.__file__).resolve().parent
    static_keys, kinds, dynamic = _emitted_fields(trader_dir)
    benign, secret = set(BENIGN_FIELDS), set(web.PUBLIC_SECRET_KEYS)

    check(f"源码里挖到 {len(kinds)} 个事件类型 / {len(static_keys)} 个静态字段名",
          len(kinds) >= 25 and len(static_keys) >= 45,
          f"实际 {len(kinds)} 类型 / {len(static_keys)} 字段（挖不到东西说明这个测试自己坏了）")
    check("挖出来的字段名不全是假的（`+=` 那种会被 ast 挡掉）",
          "_beat_delegates" not in static_keys and "parsed" not in static_keys)
    unclassified = sorted(static_keys - benign - secret)
    check("每个字段名都分类过（漏的就是下面这些）", not unclassified,
          f"没分类：{unclassified} —— 判断标准只有一条：它说的是「系统被设成什么样」"
          f"还是「刚刚发生了什么」。前者进 web.PUBLIC_SECRET_KEYS，后者进测试里的 BENIGN_FIELDS")
    check("两份名单没有重叠（同一个名字同时说「可以公开」和「必须削」＝ 自己跟自己打架）",
          not (benign & secret), f"重叠：{sorted(benign & secret)}")
    check(f"必须削的那份名单里有 {len(secret)} 个名字（含 model / policy / shell_sandbox）",
          {"model", "models", "policy", "shell_sandbox", "landlock_abi", "path"} <= secret,
          f"实际 {sorted(secret)}")

    print("\n  [11b] 动态展开点（`**xxx`）—— 挖不到键，所以站点数量本身也在守")
    got_sites = {(k, f) for k, f in dynamic}
    check("动态展开点和手抄的那份一致（新写一处就会红）",
          got_sites == set(DYNAMIC_SITES),
          f"多出来：{sorted(got_sites - set(DYNAMIC_SITES))}；"
          f"少了：{sorted(set(DYNAMIC_SITES) - got_sites)}")
    dyn_keys = set().union(*DYNAMIC_SITES.values()) if DYNAMIC_SITES else set()
    dyn_unclassified = sorted(dyn_keys - benign - secret)
    check("动态展开的键也分类过", not dyn_unclassified, f"没分类：{dyn_unclassified}")
    check("`narrative` 展开出来的 `model` 在必须削的那份里（手账是谁写的也算配置）",
          "model" in DYNAMIC_SITES[("narrative", "agent.py")] and "model" in secret)

    print("\n  [11c] 机制验证：给**每一个**事件类型塞满敏感字段，过一个出口")
    leaked: list[str] = []
    for kind in sorted(kinds):
        payload = {k: "SENTINEL" for k in secret}
        payload.update({"message": "人话保留", "text": "正文保留"})
        out = web._public_event({"id": 1, "ts": "t", "kind": kind, "data": payload})
        left = sorted(set(out["data"]) & secret)
        if left:
            leaked.append(f"{kind}: {left}")
    check(f"{len(kinds)} 个事件类型逐个过出口，敏感字段一个不剩", not leaked,
          "；".join(leaked))
    check("该留的留下了（不能为了削干净把正文也削掉）",
          web._public_event({"id": 1, "kind": "system",
                             "data": {"message": "人话", "model": "x"}})["data"]
          == {"message": "人话"})
    check("削的是副本，原事件不动（私有流是同一批对象）",
          (lambda ev: (web._public_event(ev), "model" in ev["data"])[1])(
              {"id": 1, "kind": "system", "data": {"message": "x", "model": "y"}}))

    print("\n" + "=" * 68)
    if FAILS:
        print(f"  失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"    · {f}")
        print("=" * 68)
        return 1
    print("  全部通过")
    if SKIPPED:
        print("  ⚠ 但有 %d 节被跳过（跳过不算通过）：%s" % (len(SKIPPED), "、".join(SKIPPED)))
    print("=" * 68)
    return 0


async def _first_data(resp) -> None:
    """把 SSE 的第一段读出来（读到就行，不要求是 data 行）。"""
    async for _line in resp.aiter_lines():
        return


async def _status_only(app, path: str) -> int | None:
    """手工驱动一次 ASGI 调用，**只等响应头**，拿到状态码就放手。

    为什么不用 httpx：它这套 ASGI transport **不支持流式响应** ——
    `client.stream()` 会一直等到响应体结束（实测：挂到被 wait_for 掐死）。
    而我们要打的偏偏是一条永不结束的响应。等不到响应头就返回 None，
    调用方按"跳过"处理（跳过不算通过）。
    """
    task, status = await _drive(app, path)
    if task is None:
        return None
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
    return status


async def _drive(app, path: str) -> tuple[asyncio.Task | None, int | None]:
    """把一次 GET 推到"响应头已发出"那一刻，把任务挂着返回。

    返回 (task, status)。task 由调用方取消 —— 取消就是"访客关了标签页"，
    这正是要验的那条路径（额度得还回来）。
    """
    started, box = asyncio.Event(), {}
    gate = asyncio.Event()          # 永不 set：客户端不再发任何东西

    async def receive():
        await gate.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            box.setdefault("status", msg["status"])
            started.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [], "client": ("203.0.113.9", 4242), "server": ("t", 80),
        "root_path": "",
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return None, None
    return task, box.get("status")


def _expire_probe(web) -> bool:
    lim = web.PublicStreamLimiter(cap=1, lease_seconds=0.0)
    lim.open()
    return lim.open() is not None          # 上一条租约过期 → 这一条能进


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
