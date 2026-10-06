"""叙事层：把干巴的心跳日志，重写成 HalluAct 自己写的记录。

# 唯一的铁律：单向

    操盘上下文 ──► 事件流 ──► 叙事层 ──┬──► 事件流（narrative 事件）
                                        └──► /data/logs/journal.md

它**读**操盘的产物，但绝不产出任何能进入操盘上下文的东西。

这个保证不是靠约定，是靠形状：本模块对外只有一个 `narrate()`，
它的返回值是一段纯文本；而 agent.py 里的调用点只有一处，
返回值直接交给 `bus.emit()`。想知道有没有泄漏，读那三行就够了。

`bus.emit()` 只会写事件缓冲和 JSONL —— 代码里从来没有把事件
append 回 `messages` 的路径。

# 手账为什么不落在工作区

落点刻意选了 `/data/logs/journal.md` 而不是 `/data/workspace/`：
工作区是 Agent 自己能 `fs_read` 到的地方，手账放进去等于把叙事
从后门喂回了操盘上下文。而且日志目录本来就已经以 rw 挂载给大脑
（事件流就写在那儿），不需要额外开任何挂载。

# 失败姿态

叙事是装饰，不是功能。任何异常都必须被吞掉：模型不通、超时、
返回垃圾，都只是这一轮没有日志，绝不允许影响心跳本身。

# 模型怎么选（一条硬规矩）

⛔ **不许用 DeepSeek 系写任何文字类产出。** 文笔很差，文科很差。
写总结 / 角色日记 / 叙事 / 复盘叙述，一概不行。

这不是口味问题，是**摆在一起比出来的**：同一份素材、同一个提示词，
它写出来是"合规报告"那个味道。这个模块被退稿那次的理由就是
"有一股死人味儿"。它可以做结构化提取和判断，但**当笔用不行**
（见 `PROSE_FORBIDDEN` 和 `model_advisor`）。

**换模型时还有一件事必须一起看**：思考型模型的 reasoning token
也会算进 `max_tokens`。给少了，日志会被 `finish_reason=length`
拦腰截断 —— 而 `journal.md` 是 append-only 的，还是跨重启恢复叙事
节奏的唯一依据，一篇被腰斩的日志会**一辈子待在账本里**。
所以 `model_advisor` 会为这两件事出声，并且在面板上显示出来。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import replace
from typing import Any

from .config import Config
from .llm import LLM, LLMError

log = logging.getLogger("trader.narrator")


# 「有事发生」的定义：这两类事件一旦出现，立刻写一篇，不等计时器。
#
# cost_nudge 和 skipped 刻意**不在这里**。实测 35 轮心跳里 events 模式只响了 1 次，
# 而唯一在响的触发源就是 cost_nudge —— 等于"系统每唠叨一次就写一篇日志"，
# 和初衷正好相反。那个提醒本身在 README 里就记着"会被无视"，拿它当触发源是错的。
_NOTABLE = {"trade", "trade_rejected", "human_instruction", "error",
            "sleep_start", "sleep_end"}

# 「这句话收住了」的结尾字符。**前端 index.html 的 CUT_TAIL_OK 必须是同一串** ——
# 两边判据漂了会在面板上造成"一边说截断、一边不说"，所以 test_frontend.py 里
# 有一条断言直接比对这两串字面值。
TAIL_OK_CHARS = '。！？…"”」』）)*'

# 硬截断：素材总长的上限
MAX_DIGEST_CHARS = 9000
# 最近一轮的正文最多给这么多字（它是当前状态，最该写清楚）
_LAST_BEAT_BUDGET = 2500
# 更早的轮次每轮给多少（会被夹在这个区间里）
_EARLIER_BEAT_MIN, _EARLIER_BEAT_MAX = 120, 240


# ===========================================================================
# 模型禁忌（Operator 定的规矩，2026-09-27）
# ===========================================================================
#
# ⛔ **不许用 DeepSeek 系写任何文字类产出。** 文笔很差，文科很差。
#
# 这不是口味问题，是摆在一起比出来的。同一份素材、同一个提示词，
# 它写出来的东西是"合规报告"那个味道：短句、破折号、编号感、什么都要
# "首先其次最后"，读起来像给上级交的周报。叙事层被退稿那次的理由就是
# "有一股死人味儿" —— 那味儿是它带来的。
#
# 它可以干的活：结构化提取、数值汇总、把长数据序列读成一句结论、
# 判断形态成不成立、指出任务前提本身有问题（见 README 第十一节）。
# 一句话：**当工具用没问题，当笔用不行。**
PROSE_FORBIDDEN = ("deepseek",)

# 思考型模型的标记。命中它就意味着 reasoning token 也要算进 max_tokens，
# 于是"够用"的预算要往上抬 —— 这一条对应的是一次真实的截断事故，见下。
THINKING_MARKERS = ("thinking", "reasoning", "-r1", "/r1", "o1-", "o3-", "o4-", "qwq")

# 思考型模型的最低 token 预算。
#
# 这个数是从实测里反推出来的：某思考模型在 max_tokens=2000 时思考吃掉 1922，
# 正文只写了 94 字就被 finish_reason=length 拦腰截断。
# 一篇 600 字中文正文 ≈ 1.1k token，思考的波动范围比正文大得多（见过 4002 字），
# 所以安全值取 8192 而不是"刚好够"。4096 只剩一半余量，太贴边。
MIN_TOKENS_FOR_THINKING = 8192


def is_thinking_model(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in THINKING_MARKERS)


def model_advisor(model: str, max_tokens: int) -> tuple[str, str] | None:
    """配置体检。返回 (级别, 说明) 或 None。

    级别是 "error" / "warn" —— 调用方负责把它打到日志和面板上。
    这里刻意不抛异常、不拒绝启动：叙事层坏掉不该影响操盘，
    但**必须吵到有人看见**。静默降级过的安全措施比没有更危险
    （这一条在整个项目里已经踩过两次了）。
    """
    m = model or ""
    low = m.lower()
    if any(k in low for k in PROSE_FORBIDDEN):
        return ("error",
                f"叙事层模型是 {m} —— 这个模型不许接文字类的活儿（文笔很差、文科很差）。"
                f"写总结 / 角色日记 / 叙事 / 复盘一概不行。换一个会写字的。")
    if is_thinking_model(m) and max_tokens < MIN_TOKENS_FOR_THINKING:
        return ("error",
                f"叙事层模型 {m} 是思考型，但 NARRATOR_MAX_TOKENS 只有 {max_tokens} —— "
                f"reasoning token 也算进这个预算，日志会被拦腰截断。"
                f"至少给到 {MIN_TOKENS_FOR_THINKING}。")
    if not m.strip():
        return ("warn", "叙事层没单独配 NARRATOR_MODEL，会回落主模型。")
    return None


def _deboilerplate(text: str) -> str:
    """剥掉模板化的八股。

    这些行每轮都出现、且对叙事零价值，但会吃掉大量预算：
      · markdown 表格行      —— 几十个数字，读起来是噪音
      · 账户状态行           —— "总权益 10,000.00 USDT（100% 现金）" 每轮都一样
      · 结尾的琐务声明       —— "已更新笔记 / 权益采样已打点"
    实测：剥完之后同样的预算能多装 2~3 轮的**有效内容**。
    """
    out = []
    for ln in text.splitlines():
        st = ln.strip()
        if not st:
            out.append("")
            continue
        if st.startswith("|") or st.startswith(":--") or st.startswith("|:"):
            continue                        # 表格行
        if re.fullmatch(r"[|:\-\s]+", st):
            continue                        # 表格分隔线
        # 账户状态：每轮都一模一样的那些数
        if re.search(r"10,?000(\.00)?\s*USDT", st) and len(st) < 80:
            continue
        if re.search(r"(总权益|当前持仓|持仓|权益)\s*[:：]?\s*\*{0,2}(无|0|100%\s*现金)", st) and len(st) < 60:
            continue
        if re.search(r"(无成交|无触发|无后台|账本干净|期间事件|睡着期间事件)", st) and len(st) < 70:
            continue
        # 结尾琐务
        if re.search(r"(已(打点|更新|追加|同步)|权益(采样|曲线|快照)|snapshot_equity|写入\s*`?notes/)", st) and len(st) < 90:
            continue
        out.append(ln)

    # 折叠连续空行
    res, prev_blank = [], False
    for ln in out:
        blank = not ln.strip()
        if blank and prev_blank:
            continue
        res.append(ln)
        prev_blank = blank
    return "\n".join(res).strip()

class Narrator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.enabled = bool(cfg.narrator_configured)
        self._llm: LLM | None = None
        self._char_mtime: float = -1.0
        self._char_cache: str = ""
        # 上次叙事的时间。刻意从手账里恢复，而不是只存在内存里 ——
        # 否则每次重建容器都归零，于是"刚写完就立刻又写一篇"。
        # （实测：重建一次就多一条 beat #1，就是这么来的。）
        self._last_at: float = self._restore_last_at()
        # 被 max_tokens 截断过几次、最后一次是什么时候。
        # advice 说的是"配置看起来对不对"，这两个是"实际发生过没有" —— 两件事。
        self.truncated_count: int = 0
        self.last_truncated: dict[str, Any] | None = None
        # 配置体检：模型选错了要吵，不能只是日志里一行 INFO 就过去。
        self.advice = model_advisor(cfg.narrator_effective_model, cfg.narrator_max_tokens)
        if self.advice:
            lvl, why = self.advice
            (log.error if lvl == "error" else log.warning)("叙事层配置：%s", why)
        if self.enabled:
            log.info(
                "叙事层已启用：model=%s 定时=%s 预算=%s token",
                cfg.narrator_effective_model,
                f"{cfg.narrator_max_gap // 3600}h" if cfg.narrator_max_gap > 0 else "关（每轮都写）",
                cfg.narrator_max_tokens,
            )

    # ------------------------------------------------------------------ 外部
    @property
    def llm(self) -> LLM:
        if self._llm is None:
            # 独立端点优先；没配就整份回落主模型（连温度也一起换掉）。
            self._llm = LLM(replace(
                self.cfg,
                llm_base_url=self.cfg.narrator_base_url or self.cfg.llm_base_url,
                llm_api_key=self.cfg.narrator_api_key or self.cfg.llm_api_key,
                llm_model=self.cfg.narrator_model or self.cfg.llm_model,
                llm_temperature=self.cfg.narrator_temperature,
                llm_max_tokens=self.cfg.narrator_max_tokens,
                llm_timeout=self.cfg.narrator_timeout,
            ))
        return self._llm

    def load_character(self) -> str:
        """读 config/character.md，按 mtime 缓存（改完立即生效）。"""
        path = self.cfg.narrator_character_file
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if self._char_cache:
                self._char_cache = ""
                self._char_mtime = -1.0
            return ""
        if mtime == self._char_mtime:
            return self._char_cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            return self._char_cache
        self._char_mtime = mtime
        self._char_cache = text
        return text

    def should_narrate(self, events: list[dict], beats: int = 1) -> bool:
        """两条触发，满足任意一条就写：

          A. 有事        —— 这段窗口里出过成交 / 被拒 / Operator 留言 / 报错
          B. 太久没说了  —— 距上次叙事超过 max_gap 秒

        B 不是"兜底"，它本身就是内容：长时间不开仓，**为什么不开仓**同样值得写。
        震荡行情里 Agent 可以一整天不动，这时候的日志不该是一片空白。
        """
        if not self.enabled:
            return False

        # A：有事，立刻写
        if any(ev.get("kind") in _NOTABLE for ev in events):
            return True

        # B：无事，但攒得够久了
        gap = self.cfg.narrator_max_gap
        if gap <= 0:
            return beats >= 1          # 关掉定时 = 每轮都写（旧 always 语义）
        return time.time() - self._last_at >= gap

    def _build_messages(self, events: list[dict], beat: int) -> list[dict] | None:
        """装配这一次要发出去的 messages。素材为空返回 None。

        ★ 抽出来是为了让 `preview()` 能走**同一条**装配路径。
        以前想验"素材到底长什么样"只能真的调一次模型（要说 token，还要往
        append-only 的手账里写一篇），所以实际上没人验过 —— 而素材装配
        正是最容易出错的那一段（预算分配、截断、剥八股）。
        """
        digest = self._digest(events)
        if not digest.strip():
            return None
        return [
            {"role": "system", "content": self._system_prompt(self.load_character())},
            {"role": "user", "content": self._user_prompt(digest, beat)},
        ]

    def preview(self, events: list[dict], beat: int = 1) -> dict[str, Any]:
        """不发请求：把"这一次会发出去什么"原样交出来（供面板与测试）。

        **不读手账、不写手账、不联网。** 它存在的理由和 `model_advisor` 一样：
        叙事层是这套系统里最贵、也最不透明的一次调用，出了问题必须有一处
        能在不花钱的前提下把内部状态看清楚。
        """
        messages = self._build_messages(events, beat)
        if messages is None:
            return {"empty": True, "reason": "这段窗口里没有可用的素材"}
        return {
            "empty": False,
            "system_chars": len(messages[0]["content"]),
            "user_chars": len(messages[1]["content"]),
            "total_chars": len(messages[0]["content"]) + len(messages[1]["content"]),
            "system": messages[0]["content"],
            "user": messages[1]["content"],
        }

    async def narrate(self, events: list[dict], beat: int) -> dict[str, Any] | None:
        """把这一轮心跳重写成一段第一人称记录。失败返回 None。"""
        if not self.enabled:
            return None
        try:
            messages = self._build_messages(events, beat)
            if messages is None:
                return None
            t0 = time.time()
            reply = await self.llm.chat(messages, tools=None)
            text = _strip_fences((reply.get("content") or "").strip())
            if not text:
                return None
            usage = reply.get("usage") or {}
            finish = str(reply.get("finish_reason") or "")
            truncated = finish == "length"
            self._last_at = time.time()
            if truncated:
                self.truncated_count += 1
                self.last_truncated = {
                    "at": time.strftime("%m-%d %H:%M"),
                    "beat": beat,
                    "chars": len(text),
                    "model": self.cfg.narrator_effective_model,
                }
                # 要吵。静默降级过的安全措施比没有更危险 —— 这条在这个项目里踩过两次。
                log.error(
                    "叙事层这一篇被 max_tokens 截断了（finish_reason=length，正文 %d 字）。"
                    "正文不进手账（只留一行记录），但这一轮的心跳内容就是白写了 ——"
                    "下一篇还会从同一个位置接着写。把 NARRATOR_MAX_TOKENS 抬上去"
                    "（当前 %d；思考型模型至少 %d）。",
                    len(text), self.cfg.narrator_max_tokens, MIN_TOKENS_FOR_THINKING,
                )
            self._append_journal(beat, text, truncated=truncated)
            return {
                "text": text,
                "model": self.cfg.narrator_effective_model,
                "beat": beat,
                "duration": round(time.time() - t0, 1),
                "tokens": usage.get("total_tokens"),
                # ★ 这两个字段是 2026-09-27 补的，补的是一次真事故：
                #   `llm.chat()` 一直在返回 finish_reason，而这里**拿到了却没用**。
                #   后果是"这篇是不是被截断的"在事件流里完全看不出来 ——
                #   HANDOVER 里那条待办「要看 finish_reason ≠ length」根本无从看起，
                #   而 02:57 那篇断在「SOL 破 124 或」的日志已经静悄悄进了手账。
                "finish_reason": finish or None,
                "truncated": truncated,
            }
        except LLMError as exc:
            log.warning("叙事层模型调用失败：%s", exc)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 装饰性功能，绝不允许弄挂心跳
            log.warning("叙事层异常：%s: %s", type(exc).__name__, exc)
            return None

    # ------------------------------------------------------------------ 组装
    def _system_prompt(self, character: str) -> str:
        r"""叙事层的 system prompt（2026-09-27 重写）。

        起草：Gemini-3.2-Pro/Antigravity。定稿：Operator 从两版原稿里选了它，逐字采用。

        我先交过一版，被退了，理由是"有一股死人味儿"。回头看这个判断是对的：
        我那版把要求写成了 9 条编号的合规清单，短句、破折号、命令式；
        这一版是在**跟模型说话**（"你觉得盘面无聊吗？你在等什么条件？"），
        是人在解释自己为什么要这么写。同一套约束，后者更像人话，
        模型也就更容易把人话写出来。**提示词自己的语感会渗进产出里** ——
        这是这次重写最大的收获，比任何一条具体规则都值钱。

        这一版对旧版的功能改动，都是拿线上 4 篇真实产出的毛病定的：
          1. 数字极简令 —— 全文具体数字不超过 4 个。旧版"只留他真会在意的几个"
             是空话，模型无法执行；换成可计数的硬规则。
          2. 禁止汇报系统杂务 —— 旧版 4 篇里 3 篇以"记了权益采样点、
             日志写进 notes/xxx.md 了"收尾。
          3. 打破模板 —— 旧版 4 篇骨架完全一致。
        ★ 关于 `$` 的来源，我一开始的判断是错的，记在这里免得下次重犯：

        我曾认定"旧版提示词自己给了三个可照抄的坏样例（`$0.30\%$` 之类），
        所以产出才满屏 `$`"。但事后核账发现：**手账里确实 33 处 `$`，
        而操盘层自己的输出里只有 10% 的文本带 LaTeX。**
        也就是说主要的 LaTeX 是叙事模型**自己生成的**，不是从素材抄的 ——
        反面样例顶多是帮凶，不是主犯。

        所以这条规则的真正意义是"堵住一个不限量生成的来源"，而不是"纠正抄袭"。
        别把它当成"负面指令会变成格式清单"的证据，那是我当时的过度归纳。

        关于"数字极简令"：Gemini 原稿里有"全文具体数字不超过 4 个"这条硬规则，
        实测确实把数字密度从 17 个压到 2 个。但 Operator 看过对照产出后决定**删掉它** ——
        一是没必要限制，二是模型规避上限时会改用中文数字（产出里出现了"零点五到一点一"），
        量比这种要精度的信息写成中文数字反而更难读。
        所以现在不再给数字设上限，只保留"不用 `$` 包数字"这一条。
        """
        prompt = f"""# 你的任务
你正在把一份冷冰冰的机器交易日志，重写成「你本人（HalluAct）亲手写的交易手账」。

## 核心准则：如何写「什么都没做」
「什么都没做」是你最常做的决策，它本身就是重要内容。
如果你这段时间没交易，**绝对不要编造交易**。
不要用罗列一堆死水数据来凑字数，而是写出你的**思考过程**：你觉得盘面无聊吗？你在等什么条件（比如某个支撑位、某种放量）？为什么现在的条件不够好（摩擦成本太高、没确认）？写清「等待的逻辑」，比一笔含糊的交易更有价值。

## 他睡觉的时候
睡眠是他的正常作息，**不是空白，也不是故障**。素材里出现「他去睡了」「他醒了」的时候，
照实写：他为什么在这个时点去睡、睡前把什么安顿好了（止损、自唤醒阈值）、
或者醒来时盘面变成了什么样。
**不要编造梦境，也不要把他写成熬夜守着盘的苦工。** 素材里没有的睡眠不要自己加。

## 硬性约束（必须遵守）
1. **视角与口吻**：完全的第一人称（「我」）。直接开始写你的想法，不要写旁白，不要解释背景。
2. **绝不捏造**：素材里没有的行情、成交、对话，一律不许写。数字必须和素材完全一致。
3. **禁止 LaTeX 与特殊符号**：**绝对不要**使用 `$` 符号包裹数字。所有数字、百分比直接用最朴素的纯文本写（例如：0.30%、84120、122.94）。
4. **禁止汇报系统杂务**：**绝对不要**在结尾（或任何地方）写「记录了权益点」「写入了某某文件」「取了多少次行情」这类系统动作。写完你的市场判断，直接停笔。
5. **打破模板**：不要每次都用「扫了四个标的，结论是...」开头。可以直接从某个币的槽点开始，可以从一句感慨开始，也可以直接抛出你的等待目标。
6. **格式与长度**：字数限制在 {self.cfg.narrator_min_chars} ~ {self.cfg.narrator_max_chars} 字。短而精。不要标题，不要代码块围栏，不要总结升华，不要感叹号，禁止使用斜体。"""
        if character:
            prompt += f"\n--- 下面是这个人是谁 ---\n{character}\n--- 人物志结束 ---\n"
        return prompt

    def _user_prompt(self, digest: str, beat: int) -> str:
        journal = self._journal_tail()
        return f"""这是截止到第 {beat} 轮心跳的原始素材：

{digest}

--- 你之前写过的手账（最近几条）---
[注：如果以下内容为空，说明这是第一篇，直接开写即可。如果有内容，只用来接上你的语气和语境，绝对不要重写过去的事，也不要假装过去的事是刚发生的。]
{journal}
--- 手账结束 ---

现在，请以你的第一人称直接写下这篇手账。
记住：绝对不用 `$` 符号；写完判断直接结束，绝不汇报保存日志等系统杂务。"""

    # ------------------------------------------------------------------ 素材
    def _digest(self, events: list[dict]) -> str:
        """把这段窗口压成一段素材。

        窗口可能横跨很多轮心跳（定时触发时最多 24 轮），所以不能简单拼接：
        24 轮正文原始就有 2 万字，直接拼会撞死在 MAX_DIGEST_CHARS 上，
        而且撞死的方式是"尾巴被砍掉"—— 砍掉的恰恰是最近的、最该看的内容。

        所以按预算分配：

          1. 事件（成交 / 被拒 / 留言 / 报错）是稀疏的，完整保留，不参与分配
          2. 最近一轮给足（它是当前状态）
          3. 剩下的预算平分给更早的轮次，每轮夹在 [100, 900] 之间

        这样单轮触发时几乎不截断，24 轮触发时每轮自动压缩成一句话，不用配任何参数。
        工具调用的原始 JSON 一律丢弃 —— 又长又没营养，只会把叙事模型带进"写报告"的语气。
        """
        beats: list[dict] = []      # [{"no": int, "texts": [str]}]
        notable: list[str] = []     # 稀疏事件，原样保留
        tools = fetches = 0
        cur: dict | None = None

        for ev in events:
            kind = ev.get("kind")
            d = ev.get("data") or {}

            if kind == "beat_start":
                cur = {"no": d.get("beat"), "texts": []}
                beats.append(cur)
            elif kind == "tool_call":
                tools += 1
                if str(d.get("tool", "")).startswith(("market__", "exa__")):
                    fetches += 1
            elif kind in ("agent_text", "beat_summary"):
                t = (d.get("text") or "").strip()
                if not t:
                    continue
                # 顺手把模板八股剥掉，省下的预算留给有效内容
                t = _deboilerplate(t)
                if not t:
                    continue
                # 一轮里 agent_text 会出现多次、最后一次通常和 beat_summary 重复
                if cur is not None and t not in cur["texts"]:
                    cur["texts"].append(t)
            elif kind == "thinking":
                continue  # 思维链是给调试看的，叙事模型看了会写成分析报告
            elif kind in _NOTABLE:
                line = self._notable_line(kind, d)
                if line:
                    notable.append(line)

        # 同一条事件重复出现时压成「×N」。
        # 实测踩过：调度器异常每 30 秒重试一次，一轮窗口里塞进 7 条一模一样的
        # 「[出错] name 'bus' is not defined」，把预算吃了、还让素材看起来像
        # 出了 7 次不同的事故。计数才是真相。
        if notable:
            counted: list[str] = []
            seen: dict[str, int] = {}
            order: list[str] = []
            for line in notable:
                if line in seen:
                    seen[line] += 1
                else:
                    seen[line] = 1
                    order.append(line)
            for line in order:
                n = seen[line]
                counted.append(line + (f"（同一件事重复了 {n} 次）" if n > 1 else ""))
            notable = counted

        # ---- 预算分配 ----
        #
        # 两条硬约束：
        #   1. 最近一轮优先，它的空间先扣出来 —— 它是当前状态，最不能被砍
        #   2. 更早的轮次用计数器硬拦，不靠"每轮夹在某个区间"这种软约束。
        #      软约束在窗口很长时会溢出（96 轮 × 最小 100 字 = 9600 > 9000），
        #      而溢出后的截断砍掉的正好是尾部 —— 也就是最近那一轮。
        notable_txt = "\n".join(notable)
        budget = MAX_DIGEST_CHARS - len(notable_txt) - 400

        if not beats:
            return notable_txt.strip()

        last = beats[-1]
        earlier = beats[:-1]

        last_txt = self._pick(last["texts"], _LAST_BEAT_BUDGET, recent=True)
        budget -= len(last_txt)

        earlier_lines: list[str] = []
        if earlier:
            per = max(_EARLIER_BEAT_MIN, min(_EARLIER_BEAT_MAX, budget // len(earlier)))
            spent = 0
            # 从近到远取：靠近当前状态的轮次比很久以前的更值得留
            for b in reversed(earlier):
                t = self._pick(b["texts"], per)
                if not t:
                    continue
                entry = f"· 第 {b['no']} 轮：{t}"
                if spent + len(entry) > budget:
                    break
                spent += len(entry)
                earlier_lines.append(entry)
            earlier_lines.reverse()

        lines = []
        if tools:
            lines.append(f"时间跨度内共 {len(beats)} 轮心跳，工具调用 {tools} 次"
                         f"（其中取行情/搜索 {fetches} 次）")

        if notable_txt:
            lines.append("")
            lines.append("[这期间发生的事]")
            lines.append(notable_txt)

        if earlier_lines:
            lines.append("")
            lines.append("[他之前几轮的想法，按时间顺序]")
            lines.extend(earlier_lines)

        if last_txt:
            lines.append("")
            lines.append(f"[最近这一轮（第 {last['no']} 轮）他说的话]")
            lines.append(last_txt)

        out = "\n".join(lines).strip()
        if len(out) > MAX_DIGEST_CHARS:
            out = out[:MAX_DIGEST_CHARS] + "\n…（截断）"
        return out

    @staticmethod
    def _pick(texts: list[str], limit: int, recent: bool = False) -> str:
        """从一轮里的几段话中挑出最适合叙事的，并压到 limit 以内。

        两个判断，都是拿线上真实日志量出来的：

        1. **取最后一段，不取最长的一段。**
           操盘 Agent 的输出是「账户模板 → 盘面罗列 → 决策 → 下轮监控」的
           格式化报告。最长的那段就是整篇报告，八成是数字；
           最后一段才是收尾结论 —— 决定了什么、为什么。
           叙事要的是结论，不是过程。

        2. **截断时向尾部压。**
           报告的**开头**是每轮一模一样的「账户与持仓核对 / 总权益 10000」，
           **结尾**才是「本轮决策 + 下轮监控」。所以早期轮次索性只留尾部：

             最近一轮  recent=True   留 1/4 头 + 3/4 尾（它是当前状态，要完整）
             更早的轮次 recent=False 只留尾部（只要它当时的结论和在看什么）

           实测：12 轮窗口按这个口径，素材从 7600 字降到 5000 字上下，
           而且里面剩下的基本都是「结论 + 在等什么」，重复的盘面描述被挤掉了。
        """
        if not texts:
            return ""
        picked = texts[-1]
        if len(picked) <= limit:
            return picked
        if recent:
            head = limit // 4
            return picked[:head].rstrip() + " … " + picked[-(limit - head):].lstrip()
        # 只要尾部。加个省略号提示这里被裁过，别让叙事模型以为一轮就这么短。
        return "… " + picked[-limit:].lstrip()

    @staticmethod
    def _notable_line(kind: str, d: dict) -> str:
        if kind == "trade":
            pnl = d.get("realized_pnl")
            auto = "（后台自动触发，不是它自己下的单）" if d.get("auto") else ""
            s = (
                f"[成交] {d.get('side')} {d.get('symbol')} "
                f"数量 {d.get('qty')} 成交价 {d.get('fill_price') or d.get('price')} "
                f"金额 {d.get('notional')} 手续费 {d.get('fee')}{auto}"
                + (f" 实现盈亏 {pnl}" if pnl is not None else "")
            )
            if d.get("reason"):
                s += f"\n  理由：{d['reason']}"
            return s
        if kind == "trade_rejected":
            return f"[被风控拒绝] {d.get('tool')}：{d.get('reason')}"
        if kind == "human_instruction":
            return f"[Operator 给他留了话] {d.get('text')}"
        if kind == "error":
            return f"[出错] {d.get('message')}"
        if kind == "sleep_start":
            # 睡眠期间的定时唤醒是静默的，所以"他去睡了"这件事本身
            # 就是这个窗口里最该写的东西 —— 它不是空白。
            return (f"[他去睡了] 原因：{d.get('reason')}　"
                    f"（当时滚动 24 小时已睡 {d.get('slept_last_24h_hours')} 小时、"
                    f"还欠 {d.get('debt_hours')} 小时、已连续清醒 "
                    f"{d.get('awake_span_hours')} 小时）")
        if kind == "sleep_end":
            return (f"[他醒了] 这一觉睡了 {d.get('slept_hours')} 小时，"
                    f"原因：{d.get('reason')}　"
                    f"（滚动 24 小时累计 {d.get('slept_last_24h_hours')} 小时，"
                    f"还欠 {d.get('debt_hours')} 小时）")
        return ""

    # ------------------------------------------------------------------ 手账
    def _restore_last_at(self) -> float:
        """从手账最后一条标题行里把时间读回来，让叙事节奏跨重启保持。

        手账标题长这样：## beat #12 · 2026-09-27 06:30
        """
        try:
            with open(self.cfg.narrator_journal_file, "r", encoding="utf-8") as f:
                data = f.read()
        except OSError:
            return 0.0
        hit = None
        for line in data.splitlines():
            if line.startswith("## beat #") and " · " in line:
                hit = line.rsplit(" · ", 1)[-1].strip()
        if not hit:
            return 0.0
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return time.mktime(time.strptime(hit, fmt))
            except ValueError:
                continue
        return 0.0

    def _journal_tail(self) -> str:
        """手账末尾的一段，作为「你之前是怎么写的」喂给模型。

        ★ 这里会做一次**截断检测**（2026-09-27 加，起因是 preview() 第一次
        把这段素材打出来给人看）：

            手账尾部那半句「…SOL 破 124 或」跟着素材一起进了提示词，
            而紧随其后的指令是「现在直接写下这篇手账」。

        对模型的杀伤力比对人更大 —— **一篇断在半句的"范文"有可能被学成一种写法**。
        `_append_journal` 现在不会再把截断的正文写进账本，但账上已经有一篇
        （02:57，那时还没这条规则），而且 append-only 的原则是不改历史。

        所以处理放在**读侧**：不改账本，只在"喂给模型的这一份"上加一句注。
        判据是"结尾不像一个收得住的句子" —— 宁可误判（多一句注，模型会忽略），
        不可漏判（半句被当成写法）。
        """
        n = self.cfg.narrator_journal_excerpt
        if n <= 0:
            return ""
        try:
            with open(self.cfg.narrator_journal_file, "r", encoding="utf-8") as f:
                data = f.read()
        except OSError:
            return ""
        tail = data[-n:].strip()
        if not tail:
            return tail
        if tail.rstrip()[-1] not in TAIL_OK_CHARS:
            tail += ("\n\n（注：上面最后一段是**被截断**的残句，不是写法。"
                     "它当时撞上了 token 上限，不是你写不下去。不要照学。）")
        return tail

    def _append_journal(self, beat: int, text: str, truncated: bool = False) -> None:
        """把这一篇追加进手账。**被截断的正文不写进去，只留一行记录。**

        为什么截断的正文不进账本：`journal.md` 有两个读者，半截文章对两个都是负资产。

          · 人 —— 读到「SOL 破 124 或」会以为是自己漏看了后半段，而它根本不存在
          · 模型 —— 手账尾部会作为「前一篇长这样」喂回下一次。一篇断在半句的范文
            有可能被学成一种写法。**不该让残缺的东西当范例。**

        但事实不能丢：留一行说明，并且保住 `## beat #N · 时间` 标题 ——
        `_restore_last_at()` 靠它恢复叙事节奏的锚点，少一行就会变成"重启后立刻重写一篇"。

        （那篇已经躺在账上的 02:57 是这条规则之前的事，append-only 的原则是不改历史，
          所以它留在那儿，记录在 DEPLOY-20260927-FIX.md 里。）
        """
        path = self.cfg.narrator_journal_file
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            head = f"\n## beat #{beat} · {time.strftime('%Y-%m-%d %H:%M')}\n\n"
            with open(path, "a", encoding="utf-8") as f:
                if truncated:
                    f.write(head + "（这一篇没写进手账：撞上了 max_tokens 上限，正文被拦腰截断。"
                                   "半截文章不该混进这本账，但它确实发生过，所以留这一行。）\n")
                    return
                f.write(head + text + "\n")
        except OSError as exc:
            log.warning("手账写入失败：%s", exc)

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        character = self.load_character()
        return {
            "enabled": self.enabled,
            "model": self.cfg.narrator_effective_model if self.enabled else None,
            "max_tokens": self.cfg.narrator_max_tokens,
            "thinking": is_thinking_model(self.cfg.narrator_effective_model),
            # 配置体检的结论。面板上要能一眼看见"模型选错了"或"预算给少了"，
            # 而不是等人去翻日志。
            "advice": ({"level": self.advice[0], "message": self.advice[1]}
                       if getattr(self, "advice", None) else None),
            "max_gap_hours": round(self.cfg.narrator_max_gap / 3600, 1),
            "character_loaded": bool(character),
            "character_chars": len(character),
            "journal_exists": os.path.isfile(self.cfg.narrator_journal_file),
            "last_at": (
                time.strftime("%m-%d %H:%M", time.localtime(self._last_at))
                if self._last_at else None
            ),
            # 运行时体检：真的被截断过几次、最后一次是谁。
            # 面板上要能一眼看见，而不是靠人肉去数句子断没断。
            "truncated_count": self.truncated_count,
            "last_truncated": self.last_truncated,
        }


def _strip_fences(text: str) -> str:
    """有的模型爱把正文裹进 ``` 里，扒掉。"""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if len(lines) >= 2 and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return t
