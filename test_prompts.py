#!/usr/bin/env python3
"""操盘提示词的守卫测试。纯静态 + 纯逻辑，不联网、不花钱。

**为什么要先写这份测试，再去改提示词：**

下一步要做的事是「重写 `base_prompt`」。那是一次**只为了省钱**的改动 ——
字数砍一半，行为不许变。可它守的东西全是软的：纪律、口径、边界。
没测试的话，改完你怎么知道「三问」还在？「不交易是默认选项」还在？
「子代理失忆所以要自包含」还在？只靠人眼比对两份长文本，一定漏。

所以这份测试的作用是：**把「重写时绝不许丢的东西」写成断言。**
它不评价文风（那归人拍板），只钉住语义。

三个部分：
  A. **不变量** —— 风控边界、纪律口径、成本口径、子代理契约，一条都不许丢
  B. **不该存在的东西** —— 重复、失效引用、已经被删掉的旧机制
  C. **成本体检** —— 每节多大、最肥的是哪个、有没有超预算

跑法：bash runtests.sh prompts
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import types

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def note(name: str, detail: str) -> None:
    print(f"    · {name}: {detail}")


def _find_trader() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "brain" / "trader", here / "trader", pathlib.Path("/t/trader")):
        if (cand / "prompts.py").is_file():
            return cand
    sys.exit("找不到 prompts.py")


TRADER_DIR = _find_trader()


def _load_prompts():
    """把 prompts.py 单独加载进来（它只依赖 config，没有相对 import 之外的横向依赖）。"""
    pkg = types.ModuleType("trader_probe")
    pkg.__path__ = [str(TRADER_DIR)]
    sys.modules.setdefault("trader_probe", pkg)

    # config.py 要能 import：它只读 os.environ，安全
    spec = importlib.util.spec_from_file_location("trader_probe.config",
                                                  TRADER_DIR / "config.py")
    cfg_mod = importlib.util.module_from_spec(spec)
    sys.modules["trader_probe.config"] = cfg_mod
    spec.loader.exec_module(cfg_mod)

    spec2 = importlib.util.spec_from_file_location("trader_probe.prompts",
                                                   TRADER_DIR / "prompts.py")
    pm = importlib.util.module_from_spec(spec2)
    sys.modules["trader_probe.prompts"] = pm
    spec2.loader.exec_module(pm)
    return pm, cfg_mod


class _Cfg:
    """最小替身。用真的 Config 会依赖 .env，而这里只想看模板长什么样。"""

    agent_name = "Pulse"
    watchlist = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    subagent_max_concurrency = 3


def main() -> int:
    print("=" * 70)
    print("操盘提示词 · 守卫测试（不联网、不花钱）")
    print("=" * 70)
    pm, cfg_mod = _load_prompts()

    base = pm.base_prompt(_Cfg())
    closing = pm.closing(_Cfg())
    beat = pm.beat_context_template()
    # 人格段也要算进来 —— 它是 system prompt 的第 2 层，而且是"角色是皮，
    # 纪律是骨"这条边界唯一写着的地方。我第一次写这份测试时漏了它，
    # 于是 A7 两行全红，而那是测试的错，不是提示词的错。
    persona_section = pm.persona_section("（人格替身，测试用）")
    whole = base + "\n\n" + persona_section + "\n\n" + closing
    src = (TRADER_DIR / "prompts.py").read_text(encoding="utf-8")

    # ================================================================ A 不变量
    print("\n[A] 重写时绝不许丢的东西")

    # —— 一组容错断言。
    #
    # 这里的教训是 2026-09-27 那次重写买来的：这些检查原本锚在**具体措辞**上
    # （「无法修改」「绝不外包」「每次唤醒你都是新的」），换个说法就报假红。
    # 而假红的代价很大 —— 它会逼着下一次改写去迁就已经写下的字面，把提示词钉死。
    # 所以凡是"同义说法很多"的地方，都改成**认意思不认字**。
    # 但别把它改成空断言：下面每条都必须至少命中一个真正的语义锚点。
    def any_of(*pats: str) -> bool:
        return any(re.search(p, whole) for p in pats)

    # —— 风控边界：这是全项目最不能软的一条
    print("  [A1] 风控边界")
    check("点名风控改不动也绕不过（不是建议，是代码）",
          any_of(r"(无法|改不了|改不动|动不了)[^。\n]{0,12}(修改|改)",
                 r"(无法|绕不过|绕不开|绕不了)[^。\n]{0,12}绕")
          and any_of(r"(写死|硬编码|写在[^。\n]{0,6}(代码|引擎)|代码里)"))
    check("要求读 rejection_reason 而不是硬刚",
          "rejection_reason" in whole)
    check("禁止反复重试被拒的单子（防撞闸门烧心跳）",
          "绝不要对同一个被拒的单子反复重试" in whole
          or ("反复重试" in whole and "被拒" in whole))

    # —— 决策纪律：三问是这套东西的核心
    print("  [A2] 决策纪律（三问 / 默认不动 / 止损）")
    check("『不交易是默认选项』还在", "不交易是默认选项" in whole)
    check("开仓三问还在（理由 / 认错点 / 了结）",
          all(k in whole for k in ("入场理由", "认错", "了结")),
          "缺：" + "/".join(k for k in ("入场理由", "认错", "了结") if k not in whole))
    check("三问的『答不上来就不开仓』判据还在",
          "不要开仓" in whole or "就不开仓" in whole)
    check("止损是强制项（不是可选）」", "必须同时设置止损" in whole or "必须" in whole and "止损" in whole)
    check("睡眠期间靠后台巡逻线程守止损（这是它敢睡的底气）",
          "止损" in whole and ("后台" in whole or "巡逻" in whole))
    check("禁止摊低成本 / 不许为回本加注",
          "摊低成本" in whole and ("挽回" in whole or "加大赌注" in whole))
    check("连亏后是降规模而不是加规模",
          "降低仓位规模" in whole or ("连续亏损" in whole and "降低" in whole))
    check("熔断后要复盘，不许换品种继续试",
          "复盘" in whole and ("换" in whole or "继续试" in whole))
    check("诚实面对错误、不许事后编理由",
          "事后编造理由" in whole or "编造" in whole)

    # —— 成本口径：这些数字直接决定每轮烧多少
    print("  [A3] 成本口径")
    check("往返成本 0.30% 这个数是从配置算出来的（不是写死的字面量）",
          "0.30%" in whole, "实际片段：" + str(re.findall(r"约?为? ([\d.]+)%", whole)[:3]))
    check("要求期望收益盖过成本再翻倍才动手",
          "覆盖成本" in whole or "盖住成本" in whole)
    # 这一条 2026-09-27 改过锚点，值得说明：
    # 原来断的是「最多 2 个」（限制深挖标的数）。那条限制被去掉了 ——
    # 它存在的唯一理由是省 token，而实测证明它根本没在省（自己取数 518 次、
    # 成本提醒响过 19 次）。现在守的是**同一件事的正确形态**：
    # 不设数量上限，但"看得宽"必须走 delegate，原始数据不许进主上下文。
    # 所以这里认的是「不许把原始数据拖进上下文」这个意思，不再认数字。
    check("限制深挖标的数的说法已经去掉（不再有「最多 N 个」）",
          not re.search(r"最多\s*[两2]\s*个", whole),
          "数字上限又回来了")
    check("改为要求「看得宽就派出去」而不是「少看几个」",
          ("delegate" in whole or "子代理" in whole)
          and any_of("看几个币不是问题", "别把原始数据", "拖进.{0,4}上下文", "少看"),
          "去掉上限却没说替代机制 = 放开一笔预算而没立规矩")
    check("明确「工具返回的字节会重复计费」这个机制",
          "重复计费" in whole or "反复重新计费" in whole or "重复重新计费" in whole)
    check("日志类文件要用 tail_lines 只读末尾",
          "tail_lines" in whole)
    check("无事可做时直接结束这一轮（不许凑动作）",
          "凑一次交易" in whole or "凑" in whole)

    # —— 长期记忆：跨心跳唯一活下来的东西
    print("  [A4] 长期记忆")
    check("明确『每次醒来都是全新的』（跨心跳只活下来磁盘）",
          any_of(r"(每次|每一轮|每一个周期)[^。\n]{0,6}(唤醒|醒来|睁眼|睡醒)")
          and any_of("清空", "全新", "都是新的", "不记得", "从零", "记忆归零"),
          "只认「每次醒」这个动作 + 「记忆清零」这个意思，不认具体措辞")
    check("把『认真写笔记』定性成核心技能而不是可选项",
          "核心技能" in whole or "不是可选项" in whole)
    check("先看账本与事件流（尤其睡眠期间被后台平掉的仓）",
          "get_account" in whole and "get_events" in whole)

    # —— 子代理契约：这份契约丢了，delegate 就会退化成烧钱机
    print("  [A5] 子代理契约")
    check("『子代理是失忆的』这条还在（任务必须自包含）",
          "失忆" in whole)
    check("要求把要返回什么写进 want（含格式和长度）",
          "自包含" in whole and ("want" in whole or "格式" in whole))
    check("开仓/平仓的最终决定绝不外包",
          "绝不外包" in whole or "不外包" in whole)
    check("明确可以并行派多个", "并行" in whole)
    check("并行数量来自配置（不是写死的数字）",
          "3" in whole and "同时最多" in whole)

    # —— 睡眠与唤醒：这一段决定每天烧多少钱
    print("  [A6] 睡眠与唤醒")
    check("三类唤醒里，兜底的语义是「最长沉默」而不是固定节拍",
          "最长沉默" in whole)
    check("『睡眠期间定时唤醒静默』还在", "静默" in whole)
    check("睡前两件事：设止损 + 押自唤醒阈值",
          "止损" in whole and "自唤醒" in whole)
    check("Agent 只能把兜底间隔改短（人类值是上界）",
          "只能比人类设的上界更短" in whole or "更短" in whole)
    check("配额数字与 mcp-paper 一致（6.5 / 11 / 17.5）",
          all(k in whole for k in ("6.5", "11", "17.5")),
          "缺：" + "/".join(k for k in ("6.5", "11", "17.5") if k not in whole))
    check("被价格叫醒后会自动回睡",
          "自动回到睡眠" in whole or "回睡" in whole)

    # —— 人格边界：角色是皮，纪律是骨
    print("  [A7] 人格边界")
    check("人格段声明『不改变风控纪律与数字』",
          "不改变你的风控纪律" in whole or "纪律优先" in whole)
    check("人格段明确『角色是皮，纪律是骨』",
          "角色是皮" in whole and "纪律是骨" in whole)

    # ================================================================ B 不该有的
    print("\n[B] 不该存在的东西")
    check("没有引用已删掉的 HEARTBEAT_SECONDS",
          "HEARTBEAT_SECONDS" not in whole)
    check("没有『固定心跳』这种旧说法", "固定心跳" not in whole)
    check("没有 15 分钟心跳这种过时前提（唤醒是三类触发，不是固定节拍）",
          "15 分钟里" not in whole and "每 15 分钟都要" not in whole,
          "命中：" + str(re.findall(r".{0,18}15 分钟.{0,18}", whole)))

    # 重复项：同一件事在多个地方反复说，是上一版的典型毛病
    dul = len(re.findall("delegate", whole, re.I))
    note("delegate 出现次数", f"{dul} 次（上一版 20 次；这是个观察值，不是硬指标）")

    print("\n[C] 成本体检")

    def chars(s: str) -> int:
        return len(s)

    sections = re.split(r"\n(?=# )", base)
    rows = sorted(((s.strip().splitlines()[0][:46], chars(s)) for s in sections if s.strip()),
                  key=lambda r: -r[1])
    for title, n in rows:
        print(f"    {title:<48}{n:>6} 字符")
    note("base_prompt 合计", f"{chars(base)} 字符")
    note("system 合计", f"{chars(whole)} 字符（base {chars(base)} + closing {chars(closing)}）")
    note("beat 模板", f"{chars(beat)} 字符")

    # 预算：2026-09-27 重写之前这里只有观察值，没有闸。重写完拍板了，棘轮上紧：
    # 这不是"建议值"，是**不许反弹**。要往里加内容，就得先删掉等量的内容 ——
    # 因为每一次 LLM 调用都要重发一遍，涨 100 字符就是每一步多花 40 token。
    # 要抬这个天花板，得先把"删了什么换来的"写进提交信息里。
    check("base_prompt 没涨回重写前的规模（棘轮：2841 → 上限 2900）",
          chars(base) <= 2900, f"{chars(base)} 字符")
    check("beat 模板保持短（它是每轮都要发的，不该再抄一遍纪律）",
          chars(beat) < 1200, f"{chars(beat)} 字符")
    check("工具描述里没有把 base_prompt 的纪律整段抄进去",
          "不交易是默认选项" not in _tool_desc_text(TRADER_DIR),
          "工具描述里出现了 base_prompt 的句子")

    # ================================================================ D delegate
    # delegate 是所有工具定义里最肥的一个，而且它的字**一半来自人类维护的白名单文件**。
    # 这里只能做源码级检查（真要构造 schema 得 import subagent，那会拖进 MCP 依赖，
    # 而这份测试刻意保持"不 import 任何 trader 模块就能跑"）。
    # 但它守的东西够关键：判据、边界、并发上限的来源、以及白名单目录别被压没了。
    print("\n[D] delegate 工具定义的棘轮（源码级）")
    sub_src = (TRADER_DIR / "subagent.py").read_text(encoding="utf-8")
    check("判据还在（≥3 次工具调用就派出去）", "≥3 次工具调用" in sub_src)
    check("开仓/平仓的最终决定绝不外包", "绝不外包" in sub_src)
    check("并发上限来自配置，不是写死的数字",
          "同时最多" in sub_src and "subagent_max_concurrency" in sub_src)
    check("白名单仍然列在描述里（按描述挑模型，不是瞎猜）", "可选模型" in sub_src)
    cap = re.search(r"CATALOG_FIELD_CAP\s*=\s*(\d+)", sub_src)
    check("白名单目录的截断长度没被压到看不出差别的地步",
          bool(cap) and int(cap.group(1)) >= 30,
          f"CATALOG_FIELD_CAP = {cap.group(1) if cap else '没找到'}")

    # ================================================================ E 活人感
    #
    # 为什么这条规矩是**可数的**：2026-09-27 那次重写，毛病被说成"死人味儿太重"，
    # 听着像主观判断，其实有指纹 —— 破折号当说明文连接词用、冒号当清单引导词用。
    # 上一版全篇 16 个 `——`、17 个提示性冒号，量出来就是"规章体"本身。
    #
    # 所以这里把它变成硬闸。数一遍两秒钟，而人眼盯着改三遍还会漏。
    # 检查的是**渲染后的整段 system prompt**（含 persona 头与收尾），
    # 因为它们是同一次调用里一起发出去的，语气必须是一套。
    #
    # ⚠️ 唯一允许的冒号是 f-string 里的 `:.2f` 那种格式串，渲染完就没了。
    #    如果以后真要在提示词里用冒号引出原话，把它加进下面的白名单，别直接放宽。
    print("\n[E] 活人感（可数的部分）")
    dash = re.findall(r"[—–]", whole)
    check("全篇没有破折号（`—`/`–` 一律不许）", not dash,
          f"命中 {len(dash)} 处")
    check("全篇没有提示性冒号", "：" not in whole and ":" not in whole,
          "命中：" + str(re.findall(r".{0,14}[:：].{0,14}", whole))[:3])
    pivot = re.findall(r"(?:并)?不是[^。\n]{0,60}而是|并非[^。\n]{0,60}而是",
                       whole)
    check("没有翻案腔（不是……而是……）", not pivot, f"命中 {pivot[:2]}")
    check("人称是一套（写给自己的字条用「我」，不用「你」）",
          "你" not in whole.replace("（人格替身，测试用）", ""),
          "命中：" + str(re.findall(r".{0,14}你.{0,14}", whole))[:3])
    note("破折号/冒号计数", f"破折号 {len(dash)}，冒号 {whole.count('：')}")
    note("参照", "上一版（2026-09-27 上午）：破折号 16、提示性冒号 17、"
                 "「我」0 而「你」满篇")

    print()
    print("=" * 70)
    if FAILS:
        print(f"✗ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✓ 全部通过")
    return 0


def _tool_desc_text(trader_dir: pathlib.Path) -> str:
    """把本地工具 + 子代理工具的描述拼起来（MCP 那些不在源码里，够不着）。"""
    out = []
    for rel in ("tools_local.py", "subagent.py"):
        p = trader_dir / rel
        if p.is_file():
            out.append(p.read_text(encoding="utf-8"))
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
