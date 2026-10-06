#!/usr/bin/env python3
"""`/api/market` 的行为测试：假 runtime + `httpx.ASGITransport`，真打这个端点。

# 它为什么存在

2026-09-27 用户报：「打开面板，实时行情一直显示『等待取价……』」。
查出来是**两个独立的毛病叠在一起**：

  1. 前端 `renderMarket()` 里按标签名去找元素（少了 `#`）。`$` 就是
     `document.querySelector`，没有 `#` 就按标签名找，页面上没有 `<mkBox>`
     这种标签 → `null` → 函数第三行就 return。数据每次都取回来了，
     只是没人把它画出来。这一条现在由 test_frontend.py 的 [3b] 守。

  2. 后端把这个端点写成了「先等 24h 统计，再一起返回」。而 MCP hub 是
     **单任务串行**消费队列的（`mcp_hub._supervisor`），一次心跳跑着的时候，
     面板那几个请求排在心跳的工具调用后面 —— 于是"等 24h"= 等心跳。
     冷缓存时这个等待是**坐在请求路径上**的，前端再快也没用。

这一份测的是第 2 条：**24h 统计永远不许挡住返回。**

# 为什么用假 runtime 而不是起整个服务

这个端点的逻辑全在「数据从哪来」和「愿不愿意等」上，跟真交易所、真账本
没关系。假 runtime 能让"取数很慢"变成一个**可复现的输入**（下面 sleep 5 秒），
而这正是线上最难复现、也最要命的那个条件。不联网，不花钱。

跑法：bash runtests.sh web
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

FAILS: list[str] = []


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


# ---------------------------------------------------------------------- 假件
class FakeCfg:
    webui_user = ""          # 空 = 不开 BasicAuth，测试里不用带凭证
    webui_password = ""
    static_dir = "/tmp"
    watchlist = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    # 公开只读档那三个（2026-09-27 加）。这一份假件里用不到它们 ——
    # 但只要 create_app 读得到，就得在这儿存在，否则整个 app 装不起来。
    # 公开档自己的行为在 test_public.py 里测。
    public_log_page = True
    public_max_streams = 40
    public_events_max = 400


class FakeWake:
    """唤醒控制器里被 /api/market 用到的那几样。"""

    def __init__(self) -> None:
        self.last_prices = {"BTCUSDT": 84349.79, "ETHUSDT": 2693.41,
                            "SOLUSDT": 121.23, "BNBUSDT": 773.01}
        # refs 故意只给两个：面板要能显示 null（"还没参考价"），而不是编一个 0
        self.refs = {"BTCUSDT": 84386.01, "ETHUSDT": 2694.76}

    def watched_symbols(self) -> list[str]:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

    def prices_age_seconds(self) -> float:
        return 12.3

    def effective(self) -> dict:
        return {"poll_seconds": 30}


class FakeBus:
    """只提供 history() —— 事件总线里被这两个端点用到的那一样。"""

    def __init__(self, events: list | None = None) -> None:
        self._events = events or []

    def history(self, limit: int = 300) -> list:
        return list(self._events)[-max(1, limit):]


class FakeRuntime:
    def __init__(self, ticker_delay: float = 0.0, boom: bool = False,
                 events: list | None = None) -> None:
        self.cfg = FakeCfg()
        self.wake = FakeWake()
        self.hub = None
        self.bus = FakeBus(events)
        self.wake_event = asyncio.Event()
        self.ticker_delay = ticker_delay
        self.boom = boom
        self.ticker_calls = 0

    async def hub_json(self, name: str, args: dict | None = None):
        self.ticker_calls += 1
        if self.boom:
            raise RuntimeError("MCP 断了")
        await asyncio.sleep(self.ticker_delay)   # 故意很慢：模拟排在心跳后面
        return {"symbol": (args or {}).get("symbol"), "last": 1.0,
                "change_pct_24h": 0.5, "high_24h": 2.0, "low_24h": 0.5,
                "volume_quote": 3.0}


async def main() -> int:
    web = _load_web()
    print("=" * 68)
    print("面板行情端点 · /api/market（假 runtime，不联网）")
    print("=" * 68)

    import httpx

    # 取数故意慢 5 秒。线上的真实值是几十毫秒（实测 4 个币 ≈ 100ms），
    # 但心跳跑着的时候它会排在队列后面 —— 5 秒是在模拟那种情况。
    rt = FakeRuntime(ticker_delay=5.0)
    app = web.create_app(rt)
    transport = httpx.ASGITransport(app=app)

    print("\n[1] 冷缓存：第一次请求不许等那个慢取数")
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        t0 = time.perf_counter()
        r = await c.get("/api/market")
        took = time.perf_counter() - t0
        check("返回 200", r.status_code == 200, f"实际 {r.status_code}")
        check(f"没有被 5 秒的取数挡住（实测 {took * 1000:.0f}ms）", took < 0.5,
              "说明这条路径上还有 await —— 面板会跟着心跳一起卡")
        j = r.json()
        syms = [x["symbol"] for x in j.get("symbols", [])]
        check("四个币都在，顺序按 watchlist",
              syms == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"], f"实际 {syms}")
        check("现价走的是 30 秒轮询那份",
              all(x["source"] == "poll" for x in j["symbols"]),
              f"实际 {[x['source'] for x in j['symbols']]}")
        check("取价年龄原样透出", j.get("age_seconds") == 12.3,
              f"实际 {j.get('age_seconds')}")
        check("tickers_fresh=False（24h 还空着，后台正在补）",
              j.get("tickers_fresh") is False, f"实际 {j.get('tickers_fresh')}")
        check("24h 那几列先给 null，不编数",
              all(x["change_pct_24h"] is None for x in j["symbols"]))
        check("没参考价的币给 null（不是 0）",
              j["symbols"][2]["dev_from_ref_pct"] is None
              and j["symbols"][0]["dev_from_ref_pct"] is not None)

        print("\n[2] 高频轮询不会把取数打成一堆")
        for _ in range(5):
            await c.get("/api/market")
        check("连打 6 次只发起了一批取数（4 个币，不是每请求一批）",
              rt.ticker_calls == 4, f"实际 {rt.ticker_calls} 次")

        print("\n[3] 后台把 24h 补上了，下一次请求就带着它")
        # 后台那一批要跑完它那 5 秒（真实情况是几十毫秒）。
        # 这里老实轮询到它变新鲜，而不是 sleep 一个拍脑袋的数字然后赌。
        for _ in range(80):
            await asyncio.sleep(0.25)
            j = (await c.get("/api/market")).json()
            if j.get("tickers_fresh"):
                break
        check("tickers_fresh=True", j.get("tickers_fresh") is True)
        check("24h 涨跌补上了",
              all(x["change_pct_24h"] == 0.5 for x in j["symbols"]),
              f"实际 {[x['change_pct_24h'] for x in j['symbols']]}")
        check("24h 高低/成交额也补上了",
              all(x["high_24h"] == 2.0 and x["volume_quote"] == 3.0 for x in j["symbols"]))
        check("现价优先用轮询那份（84,349.79，不是 24h 兜底的 1.0）",
              j["symbols"][0]["price"] == 84349.79, f"实际 {j['symbols'][0]['price']}")
        check("距参考价是相对 ref 算的",
              abs(j["symbols"][0]["dev_from_ref_pct"] - (-0.043)) < 0.01,
              f"实际 {j['symbols'][0]['dev_from_ref_pct']}")
        check("took_ms 透出来了（排查用）",
              isinstance(j.get("took_ms"), int) and j["took_ms"] < 500,
              f"实际 {j.get('took_ms')}")

    print("\n[4] 取数整个坏掉：现价照样在，24h 空着，端点不报错")
    rt2 = FakeRuntime(boom=True)
    app2 = web.create_app(rt2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),
                                 base_url="http://t") as c:
        r = await c.get("/api/market")
        j = r.json()
        check("仍然 200", r.status_code == 200, f"实际 {r.status_code}")
        check("四个币的现价都在",
              [x["symbol"] for x in j["symbols"]] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
        check("现价还是轮询那份", all(x["source"] == "poll" for x in j["symbols"]))
        check("坏掉的那部分只是留空",
              all(x["change_pct_24h"] is None for x in j["symbols"]))

    print("\n[5] 叙事层预览：不发请求、不写账本，但走同一条装配路径")
    # 叙事层是全系统最贵、最不透明的一次调用（产出要写进 append-only 的手账）。
    # 这个端点存在的意义是"能在发生之前先看一眼素材" —— 2026-09-27 那篇断在
    # 「SOL 破 124 或」的日志，当时靠的是人肉读句子断没断。
    class FakeNarrator:
        enabled = True

        def __init__(self):
            import types as _t
            self.cfg = _t.SimpleNamespace(narrator_effective_model="M", narrator_max_tokens=8192)
            self.called = 0
            self.truncated_count = 1
            self.last_truncated = {"at": "02:57", "beat": 1, "chars": 450, "model": "M"}

        def preview(self, events, beat=1):
            self.called += 1
            return {"empty": False, "system_chars": 11, "user_chars": 22,
                    "total_chars": 33, "system": "s" * 11, "user": "u" * 22}

        def should_narrate(self, events, beats=1):
            return True

    fake_events = [{"id": i, "ts": "2026-09-27 08:00:00", "kind": "beat_start", "data": {}}
                   for i in range(1, 5)]
    rt3 = FakeRuntime(ticker_delay=0.01, events=fake_events)
    rt3.agent = type("A", (), {"narrator": FakeNarrator(), "_last_narrated_id": 2, "beats": 3})()
    app3 = web.create_app(rt3)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app3),
                                base_url="http://t") as c:
        j = (await c.get("/api/narrator/preview")).json()
        check("返回 enabled / 模型 / 预算", j.get("enabled") is True
              and j.get("model") == "M" and j.get("max_tokens") == 8192, repr(j)[:120])
        check("带上了这次的素材正文（system / user）",
              j.get("system") and j.get("user"), repr(j)[:120])
        check("说明这份素材是哪儿来的（自上次叙事以来的事件）",
              j.get("since_event_id") == 2 and j.get("events_considered") == 2,
              "since=%s considered=%s" % (j.get("since_event_id"), j.get("events_considered")))
        check("说了这一轮到底会不会写（would_narrate）", "would_narrate" in j)
        check("把截断计数透出来（出过事要看得到）",
              j.get("truncated_count") == 1 and j.get("last_truncated"), repr(j)[:160])
        check("允许显式指定 beat（面板点一下就能预览第 N 轮）",
              (await c.get("/api/narrator/preview?beat=9")).status_code == 200)
        # 没配叙事层时不许 500 —— 它是个可选的装饰性功能
        rt4 = FakeRuntime(ticker_delay=0.01)
        rt4.agent = type("A", (), {"narrator": None, "_last_narrated_id": 0, "beats": 1})()
        j4 = (await httpx.AsyncClient(transport=httpx.ASGITransport(app=web.create_app(rt4)),
                                     base_url="http://t").get("/api/narrator/preview")).json()
        check("叙事层没启用时返回 enabled=False（不是 500）", j4.get("enabled") is False)

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
    sys.exit(asyncio.run(main()))
