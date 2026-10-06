"""WebUI：实时日志流（SSE）+ 账户面板 + 人工干预入口。

鉴权：HTTP Basic。nginx 层与本层用同一套凭证，浏览器只会弹一次框
（nginx 会把 Authorization 头原样转发过来）。

# 两档权限（2026-09-27 加）

以前这里只有"全锁"和"全裸"两种状态（凭证一空，中间件整个跳过）。现在多了一档
**公开只读**：没有凭证的人能看日志和工具调用，别的什么都不行。

```
                      带凭证                     不带凭证
/                    面板（可写、可改策略）      401
/live                公开页                     公开页
/api/events          历史事件                   401
/api/public/events   （面板不用）               历史事件（脱敏）
/api/public/*        同上                       见 PUBLIC_ROUTES
/api/state          账户 + 风控参数             401
/api/public/state    （面板不用）               账户（无风控参数、无唤醒策略）
/api/wake  /api/control/*  /api/instruct        401
/api/workspace  /api/narrator/preview           401
```

两条不变式，都由 test_public.py 焊死：

  1. **匿名能碰到的路由 = `PUBLIC_ROUTES`，多一条都红。** 测试把 app.routes
     全枚举一遍，逐条匿名打过去，除了白名单一律必须 401。加路由时忘了想
     "这要不要给游客"，它会替你想。
  2. **配置字段在出口处抹掉，不靠"指望它别出现"。** 事件流和 status 里
     混着不少配置（整份唤醒策略、模型名、白名单文件路径），公开那一份走
     `_public_event()` / `_public_status()` 过一遍白名单/黑名单。

为什么不干脆把 index.html 挖空成"游客版"：这个项目最值钱的习惯是**审计面要小到
能一眼看完**。一份独立的小页面 + 一个 `/api/public/` 前缀，意味着"游客能看到什么"
可以一条一条对账，而不是在两万行单文件面板里找 `if(游客)`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import Config

log = logging.getLogger("trader.web")

# ---------------------------------------------------------------------- 公开集
#
# 匿名（不带 BasicAuth，或凭证不对）能碰到的**全部**东西。除这些之外一律 401。
# 改这个集合要同步三处：nginx/*.conf 的 `auth_basic off`、static/live.html
# 里 fetch 的地址、test_public.py 的第 [1] 节对账表。
PUBLIC_PAGE = "/live"
PUBLIC_PREFIX = "/api/public/"
PUBLIC_ROUTES = frozenset({
    PUBLIC_PAGE,
    PUBLIC_PREFIX + "events",
    PUBLIC_PREFIX + "events/stream",
    PUBLIC_PREFIX + "market",
    PUBLIC_PREFIX + "state",
})

# 公开流里**必须抹掉的字段名**。这是一份**全局**名单，不分事件类型。
#
# 为什么是"按字段名"而不是"按事件类型逐个列"（2026-09-27 上线当天改的）：
# 第一版写的是 `{kind: (要削的字段,)}`，结果线上第一次看真实数据就漏了三个 ——
# `沙箱自检` 那条 system 事件里带着 `shell_sandbox` / `landlock_abi` /
# `non_dumpable`（`.env` 的开关 + 安全姿态），而我只想到削 `model` 和 `servers`。
# 漏的原因不是不小心，是**判据的方向错了**：泄漏是按字段名发生的，
# 枚举事件类型永远追不上新事件。改成按名字全局拒之后，
# 加一个新事件、它顺手带上 `model`，这里什么都不用改就已经拦住了。
#
# 判据只有一条：**这个字段让读者知道了"系统被设成什么样"吗？**
#   · model / models / model_label   主模型名、子代理白名单里的 id 和标签
#   · policy / thresholds / rules    整份唤醒策略（阈值 / 间隔 / 每小时上限 / 冷却）
#   · landlock_abi / shell_sandbox / non_dumpable   沙箱模式与它挂没挂上
#   · servers / base_url / path      端点清单、白名单文件路径
#   · human / agent                  策略的两份来源（人类改的 / 它自己改的）
#   · api_key / key / password / token / secret      凭证（本该不出现在事件里，
#     列在这里是"万一"，因为这一层的成本只是一个集合成员判断）
#
# 不削的：message / text / reason / answer / args / result / qty / price …
# 那些是**内容**，是日志该有的东西。`tokens`（叙事用了多少 token）也不削 ——
# 它是读数不是配置。（注意别写成 `token`，那会误伤。）
PUBLIC_SECRET_KEYS = frozenset({
    # 模型
    "model", "models", "model_label", "llm_model", "narrator_model",
    "subagent_model", "default_model", "tokens_by_model",
    # 唤醒策略与睡眠配额
    "policy", "thresholds", "rules", "human", "agent",
    "interval_seconds", "max_triggers_per_hour", "cooldown_seconds",
    "min_hours_per_day", "max_single_hours", "max_awake_hours",
    # 沙箱姿态
    "landlock_abi", "shell_sandbox", "non_dumpable",
    # 端点 / 路径 / 凭证
    "servers", "base_url", "path", "api_key", "key", "password", "token", "secret",
})

# 事件正文（`data` 里的字符串值）一律保留 —— 那些是日志该有的东西。
# 公开的那一份走 `_public_event()`，它是这一整套权限里唯一的出口。

# /api/public/state 里 status 的白名单。**白名单，不是黑名单** ——
# agent.status() 现在是 17 个键、以后还会长，而里面 llm_model / persona /
# narrator / wake / subagent 全是配置。黑名单漏一个就是一次泄漏。
PUBLIC_STATUS_KEYS = (
    "agent_name", "paused", "beats", "skipped", "last_beat_at",
    "next_beat_at", "next_beat_seconds", "tokens_used",
    "llm_configured", "pending_instructions", "tool_count", "mcp",
)
PUBLIC_LAST_BEAT_KEYS = ("tool_calls", "fetch_calls", "delegates", "nudged", "symbols_seen")

# 一路公开流最多算"活着"多久。正常路径上它是靠生成器的 finally 收掉的，
# 这个时间只兜一种情况：请求到了但响应体从没开始发（客户端在门口就断了）。
_PUBLIC_LEASE_SECONDS = 6 * 3600


def _public_event(ev: dict) -> dict:
    """把一条事件削成"公开版"。事件本身是共享对象，所以这里返回副本。

    削的是**字段名**（`PUBLIC_SECRET_KEYS`），不分事件类型 —— 理由见那份名单上面的注释。
    只削顶层：`data` 里再嵌一层对象（比如某些工具返回值）当成正文处理，
    那是内容，不是配置。
    """
    data = ev.get("data")
    if not isinstance(data, dict):
        return ev
    kept = {k: v for k, v in data.items() if k not in PUBLIC_SECRET_KEYS}
    if len(kept) == len(data):
        return ev                      # 没削掉东西，省一次拷贝
    return {**ev, "data": kept}


def _public_status(st: dict) -> dict:
    """agent.status() → 公开版。只留"它现在在干嘛"，不留"它被设成什么样"。"""
    out = {k: st[k] for k in PUBLIC_STATUS_KEYS if k in st}
    last = st.get("last_beat")
    if isinstance(last, dict):
        out["last_beat"] = {k: last[k] for k in PUBLIC_LAST_BEAT_KEYS if k in last}
    return out


class PublicStreamLimiter:
    """公开 SSE 的并发闸门。

    为什么要有它：一路公开流就是一个**常驻连接**，而且它不花一分钱 token ——
    从成本那侧完全看不出有人在拖它。没有闸门的话，一个脚本开五千条连接就够了。

    为什么是全局而不是按 IP：服务器在 Cloudflare 橙云后面，nginx 看到的
    remote_addr 是 CF 边缘的 IP。按 IP 限流在公网流量下会变成"所有访客共用
    一个桶"，那比不限还糟。

    为什么不用信号量：`close()` 必须**幂等**。流是靠生成器的 finally 收尾的，
    而"请求进来了、响应体一字节都没发出去就断了"这种情况下 finally 不一定跑 ——
    那条租约得能靠时间自己过期，否则一次意外就永久少一路额度（攒够了公开页
    就再也打不开了，而重启才修得好）。所以这里是 (token → 开门时刻) 的表。
    """

    def __init__(self, cap: int, lease_seconds: float = _PUBLIC_LEASE_SECONDS) -> None:
        self.cap = max(1, int(cap or 1))
        self.lease_seconds = float(lease_seconds)
        self._open: dict[int, float] = {}
        self._seq = 0

    def _reap(self, now: float) -> None:
        for tok, born in list(self._open.items()):
            if now - born > self.lease_seconds:
                self._open.pop(tok, None)

    def open(self) -> int | None:
        """占一路。满了返回 None。"""
        now = time.monotonic()
        self._reap(now)
        if len(self._open) >= self.cap:
            return None
        self._seq += 1
        self._open[self._seq] = now
        return self._seq

    def close(self, token: int | None) -> None:
        if token is not None:
            self._open.pop(token, None)

    @property
    def active(self) -> int:
        return len(self._open)


def create_app(runtime) -> FastAPI:
    cfg: Config = runtime.cfg
    app = FastAPI(title="AI Trading Harness", docs_url=None, redoc_url=None, openapi_url=None)

    _cache: dict[str, tuple[float, object]] = {}
    # 每个缓存键一把锁：同一瞬间十个游客打 /api/public/state，只发起一次取数。
    # 为什么非要这个：MCP hub 是**单任务串行**消费队列的（mcp_hub._supervisor），
    # 没有 single-flight 的话，冷缓存那一刻会有十几条一模一样的账本调用
    # 排在心跳的工具调用前面 —— 游客的一屏数据把交易挤到后面。
    _cache_locks: dict[str, asyncio.Lock] = {}

    async def _cached(key: str, coro_factory, ttl: float = 4.0):
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        lock = _cache_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 拿到锁之后**再查一次**：等锁的那段时间里，可能已经有人填上了。
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            try:
                val = await coro_factory()
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            _cache[key] = (time.time(), val)
            return val

    # ------------------------------------------------------------------ 鉴权
    def _authed(request: Request) -> bool:
        if not (cfg.webui_user and cfg.webui_password):
            # 没配凭证 = 整个面板开着（老行为，故意不动：改了会把只有内网
            # 访问权、没设过凭证的人锁在门外）。.env.example 里有警告。
            return True
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        import base64
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
            user, _, pwd = raw.partition(":")
        except Exception:
            return False
        return (secrets.compare_digest(user, cfg.webui_user)
                and secrets.compare_digest(pwd, cfg.webui_password))

    def _public_ok(request: Request) -> bool:
        """这条请求匿名放行吗？**唯一的判据就是这个函数** —— 加路由想放行，
        就往 PUBLIC_ROUTES 里加，别在这里写 if。"""
        if not cfg.public_log_page:
            return False
        if request.method not in ("GET", "HEAD"):
            return False          # 公开档只有读，没有第二种动词
        path = request.url.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        return path in PUBLIC_ROUTES

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        if _authed(request) or _public_ok(request):
            return await call_next(request)
        # 401 而不是 403：浏览器收到 WWW-Authenticate 才会弹框，
        # 也就是"人"打不开的面板仍然是一句话就能进去的那份。
        return JSONResponse(
            {"detail": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="AI Trading Harness"'},
        )

    # ------------------------------------------------------------------ 页面
    @app.get("/", response_class=HTMLResponse)
    async def index():
        path = os.path.join(cfg.static_dir, "index.html")
        if not os.path.isfile(path):
            return HTMLResponse("<h1>dashboard 资源缺失</h1>", status_code=500)
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    @app.get(PUBLIC_PAGE, response_class=HTMLResponse)
    async def live():
        """公开只读页。和面板是**两个文件** —— 见本文件顶部那张路由表。"""
        path = os.path.join(cfg.static_dir, "live.html")
        if not os.path.isfile(path):
            return HTMLResponse("<h1>dashboard 资源缺失</h1>", status_code=500)
        with open(path, "r", encoding="utf-8") as f:
            # no-store：这页的内容靠 SSE 自己刷新，缓存只会让人看到旧的
            # （Cloudflare 橙云在中间，不写这一行它会按默认策略猜）
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ 状态
    @app.get("/api/state")
    async def state():
        account = await _cached("account", lambda: runtime.hub_json("paper__get_account"), 4.0)
        curve = await _cached("curve", lambda: runtime.hub_json("paper__get_equity_curve", {"limit": 400}), 20.0)
        perf = await _cached("perf", lambda: runtime.hub_json("paper__get_performance"), 15.0)
        trades = await _cached("trades", lambda: runtime.hub_json("paper__get_trade_history", {"limit": 40}), 6.0)
        limits = await _cached("limits", lambda: runtime.hub_json("paper__get_risk_limits"), 60.0)
        return {
            "status": runtime.agent.status(),
            "account": account,
            "equity_curve": curve,
            "performance": perf,
            "trades": trades,
            "risk_limits": limits,
        }

    @app.get("/api/events")
    async def events(limit: int = 200):
        return {"events": runtime.bus.history(limit)}

    # ------------------------------------------------------------------ 行情
    #
    # 面板左栏「实时行情」读的就是这里。两个数据源，刻意分开：
    #
    #   价格      直接读唤醒控制器那份 **30 秒一次的批量轮询缓存**（`last_prices`）。
    #             这里**不自己打接口** —— 那个轮询本来就在跑，`market__get_prices`
    #             又是批量的（一次 HTTP 拿 40 个币），复用它等于零边际成本。
    #             ★ 要注意的口径：`PriceMonitor` 现在无论价格触发开关关没关都取价，
    #               否则人一关触发器，面板的价就冻住了。
    #
    #   24h 统计  市面工具里没有批量的 24hr，只能按 symbol 逐个取。所以：
    #             ① 加 120 秒 TTL（面板 10 秒轮一次，不重复打）；
    #             ② 它是**懒加载**的 —— 没人看面板就一次都不发生。
    #             这两条合起来的意思是：看一眼面板 ≈ 每分钟 4 次请求，
    #             关掉面板 = 0 次。
    #   24h 统计  市面工具里没有批量的 24hr，只能按 symbol 逐个取。两层保护：
    #             ① 缓存 120 秒（面板十几秒轮一次，不重复打交易所）；
    #             ② ★ **绝不用它挡返回**。冷缓存时直接丢一个后台任务去补，
    #                这次请求立刻把现价发出去，24h 那几列先空着，下一轮填上。
    #             为什么非这样不可：MCP hub 是**单任务串行**消费队列的
    #             （`mcp_hub._supervisor`），一次心跳跑着的时候，面板这几个
    #             请求排在它的工具调用后面，等 24h 数据就是等心跳。
    #             2026-09-27 面板上那句"一直等待取价"就是这么来的 ——
    #             前端两个选择器写错了（少了 `#`），而后端又把这种等待
    #             坐实成了常态。两头都修了。
    #             这两条合起来的意思是：看一眼面板 ≈ 每分钟 4 次请求，
    #             关掉面板 = 0 次。
    def _panel_symbols() -> list[str]:
        """按 watchlist 的顺序出，而不是字母序 —— BTC/ETH/SOL/BNB 是有意义的顺序。"""
        watched: list[str] = []
        if runtime.wake is not None:
            try:
                watched = list(runtime.wake.watched_symbols())
            except Exception as exc:
                log.warning("取关注列表失败：%s", exc)
        ordered = [s for s in cfg.watchlist if s in watched or not watched]
        ordered += [s for s in watched if s not in ordered]
        return (ordered or list(cfg.watchlist))[:40]

    async def _fetch_tickers(syms: list[str]) -> dict[str, dict]:
        """逐个取 24h 统计。单个失败就少一行，绝不让整页报错。"""
        async def one(sym: str):
            try:
                return sym, await runtime.hub_json("market__get_24hr_ticker", {"symbol": sym})
            except Exception as exc:
                log.debug("24h 统计取不到 %s：%s", sym, exc)
                return sym, None

        pairs = await asyncio.gather(*(one(s) for s in syms))
        return {s: d for s, d in pairs
                if isinstance(d, dict) and "error" not in d}

    # 24h 统计的 stale-while-revalidate 缓存。key → (取到的时刻, 数据)
    _tickers: dict[str, tuple[float, dict]] = {}
    _ticker_tasks: dict[str, asyncio.Task] = {}

    def _ticker_snapshot(syms: list[str], ttl: float = 120.0) -> tuple[dict, bool]:
        """**同步**返回 (24h 数据, 是否新鲜)。冷了就丢个后台任务去补，绝不 await。"""
        key = ",".join(syms)
        hit = _tickers.get(key)
        fresh = bool(hit) and (time.time() - hit[0] < ttl)
        if not fresh and key not in _ticker_tasks:
            async def refresh():
                try:
                    val = await _fetch_tickers(syms)
                    if isinstance(val, dict):
                        _tickers[key] = (time.time(), val)
                except Exception as exc:
                    log.debug("24h 统计后台刷新失败：%s", exc)
                finally:
                    _ticker_tasks.pop(key, None)

            _ticker_tasks[key] = asyncio.create_task(refresh())
        return (hit[1] if hit else {}), fresh

    @app.get("/api/market")
    async def market():
        started = time.time()
        syms = _panel_symbols()
        prices: dict[str, float] = {}
        refs: dict[str, float] = {}
        age = None
        if runtime.wake is not None:
            prices = dict(runtime.wake.last_prices)
            refs = dict(runtime.wake.refs)
            age = runtime.wake.prices_age_seconds()

        tickers, tickers_fresh = _ticker_snapshot(syms)
        if not isinstance(tickers, dict):
            tickers = {}


        rows = []
        for sym in syms:
            t = tickers.get(sym) or {}
            price = prices.get(sym)
            if price is None and t.get("last") is not None:
                price = t["last"]
            ref = refs.get(sym)
            dev = None
            if price is not None and ref:
                dev = round((price - ref) / ref * 100, 3)
            rows.append({
                "symbol": sym,
                # 现价优先用轮询那份（30 秒新），24h 那份兜底（两分钟新）
                "price": price,
                "source": "poll" if sym in prices else ("24h" if price is not None else None),
                "change_pct_24h": t.get("change_pct_24h"),
                "high_24h": t.get("high_24h"),
                "low_24h": t.get("low_24h"),
                "volume_quote": t.get("volume_quote"),
                "ref": ref,
                "dev_from_ref_pct": dev,
            })
        return {
            "at": time.strftime("%H:%M:%S"),
            "age_seconds": age,
            "poll_seconds": (runtime.wake.effective()["poll_seconds"]
                             if runtime.wake is not None else None),
            "symbols": rows,
            # 这两个是给排查用的：took_ms 应该永远是几毫秒（不然就是又在等 24h 了），
            # tickers_fresh=False 说明 24h 那几列是上一轮的、后台正在补。
            "took_ms": int((time.time() - started) * 1000),
            "tickers_fresh": tickers_fresh,
        }

    # ------------------------------------------------------------------ 叙事层预览
    #
    # 把「下一次会发给叙事层的原文」原样交出来 —— **不发请求、不写手账**。
    #
    # 为什么值得给一个端点：叙事层是这套系统里最贵、也最不透明的一次调用，
    # 而且它的产出要写进 append-only 的手账。素材装配对不对、预算还剩多少、
    # 模型到底看得到什么 —— 以前只能靠"真发一次"去猜，而真发一次要花钱、
    # 还会污染账本（截断过一次就是这么发现的）。
    #
    # 叙事层有个真事故正好说明它的用处：2026-09-27 那篇断在「SOL 破 124 或」，
    # 事件流里看不出来、日志里也没告警，是靠人肉读句子断没断发现的。
    # narrate() 现在会在截断时打 log.error 并把 finish_reason 带进事件，
    # 而这个端点让人能在**发生之前**先看一眼素材。
    @app.get("/api/narrator/preview")
    async def narrator_preview(beat: int | None = None):
        nar = getattr(runtime.agent, "narrator", None)
        if nar is None or not getattr(nar, "enabled", False):
            return {"enabled": False}
        since = getattr(runtime.agent, "_last_narrated_id", 0)
        events = [e for e in runtime.bus.history(4000) if e.get("id", 0) > since]
        this_beat = beat if beat is not None else (getattr(runtime.agent, "beats", 1) or 1)
        out = dict(nar.preview(events, this_beat))
        out.update({
            "enabled": True,
            "model": getattr(nar.cfg, "narrator_effective_model", None),
            "max_tokens": getattr(nar.cfg, "narrator_max_tokens", None),
            "since_event_id": since,
            "events_considered": len(events),
            "would_narrate": bool(events) and nar.should_narrate(events, 1),
            "truncated_count": getattr(nar, "truncated_count", 0),
            "last_truncated": getattr(nar, "last_truncated", None),
        })
        return out

    @app.get("/api/events/stream")
    async def stream():
        async def gen():
            q = runtime.bus.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                runtime.bus.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------ 唤醒策略
    @app.get("/api/wake")
    async def wake_get():
        if runtime.wake is None:
            return {"enabled": False}
        await runtime.wake.refresh_sleep()
        return runtime.wake.status()

    @app.post("/api/wake")
    async def wake_post(payload: dict | None = None):
        """人类改唤醒策略。这是网络入口，所以白名单字段 + 数值钳位都在
        WakeController.update_human 里做，不在这里做。"""
        if runtime.wake is None:
            raise HTTPException(404, "唤醒模块未启用")
        try:
            return {"ok": True, "policy": runtime.wake.update_human(payload or {})}
        except Exception as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ 控制
    @app.post("/api/control/{action}")
    async def control(action: str, payload: dict | None = None):
        payload = payload or {}
        if action == "pause":
            runtime.agent.paused = True
            runtime.bus.emit("system", message="人类暂停了自动交易")
            return {"ok": True, "paused": True}
        if action == "resume":
            runtime.agent.paused = False
            runtime.bus.emit("system", message="人类恢复了自动交易")
            return {"ok": True, "paused": False}
        if action == "wake":
            # 人类手动叫醒：走唤醒控制器登记，这样「这轮是谁叫醒的」
            # 会正确记成 human_manual，也能穿透睡眠（人有权把人拽起来）。
            if runtime.wake is not None:
                runtime.wake.request_wake("human_manual", "人类在面板上点了立即唤醒")
            runtime.wake_event.set()
            runtime.bus.emit("system", message="人类触发了一次立即唤醒")
            return {"ok": True}
        if action == "sleep_toggle":
            if runtime.wake is None:
                raise HTTPException(404, "唤醒模块未启用")
            if (runtime.wake.sleep or {}).get("sleeping"):
                await runtime.wake.leave_sleep("人类在面板上把她叫醒了")
                return {"ok": True, "sleeping": False}
            await runtime.wake.enter_sleep("人类在面板上让她去睡")
            return {"ok": True, "sleeping": True}
        if action == "snapshot":
            try:
                await runtime.hub.call("paper__snapshot_equity")
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            _cache.pop("curve", None)
            return {"ok": True}
        raise HTTPException(404, f"未知操作 {action}")

    @app.post("/api/instruct")
    async def instruct(payload: dict):
        text = str((payload or {}).get("text") or "").strip()
        if not text:
            raise HTTPException(400, "指令为空")
        runtime.agent.instruct(text)
        runtime.wake_event.set()
        return {"ok": True}

    # ------------------------------------------------------------------ 工作区
    @app.get("/api/workspace")
    async def workspace(path: str = ""):
        base = os.path.realpath(cfg.workspace_dir)
        target = os.path.realpath(os.path.join(base, (path or "").lstrip("/")))
        if target != base and not target.startswith(base + os.sep):
            raise HTTPException(403, "路径越界")
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                return {"type": "file", "path": os.path.relpath(target, base), "content": f.read(60000)}
        if not os.path.isdir(target):
            return {"type": "missing", "path": path}
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            entries.append({
                "name": name,
                "dir": os.path.isdir(full),
                "size": 0 if os.path.isdir(full) else os.path.getsize(full),
                "mtime": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(full))),
            })
        return {"type": "dir", "path": os.path.relpath(target, base) if target != base else "", "entries": entries}

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "agent": runtime.agent.status(),
            "event_seq": runtime.bus.seq,
        }

    # ------------------------------------------------------------------ 公开档
    #
    # 以下五条是**匿名可达**的全部接口（`events` 与 `events/stream` 算两条）。它们都是从上面那几个私有端点派生的，
    # 差别只在"出口处削了什么"。削的动作全部集中在这里，不散在业务逻辑里 ——
    # 想让某一项也公开，改的是这里的白名单，不是给它开个新口子。
    _streams = PublicStreamLimiter(cfg.public_max_streams)

    @app.get(PUBLIC_PREFIX + "events")
    async def public_events(limit: int = 200):
        cap = max(1, int(cfg.public_events_max or 0) or 1)
        n = max(1, min(int(limit or 200), cap))
        return {"events": [_public_event(e) for e in runtime.bus.history(n)]}

    @app.get(PUBLIC_PREFIX + "events/stream")
    async def public_stream():
        """匿名实时流。和私有那条同一份事件源，出口处脱敏，并且**有并发上限**。

        上限是这一档里唯一"会拒绝人"的东西：一路 SSE 就是一个常驻连接，
        没有它的话，一个脚本就能把整个公开页拖垮（而它并不花 token，
        所以从成本那侧完全看不出来）。
        """
        tok = _streams.open()
        if tok is None:
            return JSONResponse(
                {"detail": f"公开日志流已满（上限 {cfg.public_max_streams} 路），稍后再来"},
                status_code=503,
            )

        async def gen():
            q = runtime.bus.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield ("data: " + json.dumps(_public_event(ev), ensure_ascii=False,
                                                     default=str) + "\n\n")
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                runtime.bus.unsubscribe(q)
                _streams.close(tok)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get(PUBLIC_PREFIX + "market")
    async def public_market():
        data = await market()
        for row in (data.get("symbols") or []):
            # `ref` 是唤醒策略里人类阈值的基准价，`dev_from_ref_pct` 是它和现价的
            # 距离 —— 这两个数合起来等于把阈值反过来算出来。行情给，阈值不给。
            row.pop("ref", None)
            row.pop("dev_from_ref_pct", None)
        return data

    @app.get(PUBLIC_PREFIX + "state")
    async def public_state():
        """账户/权益/绩效/成交 —— 给，风控参数不给。

        `_cached` 和私有那份是同一个缓存，所以游客刷面板**不会**让账本多挨一次读
        （并且现在还有 single-flight 兜着同一瞬间的并发）。
        """
        full = await state()
        return {
            "status": _public_status(full.get("status") or {}),
            "account": full.get("account"),
            "equity_curve": full.get("equity_curve"),
            "performance": full.get("performance"),
            "trades": full.get("trades"),
        }

    return app
