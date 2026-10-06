#!/usr/bin/env python3
"""叙事层的纪律测试 —— 纯逻辑，不联网，不花钱。

考三件事，都是"错了会静默出事"的那种：

  1. **单向不变量**：叙事层的输出只经过 bus.emit，绝不进操盘上下文。
     这是整个设计的命门。靠人肉读代码守不住，得让测试守。
  2. **素材预算**：不管窗口多长，素材不能超上限，而且**最近一轮不能被砍**。
     （软约束会溢出，溢出后的截断砍的正好是尾部 = 最近那轮 = 最该看的内容。）
  3. **触发口径**：有事立刻写；没事攒够时间也写；没事又没攒够就不写。
     cost_nudge 不许当触发源 —— 那是踩过的坑。

跑法：  bash runtests.sh narrator
或者：  python3 test_narrator.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import pathlib
import tempfile
import importlib.util

ROOT = pathlib.Path(__file__).resolve().parent


def _find_trader() -> pathlib.Path:
    """定位 trader 包，兼容两种布局：

      · 直接在仓库里跑        -> <仓库>/brain/trader/
      · 被 runtests.sh 拷过去 -> /t/trader/   （脚本和源码平铺在一起）

    之前这里写死了 brain/trader，所以经由 runtests.sh 跑的时候找不到文件。
    """
    for cand in (ROOT / "brain" / "trader", ROOT / "trader"):
        if (cand / "narrator.py").is_file():
            return cand
    raise SystemExit(f"找不到 trader 包（找过 {ROOT}/brain/trader 和 {ROOT}/trader）")


# ---------------------------------------------------------------- 极简 Config
class FakeCfg:
    """只提供 narrator 需要的字段，避免真的去读环境变量。"""

    def __init__(self, **kw):
        self.narrator_enabled = True
        self.narrator_max_gap = 6 * 3600
        self.narrator_base_url = ""
        self.narrator_api_key = ""
        self.narrator_model = ""
        self.narrator_temperature = 0.85
        self.narrator_max_tokens = 2000
        self.narrator_timeout = 120
        self.narrator_min_chars = 150
        self.narrator_max_chars = 600
        self.narrator_character_file = ""
        self.narrator_journal_file = ""
        self.narrator_journal_excerpt = 1200
        self.llm_base_url = "http://x/v1"
        self.llm_api_key = "k"
        self.llm_model = "m"
        self.llm_temperature = 0.3
        self.llm_max_tokens = 4096
        self.llm_timeout = 60
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def narrator_configured(self):
        return self.narrator_enabled

    @property
    def narrator_effective_model(self):
        return self.narrator_model or self.llm_model


FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


# ---------------------------------------------------------------- 造事件
def ev(i: int, kind: str, **data) -> dict:
    return {"id": i, "ts": "2026-09-27 00:00:00", "kind": kind, "data": data}


def quiet_window(n_beats: int, text_len: int = 900, start_id: int = 1):
    """造 n_beats 轮"什么都没发生"的心跳。"""
    out, i = [], start_id
    for b in range(1, n_beats + 1):
        out.append(ev(i, "beat_start", trigger="schedule", beat=b))
        i += 1
        out.append(ev(i, "tool_call", tool="market__get_market_overview", args="{}", step=1))
        i += 1
        out.append(ev(i, "tool_result", tool="market__get_market_overview", result="{}", step=1))
        i += 1
        out.append(ev(i, "agent_text", text=f"第{b}轮：什么都没发生。" + "磨。" * (text_len // 2), step=1))
        i += 1
        out.append(ev(i, "beat_summary", text=f"第{b}轮：什么都没发生。" + "磨。" * (text_len // 2), duration=200, ok=True))
        i += 1
    return out


TRADER_DIR = _find_trader()
NARRATOR_PATH = TRADER_DIR / "narrator.py"


def main() -> int:
    print("=" * 68)
    print(" 叙事层纪律测试")
    print("=" * 68)

    # 用假的 config 模块骗过 narrator 的 `from .config import Config`
    # （narrator 只在类型注解里用到 Config，运行时不需要真的）
    if not NARRATOR_PATH.is_file():
        print(f"找不到 {NARRATOR_PATH}")
        return 1

    # narrator.py 里有 `from .config import Config` / `from .llm import ...` 的相对导入，
    # 所以必须当成包内模块加载。做法：
    #   1. 把 brain/ 加进 sys.path
    #   2. 用一个**假的 trader 包**占住名字，这样真正的 trader/__init__.py 不会被执行
    #   3. 把 config 和 llm 换成假的 —— 测试不该碰真实环境变量，也不该联网
    trader_dir = TRADER_DIR.parent
    sys.path.insert(0, str(trader_dir))

    import types
    fake_pkg = types.ModuleType("trader")
    fake_pkg.__path__ = [str(trader_dir / "trader")]
    sys.modules["trader"] = fake_pkg

    cfg_mod = types.ModuleType("trader.config")
    cfg_mod.Config = FakeCfg
    sys.modules["trader.config"] = cfg_mod

    # llm.py 依赖 openai SDK；测试里不需要真模型，塞个占位
    llm_mod = types.ModuleType("trader.llm")

    class LLMError(RuntimeError):
        pass

    class LLM:
        def __init__(self, cfg):
            self.cfg = cfg

        async def chat(self, messages, tools=None):
            raise LLMError("测试里不联网")

    llm_mod.LLM = LLM
    llm_mod.LLMError = LLMError
    sys.modules["trader.llm"] = llm_mod

    import importlib
    narrator_mod = importlib.import_module("trader.narrator")
    Narrator = narrator_mod.Narrator

    # ============================================================ 1. 单向不变量
    print("\n[1] 单向不变量：叙事层碰不到操盘上下文")
    agent_src = (TRADER_DIR / "agent.py").read_text()

    # 找到 _narrate 这个函数体
    start = agent_src.index("async def _narrate(")
    end = agent_src.index("\n    async def ", start + 10) if "\n    async def " in agent_src[start + 10:] else len(agent_src)
    body = agent_src[start:end]

    check("_narrate 里确实调用了 bus.emit(\"narrative\"", 'bus.emit("narrative"' in body)
    check("_narrate 里**没有** append 进 messages",
          "messages.append" not in body and "messages +=" not in body,
          "★ 叙事结果进了操盘上下文，单向性被破坏")
    check("_narrate 的返回值没有外泄给调用方",
          "return result" not in body,
          "★ 返回值被传出去了，可能被别人塞进上下文")

    # 全文件扫一遍：有没有把 narrative 塞回 messages 的地方
    check("全文件没有把 narrative 事件塞回 messages",
          "narrative" not in agent_src.replace(
              'self.bus.emit("narrative", **result)', "").replace(
              '"""叙事层在本项目里', ""),
          "★ 除了 emit 之外还有别的 narrative 引用")

    check("叙事层异常被就地吞掉（不会弄挂心跳）",
          "except Exception as exc" in body and "return" in body)

    # ============================================================ 2. 素材预算
    print("\n[2] 素材预算：不超上限，且最近一轮不被砍")
    with tempfile.TemporaryDirectory() as td:
        jf = os.path.join(td, "journal.md")
        n = Narrator(FakeCfg(narrator_journal_file=jf))

        MAX = narrator_mod.MAX_DIGEST_CHARS
        LAST_BUDGET = narrator_mod._LAST_BEAT_BUDGET

        for label, nbeats in [("单轮（事件触发）", 1), ("6 小时窗口", 24), ("24 小时窗口", 96)]:
            evs = quiet_window(nbeats)
            d = n._digest(evs)
            ok_len = len(d) <= MAX
            # 最近一轮的正文有没有被挤掉
            tail_marker = f"第{nbeats}轮"
            ok_tail = tail_marker in d
            check(f"{label}: 素材 {len(d)} 字符 ≤ {MAX}", ok_len)
            check(f"{label}: 最近一轮完整保留", ok_tail, f"找不到 {tail_marker}")

        # 有事件时，事件不许被挤掉
        evs = quiet_window(24) + [
            ev(9001, "trade", symbol="SOLUSDT", side="BUY", qty=41.3, fill_price=121.85,
               notional=5032.0, fee=5.03, reason="放量突破"),
            ev(9002, "human_instruction", text="别碰山寨币"),
        ]
        d = n._digest(evs)
        check("有事件时成交保留", "[成交] BUY SOLUSDT" in d)
        check("有事件时留言保留", "别碰山寨币" in d)
        check("有事件时总长仍在上限内", len(d) <= MAX, f"实际 {len(d)}")

        # 后台止损要带标记
        d2 = n._digest([
            ev(1, "beat_start", trigger="schedule", beat=5),
            ev(2, "agent_text", text="醒来看看。", step=1),
            ev(3, "trade", symbol="BTCUSDT", side="SELL", qty=0.01, fill_price=82400,
               notional=824.0, realized_pnl=-25.4, reason="[AUTO] 触发止损", auto=True),
        ])
        check("后台止损被标为自动（不许写成它自己下的单）",
              "后台自动触发" in d2)

    # ============================================================ 3. 触发口径
    print("\n[3] 触发口径")
    with tempfile.TemporaryDirectory() as td:
        jf = os.path.join(td, "journal.md")
        n = Narrator(FakeCfg(narrator_journal_file=jf, narrator_max_gap=6 * 3600))

        # 刚写过，且没有事件 → 不写
        n._last_at = time.time()
        check("没事 + 刚写过 → 不写",
              not n.should_narrate(quiet_window(1)))

        # 攒够 6 小时，仍然没事 → 要写
        n._last_at = time.time() - 6 * 3600 - 1
        check("没事 + 攒满 6 小时 → 写（'为什么不动'本身就是内容）",
              n.should_narrate(quiet_window(24)))

        # 有事 → 立刻写，不等计时器
        n._last_at = time.time()
        with_trade = [ev(1, "beat_start", trigger="schedule", beat=1),
                      ev(2, "trade", symbol="ETHUSDT", side="BUY", qty=1, fill_price=2689)]
        check("有事 + 刚写过 → 立刻写", n.should_narrate(with_trade))

        # cost_nudge 不许当触发源
        n._last_at = time.time()
        with_nudge = [ev(1, "beat_start", trigger="schedule", beat=1),
                      ev(2, "cost_nudge", text="本轮你已经自己取了 4 次行情", calls=4)]
        check("cost_nudge 不触发（踩过的坑：它每轮都响）",
              not n.should_narrate(with_nudge))

        # skipped 也不许
        with_skip = [ev(1, "beat_start", trigger="schedule", beat=1),
                     ev(2, "skipped", reason="不在活跃时段")]
        check("skipped 不触发", not n.should_narrate(with_skip))

        # 关掉定时（gap=0）= 每轮都写，旧 always 语义
        n2 = Narrator(FakeCfg(narrator_journal_file=jf, narrator_max_gap=0))
        n2._last_at = time.time()
        check("gap=0 时恢复'每轮都写'语义", n2.should_narrate(quiet_window(1)))

    # ============================================================ 4. 跨重启时间恢复
    print("\n[4] 跨重启：从手账把上次时间读回来")
    with tempfile.TemporaryDirectory() as td:
        jf = os.path.join(td, "journal.md")
        pathlib.Path(jf).write_text(
            "\n## beat #3 · 2026-09-27 03:00\n\n第三篇。\n"
            "\n## beat #7 · 2026-09-27 06:30\n\n第七篇。\n",
            encoding="utf-8",
        )
        n = Narrator(FakeCfg(narrator_journal_file=jf))
        expect = time.mktime(time.strptime("2026-09-27 06:30", "%Y-%m-%d %H:%M"))
        check("取的是最后一条而不是第一条", abs(n._last_at - expect) < 60,
              f"拿到 {time.strftime('%m-%d %H:%M', time.localtime(n._last_at))}")
        check("刚重启完不会立刻又写一篇",
              not n.should_narrate(quiet_window(1)))

        # 手账不存在时不能炸
        n3 = Narrator(FakeCfg(narrator_journal_file=os.path.join(td, "nope.md")))
        check("手账不存在时安静降级", n3._last_at == 0.0)

    # ============================================================ 4b. 留言不被重复计入
    print("\n[4b] 人类留言只算一次")
    check("agent.py 里不再有 _beat_pending（那会重复计入）",
          "_beat_pending" not in agent_src,
          "★ 留言会同时以事件和内存副本进入叙事素材")
    check("留言靠事件流传递（instruct 会发 human_instruction 事件）",
          'self.bus.emit("human_instruction"' in agent_src)

    # ============================================================ 5. 配置口径
    print("\n[5] 配置")
    cfg_src = (TRADER_DIR / "config.py").read_text()
    # 注意别用子串匹配：NARRATOR_MODEL 里就含 NARRATOR_MODE
    check("旧的 NARRATOR_MODE 已移除", '"NARRATOR_MODE"' not in cfg_src)
    check("旧的 NARRATOR_MIN_INTERVAL 已移除", '"NARRATOR_MIN_INTERVAL"' not in cfg_src)
    check("新配置 NARRATOR_MAX_GAP 存在", '"NARRATOR_MAX_GAP"' in cfg_src)
    check("NARRATOR_MAX_TOKENS 的默认值是 8192（思考型模型需要）",
          '_i("NARRATOR_MAX_TOKENS", 8192)' in cfg_src, cfg_src[cfg_src.index("NARRATOR_MAX_TOKENS"):][:80])

    # ============================================================ 6. 模型禁忌
    print("\n[6] 叙事层模型禁忌（Operator 定的硬规矩）")
    NARR_SRC = (TRADER_DIR / "narrator.py").read_text()
    advisor = narrator_mod.model_advisor
    is_thinking = narrator_mod.is_thinking_model

    a = advisor("deepseek-v4.1-flash", 8192)
    check("DeepSeek 接文字活儿 → error（要吵，不是静默）",
          a is not None and a[0] == "error", str(a))
    check("理由里点名「文笔 / 文科」", a is not None and ("文笔" in a[1] or "文科" in a[1]), str(a))
    for bad in ("deepseek-v4.1-flash", "DeepSeek-V3", "deepseek-r1", "my-deepseek-mirror"):
        check(f"{bad} 都被拦（大小写与别名都要认）",
              (advisor(bad, 8192) or ("", ""))[0] == "error", bad)
    check("换成 Gemini 放行（别误伤）",
          advisor("Gemini-3.8-Flash-Thinking/Antigravity", 8192) is None)

    # 这一条对应的是一次真实的截断事故：思考吃掉 1922，正文 94 字就被切了
    a = advisor("Gemini-3.8-Flash-Thinking/Antigravity", 2000)
    check("思考型 + 预算 2000 → error", a is not None and a[0] == "error", str(a))
    check("理由里点名「截断」", a is not None and "截断" in a[1], str(a))
    check("4096 仍算不够（实测思考一个人吃掉 1922，4096 只剩一半余量）",
          (advisor("Gemini-3.8-Flash-Thinking/Antigravity", 4096) or (None,))[0] == "error")
    check("8192 才放行",
          advisor("Gemini-3.8-Flash-Thinking/Antigravity", 8192) is None)
    check("非思考模型配 2000 不误报", advisor("glm-5.3-fast", 2000) is None)
    check("没配模型 → warn（回落主模型，不算错）",
          (advisor("", 8192) or ("",))[0] == "warn")
    check("is_thinking_model 认得出常见思考型，且不误判普通模型",
          all(is_thinking(x) for x in ("Gemini-3.8-Flash-Thinking/Antigravity",
                                       "o3-mini", "deepseek-r1", "QwQ-32B"))
          and not is_thinking("glm-5.3-fast"))

    # 体检结论必须能到面板上 —— 只打在日志里等于没打
    print("\n[6b] 体检结论要能到面板")
    check("Narrator.status() 里有 advice 字段（面板要显示，不是只打日志）",
          '"advice"' in NARR_SRC)
    check("status() 里有 thinking 标记（面板能看出这是思考型）", '"thinking"' in NARR_SRC)
    check("__init__ 里真的跑了体检",
          "model_advisor(cfg.narrator_effective_model" in NARR_SRC)
    check("error 级别走 log.error，不是 info（静默降级等于没有）",
          "log.error if lvl" in NARR_SRC)
    check("模块 docstring 里写了这条规矩（读代码的人第一眼就该看到）",
          "不许用 DeepSeek" in NARR_SRC)
    check("PROSE_FORBIDDEN 里确实有 deepseek",
          'PROSE_FORBIDDEN = ("deepseek",)' in NARR_SRC)
    print("     （线上实际值：跑 `bash status.sh`，或看面板顶栏 token 胶囊的 tooltip）")

    # ============================================================ 7. 截断纪律
    print("\n[7] 被 max_tokens 截断时的纪律（2026-09-27 那篇断在「SOL 破 124 或」）")
    #
    # 那次事故的确切形状：`llm.chat()` 一直在返回 finish_reason，而 narrate()
    # **拿到了却没用**。于是"这一篇是不是被截断的"在事件流里完全看不出来，
    # HANDOVER 里那条待办「要看 finish_reason ≠ length」根本无从看起，
    # 而半句话已经进了 append-only 的手账。
    #
    # 这一节端到端跑 narrate()：给 Narrator 塞一个假 LLM（`_llm` 是内部字段，
    # property 只在它为 None 时才构造真客户端），于是**不联网、不花钱**。

    class FakeLLM:
        def __init__(self, content, finish_reason="stop", usage=None):
            self.content = content
            self.finish_reason = finish_reason
            self.usage = usage if usage is not None else {"total_tokens": 123}
            self.seen_messages = None

        async def chat(self, messages, tools=None):
            self.seen_messages = messages
            return {"content": self.content, "reasoning": "", "tool_calls": [],
                    "finish_reason": self.finish_reason, "usage": self.usage}

    def run_narrate(narr, events, beat=1):
        return asyncio.run(narr.narrate(events, beat))

    with tempfile.TemporaryDirectory() as td:
        # ---- ① 正常收尾 ----
        jf = os.path.join(td, "j1.md")
        n1 = Narrator(FakeCfg(narrator_journal_file=jf))
        ok_reply = "盘面很闷，我没动手。BTC 还在那个箱子里。" * 6
        n1._llm = FakeLLM(ok_reply, finish_reason="stop")
        r1 = run_narrate(n1, quiet_window(1))
        check("正常收尾时 truncated=False", r1 and r1.get("truncated") is False, repr(r1))
        check("正常收尾时 finish_reason 照样带上（别只在出问题时才有字段）",
              r1 and r1.get("finish_reason") == "stop", repr(r1))
        body1 = open(jf, encoding="utf-8").read()
        check("正常的那篇进了手账", "盘面很闷" in body1, body1[:120])
        check("正常的那篇没有「没写进手账」那行", "没写进手账" not in body1)
        check("手账标题带 beat 与时间（重启后靠它恢复节奏）",
              "## beat #1 · " in body1, body1[:60])

        # ---- ② 被截断 ----
        jf2 = os.path.join(td, "j2.md")
        n2 = Narrator(FakeCfg(narrator_journal_file=jf2))
        cut_reply = "睡前就是看这个才去睡的。BTC 站上 84,600 或者跌破 83,300，SOL 破 124 或"
        n2._llm = FakeLLM(cut_reply, finish_reason="length", usage={"total_tokens": 5319})
        r2 = run_narrate(n2, quiet_window(1))
        check("截断时 truncated=True", r2 and r2.get("truncated") is True)
        check("截断时 finish_reason 原样带出去（面板靠它显示 ✂）",
              r2 and r2.get("finish_reason") == "length")
        check("截断被计数（status() 会把它给面板）", n2.truncated_count == 1)
        check("last_truncated 里有时间/字数/模型",
              bool(n2.last_truncated) and n2.last_truncated.get("chars") == len(cut_reply)
              and n2.last_truncated.get("beat") == 1, repr(n2.last_truncated))
        body2 = open(jf2, encoding="utf-8").read()
        check("★ 半截正文**没有**进手账",
              "SOL 破 124 或" not in body2, body2[:160])
        check("但留了一行说明（事实不能丢）", "没写进手账" in body2 and "max_tokens" in body2)
        check("那一行仍然带 beat 标题（节奏锚点不能丢，否则重启就重写一篇）",
              "## beat #1 · " in body2)
        check("截断也会推进 _last_at（否则每一轮都会重写一篇断的）",
              n2._last_at > 0)
        check("截断走 log.error 而不是 info（静默降级等于没有）",
              'log.error(\n                    "叙事层这一篇被 max_tokens 截断了' in NARR_SRC)

        # ---- ②b 读侧：喂回模型的那份要标出"最后一段是截断的" ----
        # 这一段是 preview() 逼出来的：第一次把素材打出来看，就发现
        # 手账尾部那半句「…SOL 破 124 或」跟着素材进了提示词，
        # 而紧随其后的指令是「现在直接写下这篇手账」。
        # 对模型的杀伤力比对人更大 —— 断在半句的"范文"有可能被学成一种写法。
        jf_cut = os.path.join(td, "j_cut.md")
        with open(jf_cut, "w", encoding="utf-8") as f:
            f.write("\n## beat #1 · 2026-09-27 02:57\n\n"
                    "睡前就是看这个才去睡的。BTC 站上 84,600 或者跌破 83,300，SOL 破 124 或\n")
        nc = Narrator(FakeCfg(narrator_journal_file=jf_cut))
        tail = nc._journal_tail()
        check("手账尾部是残句时，读回来会带一句「不要照学」",
              "不要照学" in tail and "截断" in tail, repr(tail[-90:]))
        check("★ 这一句只加在**读侧**（账本本身没被动过）",
              "不要照学" not in open(jf_cut, encoding="utf-8").read(),
              "★ 往账本里写注释 = 动了 append-only 的历史")

        jf_ok = os.path.join(td, "j_ok.md")
        with open(jf_ok, "w", encoding="utf-8") as f:
            f.write("\n## beat #1 · 2026-09-27 02:57\n\n盘面很闷，我没动手，继续等。\n")
        check("正常收尾的手账不会被加注（宁可少说，不许乱说）",
              "不要照学" not in Narrator(FakeCfg(narrator_journal_file=jf_ok))._journal_tail())
        with open(jf_ok, "a", encoding="utf-8") as f:
            f.write("\n***\n")
        check("以分隔线收尾的也算收住了", "不要照学" not in Narrator(FakeCfg(narrator_journal_file=jf_ok))._journal_tail())
        # 白名单是常量，前端 index.html 的 CUT_TAIL_OK 必须是同一串
        # （test_frontend.py 的 [11b] 负责比对两边，这里只确认它确实被抽出来了）
        check("白名单是模块级常量（前端要能对得上）",
              isinstance(narrator_mod.TAIL_OK_CHARS, str)
              and "。" in narrator_mod.TAIL_OK_CHARS and "*" in narrator_mod.TAIL_OK_CHARS,
              repr(getattr(narrator_mod, "TAIL_OK_CHARS", None)))

        # ---- ③ preview()：不联网、不写账，但走同一条装配路径 ----
        jf3 = os.path.join(td, "j3.md")
        n3 = Narrator(FakeCfg(narrator_journal_file=jf3))
        pv = n3.preview(quiet_window(1), beat=1)
        check("preview 不发请求也拿得到全文",
              not pv.get("empty") and pv.get("system") and pv.get("user"))
        check("preview 会告诉你两段的字数（预算够不够一眼看到）",
              pv["system_chars"] == len(pv["system"]) and pv["total_chars"] > 100)
        check("preview **不写手账**（否则预览一次就污染一次账本）",
              not os.path.exists(jf3))
        check("preview 不碰 _last_at（预览不算写过）", n3._last_at == 0)
        check("素材为空时 preview 明确说 empty",
              n3.preview([], 1).get("empty") is True)

        # ★ 这条是这一节最要紧的一条：preview 和真发出去的那份**必须是同一份**。
        #   否则"预览"会变成另一个真相，比没有预览更坏。
        n4 = Narrator(FakeCfg(narrator_journal_file=os.path.join(td, "j4.md")))
        llm4 = FakeLLM("随便写点。" * 20, finish_reason="stop")
        n4._llm = llm4
        pv4 = n4.preview(quiet_window(1), beat=7)
        run_narrate(n4, quiet_window(1), beat=7)
        check("preview 与真发出去的那份完全一致（同一条装配路径）",
              llm4.seen_messages is not None
              and llm4.seen_messages[0]["content"] == pv4["system"]
              and llm4.seen_messages[1]["content"] == pv4["user"],
              "★ 预览和实际不一致 —— 那预览就是另一个真相")

    print()
    print("=" * 68)
    if FAILS:
        print(f" 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
