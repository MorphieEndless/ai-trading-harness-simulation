#!/usr/bin/env python3
"""面板静态检查。纯 Python，不联网、不花钱、不用装任何东西。

# 它为什么存在

2026-09-27 出过一次事故：index.html 里 `esc(` 少了一个右括号，整段 `<script>`
无法解析，于是三行初始化（tick / loadHistory / connect）一句都没跑 ——
面板上表现为**状态永远停在"连接中"、日志永远空白**。后端一切正常，
所以从日志和数据那侧完全看不出问题，排查绕了很久。

一个丢掉的括号能造成"整页死掉但看起来像后端故障"，而当时没有任何检查守这一条。

# 为什么不用真正的 JS 解析器

tree-sitter 能做得更好，但它要额外装依赖。而这一类的错（括号不配平，
尤其是模板字符串里 `${...}` 表达式内的括号不配平）用一个能正确跳过
字符串/注释/正则的字面量扫描器就够抓了。少一个依赖，多一分可移植性。

# 它检查什么

  1. 括号配平（() [] {}），且每个 `${...}` 表达式内部也要自配平
  2. 字符串/模板字符串是否闭合
  3. `<script>` 标签数量
  4. 前端引用的 DOM id 是否都在 HTML 里存在（这个从第一天起就该有）
"""
from __future__ import annotations

import json
import json
import pathlib
import re
import sys

FAILS: list[str] = []
# 缺依赖而没跑的节。**跳过不等于通过** —— 最后要把它明说出来，
# 不然「全部通过」会盖住「这一节根本没跑」。
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def _line_of(js: str, idx: int) -> int:
    return js.count("\n", 0, idx) + 1


def _pos(js: str, idx: int) -> str:
    return f"行 {_line_of(js, idx)}"


OPEN = {"(": ")", "[": "]", "{": "}"}
CLOSE = {")": "(", "]": "[", "}": "{"}


def _regex_allowed(prev_tok: str) -> bool:
    """`/` 是正则开始还是除号。标准启发式：前面是标识符/数字/`)`/`]` 就当除号。"""
    if not prev_tok:
        return True
    if prev_tok in (")", "]", "str", "regex"):
        return False
    if prev_tok[0].isalnum() or prev_tok[0] in "_$":
        return prev_tok in ("return", "typeof", "instanceof", "in", "of", "case",
                            "do", "else", "void", "delete", "new", "yield", "await")
    return True


def _skip_string(js: str, i: int, errors: list) -> int | None:
    """i 指向引号。返回闭合引号之后的位置；出错返回 None。"""
    n = len(js)
    quote = js[i]
    j = i + 1
    while j < n:
        if js[j] == "\\":
            j += 2
            continue
        if js[j] == quote:
            return j + 1
        if js[j] == "\n":
            errors.append(f"{_pos(js, i)} 字符串没有闭合（换行前就断了）")
            return None
        j += 1
    errors.append(f"{_pos(js, i)} 字符串没有闭合（一直到文件尾）")
    return None


def _scan_template(js: str, i: int, errors: list) -> int | None:
    """i 指向反引号。模板字符串里的 `${...}` 递归交回 _scan_code 处理。

    这一步必须递归：`${}` 后面剩下的部分**又是字面量**，
    如果继续按代码扫，里面的引号会被当成真的字符串，于是整篇报假错。
    （第一版就是这么错的，被自校验那一节抓出来了。）
    """
    n = len(js)
    start = i
    j = i + 1
    while j < n:
        c = js[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1
        if c == "$" and j + 1 < n and js[j + 1] == "{":
            nxt = _scan_code(js, j + 2, errors, stop="}")
            if nxt is None:
                return None
            j = nxt
            continue
        j += 1
    errors.append(f"{_pos(js, start)} 模板字符串没有闭合")
    return None


def _scan_code(js: str, i: int, errors: list, stop: str | None = None) -> int | None:
    """扫一段代码。stop 是期望的结束符（扫 `${}` 时是 `}`），None = 扫到文件尾。

    返回结束符之后的位置；出错返回 None。
    """
    n = len(js)
    stack: list[tuple[str, int]] = []
    prev_tok = ""

    while i < n:
        c = js[i]
        if c.isspace():
            i += 1
            continue

        # 表达式结束符（只在最外层生效）
        if stop and c == stop and not stack:
            return i + 1

        if c == "/" and i + 1 < n and js[i + 1] == "/":
            j = js.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            j = js.find("*/", i + 2)
            if j < 0:
                errors.append(f"{_pos(js, i)} 块注释没有闭合")
                return None
            i = j + 2
            continue

        if c in "'\"":
            nxt = _skip_string(js, i, errors)
            if nxt is None:
                return None
            i, prev_tok = nxt, "str"
            continue

        if c == "`":
            nxt = _scan_template(js, i, errors)
            if nxt is None:
                return None
            i, prev_tok = nxt, "str"
            continue

        if c == "/" and _regex_allowed(prev_tok):
            j = i + 1
            in_class = False
            closed = False
            while j < n:
                ch = js[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    break
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    closed = True
                    break
                j += 1
            if closed:
                i, prev_tok = j + 1, "regex"
                continue
            # 不是正则，按除号处理

        if c in OPEN:
            stack.append((OPEN[c], i))
            prev_tok, i = c, i + 1
            continue
        if c in CLOSE:
            if not stack:
                if stop and c == stop:
                    return i + 1
                errors.append(f"{_pos(js, i)} 多了一个 `{c}`")
                i += 1
                continue
            want, start = stack.pop()
            if want != c:
                errors.append(
                    f"{_pos(js, i)} 括号不配：这里遇到 `{c}`，"
                    f"但 {_pos(js, start)} 打开的是 `{OPEN.get(want, want)}`，"
                    f"应该用 `{want}` 闭合")
            prev_tok, i = c, i + 1
            continue

        if c.isalnum() or c in "_$":
            j = i
            while j < n and (js[j].isalnum() or js[j] in "_$."):
                j += 1
            prev_tok, i = js[i:j], j
            continue

        prev_tok, i = c, i + 1

    if stop and not stack:
        errors.append("没有找到闭合的 `}`")
        return None
    for want, start in stack:
        errors.append(f"{_pos(js, start)} 这个 `{want}` 一直没有被闭合")
    return None


def scan_js(js: str) -> list[str]:
    """扫一遍 JS，返回错误描述列表（空 = 干净）。"""
    errors: list[str] = []
    _scan_code(js, 0, errors)
    return errors



def main() -> int:
    here = pathlib.Path(__file__).resolve().parent
    cand = [here / "brain" / "static" / "index.html",
            here / "static" / "index.html",
            pathlib.Path("/app/static/index.html")]
    path = next((p for p in cand if p.is_file()), None)
    if path is None:
        sys.exit("找不到 index.html")

    print("=" * 68)
    print(f"面板静态检查 · {path}")
    print("=" * 68)

    html = path.read_text(encoding="utf-8")

    n_open = html.count("<script")
    n_close = html.count("</script>")
    check("<script> 标签配平", n_open == n_close, f"{n_open} 开 / {n_close} 闭")

    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    check("至少有一段内联脚本", len(blocks) >= 1, f"实际 {len(blocks)} 段")

    js = blocks[0] if blocks else ""

    print("\n[1] JS 结构（括号 / 字符串 / 模板字符串）")
    errs = scan_js(js)
    check("整段脚本能被扫通，没有结构错", not errs,
          "\n      " + "\n      ".join(errs[:6]))

    print("\n[2] 初始化调用确实存在（这次事故的直接教训）")
    for fn in ("tick()", "loadHistory()", "connect()"):
        check(f"底部有 {fn} 调用", fn in js, "初始化没接上 = 页面永远是初始状态")

    print("\n[3] DOM id 对账（前端引用了、HTML 里却没有的 id）")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    sstart = html.find("<script")
    refs: dict[str, int] = {}
    for m in re.finditer(r'\$\("#([A-Za-z0-9_\-]+)"\)', html[sstart:]):
        refs.setdefault(m.group(1), 0)
        refs[m.group(1)] += 1
    for m in re.finditer(r'getElementById\("([A-Za-z0-9_\-]+)"\)', html[sstart:]):
        refs.setdefault(m.group(1), 0)
        refs[m.group(1)] += 1
    missing = sorted(k for k in refs if k not in ids)
    check(f"引用的 {len(refs)} 个 id 都存在", not missing,
          f"缺失：{missing}")

    print("\n[3b] `$()` 的写法（2026-09-27 那次「一直等待取价」的直接教训）")
    # `const $ = s => document.querySelector(s);` —— 要 id 就得写 `#`。
    # 写成 `$("mkBox")` 是按**标签名**找，页面上没有 <mkBox> 这种东西，
    # 于是返回 null，函数在 `if(!el) return;` 那一行静默退出：后端一切正常、
    # 日志一切正常，页面上只是某一块**永远停在初始占位文本**上。
    # 2026-09-27 真的这么漏了一次（renderMarket 里的 mkBox / mkMeta），
    # 而上面 [3] 那条对账**抓不到它** —— 它的正则只认带 `#` 的写法。
    HTML_TAGS = {
        "a", "b", "body", "br", "button", "canvas", "code", "div", "em", "h1", "h2",
        "h3", "h4", "h5", "h6", "head", "hr", "html", "i", "img", "input", "li",
        "main", "nav", "ol", "option", "p", "pre", "script", "section", "select",
        "small", "span", "strong", "style", "svg", "table", "tbody", "td", "textarea",
        "th", "thead", "tr", "ul",
    }

    def suspect_selectors(text: str) -> list[str]:
        """裸名字的 $() 调用里，既不是 id、又不像标签名的那些。"""
        out = []
        for m in re.finditer(r'\$\("([^"#.\[\s]+)"\)', text):
            name = m.group(1)
            if name in ids or name.lower() not in HTML_TAGS:
                out.append(name)
        return out

    sus = sorted(set(suspect_selectors(html[html.find("<script"):])))
    check("没有把 id 当成标签名去查（漏 `#` 会永远返回 null）", not sus,
          f"这几个是 id 或根本不是标签名：{sus}")
    # 自我校验：检测器真的抓得到那个写法（不然这一节等于摆设）
    check("检测器对 `$(\"mkBox\")` 报警",
          suspect_selectors('const b = $("mkBox");') == ["mkBox"])
    check("检测器不误报 `$(\"#mkBox\")` / `$(\"div\")`",
          suspect_selectors('$("#mkBox x"); $("div"); $(".cls");') == [])

    print("\n[4] 自我校验：扫描器真的抓得到「丢括号」那类错")
    broken = (
        'const t = `<div data-p="${esc(x?x.split("/").slice(0,-1)'
        '.join("/").replace(/\\/$/,""):""}"><span>..</span></div>`;'
    )
    good = broken.replace('):""}', '):"")}')
    check("对缺右括号的模板表达式报警", bool(scan_js(broken)),
          "扫描器漏报 = 这个检查等于摆设")
    check("对修好的版本不报警", not scan_js(good),
          f"误报：{scan_js(good)[:2]}")

    print("\n[5] markdown 渲染器（2026-09-27 加的日志渲染）")
    # 这一节守两件不同的事：
    #   ① 结构还在（函数、分支、CSS 规则没被删）—— 纯字面检查
    #   ② 行为对（几个正则别互相咬）—— 把 JS 正则照抄成 Python 再跑一遍输入
    #
    # 为什么要 ②：这堆正则里最容易出的错是**互相咬**。典型的两处：
    #   · `_斜体_` 会把 paper__get_risk_limits / fs_read 切碎（工具名里全是下划线）
    #   · 行内 code 里的 `**` 会被加粗规则吃掉（代码是代码，不是强调）
    # 这两条靠看是看不出来的，而它们出问题时的表现是"日志里到处是斜体碎片"。
    #
    # ⚠️ 诚实的边界：② 是 Python 版正则，不是真的跑了那段 JS。
    #    JS 那边已经用真引擎验过一轮（记在 WIP-20260927.md 的第二节，17 个用例全过）。
    #    这里留着是为了以后改正则时能立刻发现退化，不需要再找 JS 引擎。

    for fn in ("function mdInline(t){", "function mdRender(src){"):
        check(f"渲染器里有 {fn.split('(')[0].replace('function ','')}",
              fn in html, "函数没了 = 日志又变回带井号的纯文本")
    for branch, why in (("md-code", "围栏代码块"), ("md-h md-h", "标题"),
                        ("md-hr", "分隔线"), ("blockquote", "引用"),
                        ('table class="md-t"', "表格"), ('class="sub"', "嵌套列表")):
        check(f"mdRender 认得{why}", branch in html,
              f"少了「{branch}」这个分支")

    check("渲染前先转义（不认标签就吃注入）", "esc(src)" in html,
          "mdRender 里没有 esc(src)")
    check("渲染出的正文挂得上 .md 这个类", 'class="bd${md ? " md" : ""}"' in html)

    # 散文类事件必须走 markdown。这几类要是漏了，面板上最占地方的那几块又会
    # 变成"带井号还漏着星号"的纯文本 —— 那正是这次要修的毛病。
    prose = ["agent_text", "thinking", "beat_summary", "narrative",
             "cost_nudge", "human_instruction", "subagent_end"]
    for kind in prose:
        seg = re.search(r'case "' + kind + r'":(.*?)break;', html, re.S)
        check(f"{kind} 走 markdown",
              bool(seg) and "mdRender(" in seg.group(1) and "md = true" in seg.group(1),
              "它是散文，不该退化成纯文本")

    # 数据类事件**不该**走 markdown：它们是原始返回值，渲染反而会吃掉缩进
    seg = re.search(r'case "tool_result":(.*?)break;', html, re.S)
    check("tool_result 保持原样（那是数据不是文章）",
          bool(seg) and "mdRender(" not in seg.group(1))

    # --- CSS 的顺序约束：这条是这次最容易踩的坑 ---
    # .ev .bd.md{white-space:normal} 与 .ev.xxx .bd{white-space:pre-wrap} 的
    # 优先级都是 (0,3,0)，**靠源码顺序定胜负**。如果 .md 那条被挪到前面，
    # 它会被后面的 pre-wrap 覆盖，于是 markdown 换行和显式 <br> 叠加，
    # 整篇日志变成两倍行距。
    i_nar = html.find(".ev.narrative .bd {")
    i_md = html.find(".ev .bd.md {")
    check("`.ev .bd.md` 排在 `.ev.narrative .bd` 之后（否则被 pre-wrap 覆盖）",
          i_nar >= 0 and i_md > i_nar, f"narrative@{i_nar} md@{i_md}")
    check("所有气泡默认 pre-wrap（`\\n` 一律真换行）",
          ".ev .bd { color: #d0d7de; white-space: pre-wrap; }" in html)

    # --- 行为：把 JS 正则照抄成 Python 跑一遍危险输入 ---
    # 行内 code 先摘走
    CODE_RE = re.compile(r"`([^`\n]+)`")
    UNDER_RE = re.compile(r"(^|[\s(（\[「])_([^_\n]+)_(?=$|[\s)）\]」,，.。;；:：!！?？])")
    BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")

    def inline_probe(t: str) -> str:
        """按 index.html 里 mdInline 的顺序做一个最小复刻。"""
        codes: list[str] = []

        def stash(m):
            codes.append(m.group(1))
            return f"\x01{len(codes) - 1}\x01"

        s = CODE_RE.sub(stash, t)
        s = BOLD_RE.sub(r"<strong>\1</strong>", s)
        s = UNDER_RE.sub(r"\1<em>\2</em>", s)
        return re.sub(r"\x01(\d+)\x01", lambda m: f"<code>{codes[int(m.group(1))]}</code>", s)

    check("工具名里的下划线不会被切成斜体（paper__get_risk_limits）",
          "<em>" not in inline_probe("调 paper__get_risk_limits 和 fs_read 两个工具"),
          inline_probe("调 paper__get_risk_limits 和 fs_read 两个工具"))
    check("下划线斜体本身仍然工作",
          "<em>" in inline_probe("我说 _这个词_ 是斜体"))
    check("行内代码里的 ** 不会被吃掉",
          "<strong>" not in inline_probe("写 `**不要加粗**` 这样")
          and "<code>" in inline_probe("写 `**不要加粗**` 这样"))

    print("\n[6] 行情卡：先给人看上次的价，别让人对着空白等")
    # 用户原话：「实时行情一直显示『等待取价……』，不妨先展示上一轮取到的价格，
    # 这一轮取到了再更新，省得用户瞎等。」
    # 根因是 [3b] 那个选择器，但这一节守的是**另一件独立的事**：就算一次都没取成，
    # 也该把上次的价顶上，而不是空着一张卡。
    check("有本地缓存（localStorage）",
          "localStorage.getItem" in html and "localStorage.setItem" in html,
          "没有它，每次刷新页面都要重新等一次网络往返")
    i_restore = html.find("mkRestore();")          # 初始化块里的那句（不是函数定义）
    i_render = html.find("renderMarket();", i_restore)
    i_load = html.find("loadMarket().then(mkSchedule", i_render)
    check("启动顺序：先画缓存 → 再取新价",
          i_restore > 0 and i_render > i_restore and i_load > i_render,
          f"restore@{i_restore} render@{i_render} load@{i_load}")
    check("取价失败不擦掉已经画出来的价",
          "if(MK) MK_LIVE = false;" in html,
          "失败就清空 = 一次网络抖动让卡片变空白")
    check("没数据时重试更快（4 秒），有数据才回到 10 秒",
          "MK_FAST_MS = 4000" in html and "function mkDelay" in html and "function mkSchedule" in html)
    check("不再用固定 setInterval 轮询行情（那种写法没数据时也等满 10 秒）",
          "setInterval(loadMarket" not in html)
    check("缓存价必须标明是缓存（否则一份不动的价会被读成行情平静）",
          'class="mkwarn"' in html and ".mk .mkwarn" in html)
    check("旁注分三态：实时 / 缓存 / 取价失败",
          "MK_LIVE" in html and "MK_ERR" in html and "缓存 " in html)

    print("\n[7] 调度类事件有正文（不再甩一行 JSON）")
    # 2026-09-27 发现的：后端一共发二十多种事件，renderEvent 的 switch 只认十来种，
    # 剩下八种掉进 default，面板上直接显示 {"reason": "…", "debt_hours": 0.07} ——
    # 而里面往往只有一句本来就挺人话的 reason，被引号裹着。
    sched = {"price_trigger": "⚡", "sleep_start": "🌙", "sleep_end": "☀️",
             "wake_request": "🔔", "wake_policy_changed": "✎",
             "wake_refs_seeded": "🧷", "wake_capped": "⛔",
             "subagent_models_error": "⚠"}
    for kind, mark in sched.items():
        seg = re.search(r'case "' + kind + r'":(.*?)break;', html, re.S)
        check(f"{kind} 有正文渲染",
              bool(seg) and "body" in seg.group(1) and mark in seg.group(1),
              "掉进 default = 面板上甩一行 JSON")
    check("default 兜底摊成「键：值」而不是甩 JSON",
          "Object.entries(d)" in html, "以后再加新事件种类时也不会难看")
    check("过滤器里有一个「调度」按钮",
          'data-f="sleep_start,sleep_end,wake_request' in html)

    print("\n[8] 工具参数的摊开（2026-09-27：「tool_call 还是挤在一起」）")
    # 后端存 args 用的是 `json.dumps(...)` 的**紧凑**形态：整份 JSON 一行，
    # 本该换行的地方是**两个字符** `\n`。面板上直接显示就是几百字连成一片。
    # 线上实测 47 条 tool_call 的 args 里带这种字面 `\n`
    # （最夸张的一条把整份 markdown 塞进 fs_write 的 content，还被截到 300 字符）。
    for fn in ("function expandEsc(s){", "function fmtObj(", "function fmtCallArgs(raw){"):
        check(f"有 {fn.split('(')[0].replace('function ','')}", fn in html,
              "没有它，工具参数永远是挤在一起的一行")
    seg = re.search(r'case "tool_call":(.*?)break;', html, re.S)
    check("tool_call 的 args 走 fmtCallArgs",
          bool(seg) and "fmtCallArgs(" in seg.group(1),
          "不走它 = 那份 markdown 还是一条长线")
    seg = re.search(r'case "subagent_tool":(.*?)break;', html, re.S)
    check("subagent_tool 的 args 也走 fmtCallArgs", bool(seg) and "fmtCallArgs(" in seg.group(1))
    check("参数摊开后整体还是先 esc（转义不能少）",
          "esc(fmtCallArgs(d.args))" in html, "少了 esc 就是注入")
    check("tool_call 卡片能点击展开（摊开后可能很长）",
          "tool_call: \"▸ 点击展开完整参数…\"" in html and ".ev.tool_call.open .bd" in html,
          "没有折叠 = 一条 fs_write 能顶掉半个屏幕")
    check("tool_call 折叠高度比 tool_result 大",
          ".ev.tool_call .bd {" in html and "max-height: 96px" in html)

    # --- 行为：把 JS 逻辑照抄成 Python 跑一遍（和 [5] 同一套做法）---
    # ⚠️ 诚实的边界：这是 Python 版复刻，不是真的跑了那段 JS。
    #    JS 那边已经用真引擎喂线上真实 payload 验过一轮（12 项断言，
    #    明细在 DEPLOY-20260927-FIX.md）。留着它是为了以后改这三个函数时
    #    能立刻发现退化，不用再去找 JS 引擎。
    ESC_CODE = re.compile(r"`([^`\n]+)`")

    def norm_ws(s: str) -> str:
        """真字符的归一：CRLF → \n，真 tab → 两个空格（pre-wrap 下 tab 会撑开八格）。"""
        return s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "  ")

    ESC_MAP = {"n": "\n", "r": "", "t": "  ", "b": "", "f": "", '"': '"', "\\": "\\", "/": "/"}

    def expand_esc(s: str) -> str:
        """一遍扫完的转义还原 —— 和 index.html 里 expandEsc 同一套判据。

        为什么不能是一串 replace：`C:\\nope`（转义反斜杠 + 字母 n）会被
        后面那条 `\\n → 换行` 咬到，把一个正经路径改成两行。
        """
        out, i = [], 0
        while i < len(s):
            if s[i] != "\\" or i + 1 >= len(s):
                out.append(s[i]); i += 1; continue
            c = s[i + 1]
            if c == "u" and re.match(r"[0-9a-fA-F]{4}", s[i + 2:i + 6]):
                n = int(s[i + 2:i + 6], 16)
                out.append(chr(n) if 0x20 <= n != 0x7F else ""); i += 6; continue
            out.append(ESC_MAP[c] if c in ESC_MAP else "\\" + c)
            i += 2
        return norm_ws("".join(out))

    def fmt_obj(obj, pad: str, depth: int) -> list[str]:
        if depth > 3:
            return [pad + "…"]
        out: list[str] = []
        for k, v in list(obj.items())[:12]:
            if isinstance(v, dict):
                out.append(pad + k + ":")
                out += fmt_obj(v, pad + "  ", depth + 1)
            elif isinstance(v, str) and re.search(r"[\n\r\t]", v):
                # ★ 只认**真**换行：走到这里的值都来自 JSON.parse，转义已经是历史。
                #   再 unesc 一遍就是二次还原（值里一个正经的 `\n` 会被改成换行）。
                seg = norm_ws(v).split("\n")
                out.append(pad + k + ": " + seg[0])
                out += [pad + " " * (len(k) + 2) + x for x in seg[1:]]
            else:
                out.append(pad + k + ": " + ("null" if v is None else str(v)))
        return out

    BS = "\\"

    def fmt_call_args(raw: str) -> str:
        if not raw:
            return ""
        is_obj = False
        obj = None
        try:
            obj = json.loads(raw)
            is_obj = isinstance(obj, (dict, list))
        except (ValueError, TypeError):
            obj = None
        has_esc = re.search(re.escape(BS) + r"[nrt]", raw) is not None
        if not has_esc and (not is_obj or len(raw) <= 88):
            return " " + raw
        if is_obj and isinstance(obj, dict):
            lines = fmt_obj(obj, "", 1)
            total = len(lines)
            body = "\n".join("   " + x for x in lines[:44])
            return "\n" + body + (f"\n   …（参数太长，还有 {total - 44} 行）" if total > 44 else "")
        parts = expand_esc(raw).split("\n")
        return " " + parts[0] + "".join("\n   " + x for x in parts[1:])

    # 短的必须还是一行 —— 一次心跳几十条调用，全摊开会把日志流撑长
    check("短参数仍然一行（{\"limit\": 5} 不摊开）",
          "\n" not in fmt_call_args('{"limit": 5}'),
          fmt_call_args('{"limit": 5}'))
    check("空参数不炸", fmt_call_args("") == "")

    # 多行文本：必须出现真换行（这是这一节存在的全部理由）
    multi = json.dumps({"path": "notes/x.md", "content": "第一行\n第二行"})
    got = fmt_call_args(multi)
    check("多行文本变成了真换行（不再是一片）", "\n" in got, repr(got))
    rows = got.split("\n")
    check("续行缩进和值对齐（悬挂缩进：第二行落在第一行的正下方）",
          rows[3].index("第二行") == rows[2].index("第一行"),
          f"续行第 {rows[3].index('第二行')} 列 vs 值第 {rows[2].index('第一行')} 列")
    # 后端把 args 截到 300 字符，所以 JSON 常常是断的 —— 那条退路一个字都不能丢
    raw_cut = '{"content": "第一行' + BS + 'n第二行' + BS + 'n第三行'
    cut = fmt_call_args(raw_cut)
    check("被截断的 JSON 走退路：字面 \\n 变成真换行",
          "\n" in cut and (BS + "n") not in cut, repr(cut))
    check("退路一个字都不丢",
          all(x in cut for x in ("第一行", "第二行", "第三行", "{", "content")))

    # --- 用真引擎跑出来的两个真缺陷（2026-09-27 修）---
    # 它们都只在"值里有反斜杠"时才现身，看代码看不出来。
    literal = fmt_call_args(json.dumps({"content": "正则 /\\n/ 匹配换行"}))
    check("值里**字面**的反斜杠n 不许被改成换行（那是内容，不是排版）",
          "\\n" in literal and literal.count("\n") == 1,
          repr(literal))
    pathy = fmt_call_args(json.dumps({"path": "C:\\nope\\x"}))
    check("转义反斜杠 + 字母 n 不许被咬掉（C:\\nope 还是原样）",
          "C:\\nope" in pathy and "\\x" in pathy, repr(pathy))
    check("不认识的转义连反斜杠一起留着",
          "\\." in fmt_call_args(json.dumps({"re": "\\d+\\."})))
    tabs = fmt_call_args(json.dumps({"t": "甲\t乙"}))
    check("真 tab 归一成两个空格（pre-wrap 里 tab 会撑开八格，把行撑散）",
          "甲  乙" in tabs and "\t" not in tabs, repr(tabs))

    print("\n[9] 折叠提示只在真的被裁掉时出现（2026-09-27：「点开啥都没有」）")
    # 用户报：「"点击展开完整输出"展开了也是啥都没有」。
    # 根因不是交互坏了，是**提示的位置**错了：它原来是 .bd 的 ::after，
    # 也就是正文的最后一个孩子，会被 overflow:hidden 一起裁掉。于是行为刚好反了 ——
    #   短正文（实测 240/530 条，45%）：正文+提示都塞得进裁剪框 → 提示看得见，
    #                                    点开什么都没多（本来就全露着）
    #   长正文（真需要展开的）：提示被裁掉 → 没有提示，没人知道能点
    check("旧的 `.bd::after` 提示写法已经全部清掉", ".bd::after" not in html,
          "留着它 = 提示继续被裁掉")
    check("折叠提示改成 .ev 的独立一层",
          '.ev.clip:not(.open) .more' in html and "function markClip(el){" in html)
    check("默认不显示（只有 .clip 才显示）",
          ".ev .more { display: none; }" in html)
    check(".ev 有 position: relative（提示靠它定位）",
          "position: relative;   /* 折叠提示" in html,
          "少了它，.more 会跑到页面角落去")
    check("量的时机在进 DOM 之后",
          0 < html.find("s.appendChild(el);") < html.find("markClip(el);", html.find("s.appendChild(el);")),
          "游离元素上 scrollHeight 是 0，量了等于没量")
    check("只对可能被折的那几类量（别每张卡都强制重排）",
          "if(MORE_HINT[kind]) markClip(el);" in html)
    check("窗口宽度变了会重量（pre-wrap 跟着容器宽走）",
          'window.addEventListener("resize"' in html and "STREAM.forEach(markClip)" in html)
    for kind, hint in (("tool_result", "完整输出"), ("tool_call", "完整参数"),
                       ("thinking", "思维链详情"), ("subagent_end", "子代理报告")):
        check(f"{kind} 有折叠高度 + 展开后不裁",
              f".ev.{kind} .bd {{" in html and f".ev.{kind}.open .bd {{ max-height: none; }}" in html)
        check(f"{kind} 的提示文案在（{hint}）",
              f'{kind}: "▸ 点击展开{hint}…"' in html)

    # --- 行为：判据照抄成 Python（markClip 就这一行判断）---
    def clip_needed(scroll_h: int, client_h: int) -> bool:
        return scroll_h > client_h + 2          # 差一两像素不算被裁

    check("1 行正文装得下 → 不提示（这就是用户点开啥都没有的那种卡）",
          not clip_needed(15, 72))
    check("3 行正文装得下 → 不提示", not clip_needed(46, 72))
    check("正好卡在边界上 → 不算被裁", not clip_needed(73, 72))
    check("20 行正文超了 → 提示出现", clip_needed(308, 72))
    check("展开之后再量不会误判（markClip 里对 .open 直接返回）",
          'if(!bd || el.classList.contains("open")) return;' in html)
    check("没有折叠提示那一层的卡不重量（resize 会把每张卡都过一遍）",
          'if(!el.querySelector(".more")) return;' in html,
          "少了它，那些卡会被挂上 .clip → cursor:pointer，看着能点、点了没反应")

    print("\n[10] 真 JS 引擎里跑一遍（不是 Python 复刻）")
    # [5] 和 [8] 的行为断言都是"把 JS 逻辑照抄成 Python 再跑"，那是无奈的折中：
    # 抄错了会得到**假的**通过/失败（2026-09-27 就因为抄漏一个 lookahead
    # 报过一次假失败，查了半天）。
    # 这一节换掉那个折中：把 index.html 的 `<script>` **整段**放进 QuickJS，
    # 配一套 DOM 桩，然后真调 fmtCallArgs / renderEvent，读它渲染出来的那张卡。
    # 没装 quickjs 就打印"跳过"，不假装通过。
    try:
        import quickjs  # noqa: F401
    except ImportError:
        SKIPPED.append("[10] 真 JS 引擎（缺 quickjs）")
        print("  ~ 跳过：本机没装 quickjs（`pip install quickjs` 之后这一节会自动跑）")
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("jsrun", here / "jsrun.py")
        jsrun = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(jsrun)

        sample = json.dumps({"path": "notes/market_log.md",
                             "content": "\n## [2026-09-27 07:30] 心跳 #34\n\n### 1. 账户快照\n"
                                        "- 总权益: 10,000.00 USDT"},
                            ensure_ascii=False)
        probe = (
            "const SAMPLE = " + json.dumps(sample, ensure_ascii=False) + ";\n"
            "const OUT = {\n"
            "  短参数: fmtCallArgs('{\"limit\": 5}'),\n"
            "  长参数: fmtCallArgs(SAMPLE),\n"
            "  字面反斜杠: fmtCallArgs(" + json.dumps(json.dumps({"re": "\\n"})) + "),\n"
            "  路径: fmtCallArgs(" + json.dumps(json.dumps({"path": "C:\\nope\\x"})) + "),\n"
            "};\n"
            "__stream.length = 0;\n"
            "renderEvent({kind:'tool_call', ts:'2026-09-27 07:30:19',\n"
            "             data:{tool:'fs_append', args:SAMPLE}});\n"
            "OUT.卡片 = __stream[__stream.length - 1].innerHTML;\n"
            "__RESULT = JSON.stringify(OUT);")
        out = json.loads(jsrun.run(probe))

        check("短参数仍然一行（真引擎）", "\n" not in out["短参数"], repr(out["短参数"]))
        check("多行参数摊成了多行（真引擎）",
              out["长参数"].count("\n") >= 6, repr(out["长参数"][:120]))
        check("摊开之后没有残留的字面 \\n（真引擎）",
              "\\n" not in out["长参数"], repr(out["长参数"][:200]))
        check("内容一个字没丢（真引擎）",
              "总权益: 10,000.00 USDT" in out["长参数"]
              and "心跳 #34" in out["长参数"])
        # 这两条是这次修掉的真缺陷，也是**唯一**能证明修对了的地方：
        # 值里一个正经的 `\n`（两行文本之间的内容，不是排版）必须原样留着。
        check("值里字面的反斜杠n 没被改成换行（真引擎）",
              "\\n" in out["字面反斜杠"] and out["字面反斜杠"].count("\n") == 1,
              repr(out["字面反斜杠"]))
        check("C:\\nope\\x 这种路径没被咬掉（真引擎）",
              "C:\\nope\\x" in out["路径"], repr(out["路径"]))
        card = out["卡片"]
        check("端到端：renderEvent 画出来的卡里也是多行的",
              card.count("\n") >= 6 and "总权益: 10,000.00 USDT" in card,
              repr(card[:160]))
        check("端到端：卡片挂了折叠提示（摊开后可能很长）",
              "点击展开完整参数" in card)

    print("\n[11] 重名定义（2026-09-27：两份 cutNote 互相盖，而且不报错）")
    # 教训很具体：改 cutNote 的时候留下了两份定义，后面那份把新的悄悄吃掉了。
    # 症状是**所有截断标记都不出现，而且一个错都不报** —— JS 允许重名函数，
    # 后定义的赢。单文件面板里这不是小事：`index.html` 两千多行、
    # 没有模块系统、没有 lint。数一遍是唯一划算的办法。
    # 只查**顶层**（`\n` 后紧跟关键字 = 行首无缩进）。
    # 函数体里的 `const m = ...` 同名是合法的，缩进了就不算 ——
    # 这条边界要写清楚，不然它会变成误报机器，然后被人关掉。
    FUNC_RE = re.compile(r"(?:^|\n)function\s+([A-Za-z_$][\w$]*)\s*\(")
    VAR_RE = re.compile(r"(?:^|\n)(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")

    def dup_names(src: str) -> dict:
        out = {}
        for rx, label in ((FUNC_RE, "函数"), (VAR_RE, "变量")):
            seen = [m.group(1) for m in rx.finditer(src)]
            for n in set(seen):
                if seen.count(n) > 1:
                    out[n] = f"{label}定义了 {seen.count(n)} 次"
        return out

    js_blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    dup = dup_names(js_blocks[0] if js_blocks else "")
    check("面板脚本里没有重名定义", not dup,
          f"{dup} —— 后定义的会静默盖掉前面的")
    check("检测器抓得到「两份同名函数」",
          dup_names("function f(a){}\nfunction f(a, b){}\n") != {},
          "检测器自己坏了就白搭")
    check("检测器不误报同名但不同文件/不同作用域之外的东西",
          dup_names("function a(){}\nfunction b(){}\n") == {})

    print("\n[11b] 「收住的结尾」这份白名单，前后端必须是同一串")
    # 后端读手账时（narrator._journal_tail）和前端画卡片时（cutNote）用的是
    # **同一套判据**。漂了的表现很隐蔽：后端给模型加了注、面板上却不标（或者反过来），
    # 两边单看都没毛病。所以直接比字面值。
    #
    # 2026-09-27 加了公开页（live.html），这份判据变成**三处**：
    # 公开页是从面板裁过来的同一个 cutNote，漂了的表现更隐蔽 ——
    # 游客看到的那份说"说完了"，而账本里那半句其实断着。
    m = re.search(r"const CUT_TAIL_OK = '([^']*)'", html)
    narr_path = here / "brain" / "trader" / "narrator.py"
    m2 = re.search(r"^TAIL_OK_CHARS = '([^']*)'", narr_path.read_text(encoding="utf-8"), re.M)
    live_path = next((p for p in (here / "brain" / "static" / "live.html",
                                  here / "static" / "live.html",
                                  pathlib.Path("/app/static/live.html")) if p.is_file()), None)
    m3 = re.search(r"const CUT_TAIL_OK = '([^']*)'",
                   live_path.read_text(encoding="utf-8")) if live_path else None
    check("三处都定义了这个白名单", bool(m and m2 and m3),
          f"面板 {bool(m)} / 后端 {bool(m2)} / 公开页 {bool(m3)}")
    if m and m2 and m3:
        check("面板 = 后端", m.group(1) == m2.group(1),
              f"面板 {m.group(1)!r} vs 后端 {m2.group(1)!r}")
        check("公开页 = 后端（游客看到的那份也得是同一把尺）", m3.group(1) == m2.group(1),
              f"公开页 {m3.group(1)!r} vs 后端 {m2.group(1)!r}")

    print("\n[12] 真 JS 引擎：截断标记只在有证据时出现")
    try:
        import quickjs  # noqa: F401
    except ImportError:
        SKIPPED.append("[12] 截断标记（缺 quickjs）")
        print("  ~ 跳过：本机没装 quickjs")
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("jsrun2", here / "jsrun.py")
        jsrun2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(jsrun2)

        # (用例名, 事件, 期望出不出标记)
        cases = [
            ("叙事：正常收尾", {"kind": "narrative",
                "data": {"text": "正常的一篇。", "model": "m", "finish_reason": "stop"}}, False),
            ("叙事：finish_reason=length", {"kind": "narrative",
                "data": {"text": "断在半句", "model": "m", "finish_reason": "length",
                         "truncated": True}}, True),
            ("叙事：后端只标了 truncated", {"kind": "narrative",
                "data": {"text": "断在半句", "model": "m", "truncated": True}}, True),
            ("子代理：结论被截", {"kind": "subagent_end",
                "data": {"job": "j", "model": "m", "ok": True, "answer": "结论", "truncated": True}}, True),
            ("子代理：完整", {"kind": "subagent_end",
                "data": {"job": "j", "model": "m", "ok": True, "answer": "结论。", "truncated": False}}, False),
            ("工具结果：面板那份被截（正文里有提示）", {"kind": "tool_result",
                "data": {"tool": "t", "result": "一大段\n…（面板只留前 1500 字符，完整结果已回灌给模型）"}}, True),
            # ★ 这一条是刻意留的：给模型的那份被截了，但**面板上是完整的**。
            #   这时候挂"截断"是错的（屏幕上明明全在），所以不许出声。
            ("工具结果：给模型截了但面板完整（不许出声）", {"kind": "tool_result",
                "data": {"tool": "t", "result": "一大段", "truncated": True}}, False),
            ("工具调用：参数被截", {"kind": "tool_call",
                "data": {"tool": "fs_write", "args": '{"content":"x…"…（参数已截断）'}}, True),
            ("系统消息：普通", {"kind": "system", "data": {"message": "已连接 MCP"}}, False),
            # ★ 这几条是"历史事件"：finish_reason / truncated 都还没有的那些
            #   （02:57 那篇就是），只能靠"结尾收不收得住"来认。
            ("叙事：历史事件，断在汉字（02:57 那篇的形状）", {"kind": "narrative",
                "data": {"text": "不如把欠下的睡眠配额补回来。BTC 站上 84,600 或者跌破 83,300，SOL 破 124 或"}}, True),
            ("叙事：历史事件，断在逗号", {"kind": "narrative",
                "data": {"text": "所以我决定，"}}, True),
            ("叙事：历史事件，正常收尾 → 不许出声", {"kind": "narrative",
                "data": {"text": "盘面很闷，我没动手。"}}, False),
            ("叙事：以分隔线收尾 → 不许出声", {"kind": "narrative",
                "data": {"text": "全现金，一手不动。\n\n***"}}, False),
            ("叙事：以引号收尾 → 不许出声", {"kind": "narrative",
                "data": {"text": '他说"再等等"'}}, False),
        ]
        probe = ("const OUT = [];\n"
                 "for(const [n, ev, want] of " + json.dumps(
                     [[n, e, w] for n, e, w in cases], ensure_ascii=False) + "){\n"
                 "  __stream.length = 0;\n"
                 "  renderEvent(ev);\n"
                 "  const el = __stream[__stream.length - 1];\n"
                 "  const html = el ? el.innerHTML : '';\n"
                 "  OUT.push([n, html.indexOf('✂') >= 0, want]);\n"
                 "}\n"
                 "__RESULT = JSON.stringify(OUT);")
        for name, got, want in json.loads(jsrun2.run(probe)):
            check(f"{name} → {'出标记' if want else '不出声'}",
                  got == want, "出了不该出的标记，或该出的没出")
        # 标记必须在 .bd 外面（.bd 是 overflow:hidden 的裁剪框，放里面会被裁掉）
        probe2 = ("__stream.length = 0;\n"
                  "renderEvent({kind:'narrative', data:{text:'x', finish_reason:'length', truncated:true}});\n"
                  "const h = __stream[__stream.length-1].innerHTML;\n"
                  "__RESULT = String(h.indexOf('class=\"bd\"') < h.indexOf('class=\"cut\"')"
                  " && h.indexOf('cut') > 0);")
        check("标记挂在 .bd **外面**（放里面会被 overflow 裁掉）",
              jsrun2.run(probe2) == "true")

    print("\n[13] 公开页 live.html（同一套静态检查 + 真 JS 引擎）")
    # 为什么它也要过这一遍：公开页是**另一份单文件**，同样会犯"少一个括号 →
    # 整段脚本不跑 → 页面永远停在初始状态"这种病（document 里那次事故的教训），
    # 而且它的症状更迷惑 —— 游客不会来告诉你"你那页是空的"，他们直接关掉。
    if live_path is None:
        SKIPPED.append("[13] 公开页静态检查（找不到 live.html）")
        print("  ~ 跳过：找不到 live.html")
    else:
        lhtml = live_path.read_text(encoding="utf-8")
        lblocks = re.findall(r"<script[^>]*>(.*?)</script>", lhtml, re.S)
        check("<script> 标签配平",
              lhtml.count("<script") == lhtml.count("</script>"))
        check("只有一段内联脚本", len(lblocks) == 1, f"实际 {len(lblocks)} 段")
        ljs = lblocks[0] if lblocks else ""
        lerrs = scan_js(ljs)
        check("整段脚本能被扫通，没有结构错", not lerrs,
              "\n      " + "\n      ".join(lerrs[:6]))
        for fn in ("loadHistory()", "connect()", "loadMarket()", "loadState()"):
            check(f"底部有 {fn} 调用", fn in ljs, "初始化没接上 = 页面永远是空的")
        lids = set(re.findall(r'\bid="([^"]+)"', lhtml))
        lrefs = set(re.findall(r'\$\("#([A-Za-z0-9_\-]+)"\)', lhtml))
        lmissing = sorted(lrefs - lids)
        check(f"引用的 {len(lrefs)} 个 id 都存在", not lmissing, f"缺失：{lmissing}")
        # 游客页**不许**有折起/展开这类需要"点一下"的东西就崩的交互依赖，
        # 但更重要的是：它不能引用面板里的 id（那会静默 null）
        check("公开页和面板是两份不同的文件（id 清单不同）",
              lids != set(re.findall(r'\bid="([^"]+)"', html)))

        print("\n  [13b] 真 JS 引擎：渲染器真的能跑（不是抄成 Python 再跑）")
        try:
            import quickjs  # noqa: F401
        except ImportError:
            SKIPPED.append("[13b] 公开页渲染（缺 quickjs）")
            print("  ~ 跳过：本机没装 quickjs")
        else:
            import importlib.util
            import os
            spec = importlib.util.spec_from_file_location("jsrun_live", here / "jsrun.py")
            jsrun3 = importlib.util.module_from_spec(spec)
            old = os.environ.get("PANEL_HTML")
            os.environ["PANEL_HTML"] = str(live_path)
            try:
                spec.loader.exec_module(jsrun3)
            finally:
                if old is None:
                    os.environ.pop("PANEL_HTML", None)
                else:
                    os.environ["PANEL_HTML"] = old
            probe = (
                "__stream.length = 0;\n"
                "renderEvent({kind:'thinking', ts:'2026-09-27 07:30:11',\n"
                "  data:{text:'### 盘面\\n\\n- **BTC** 站稳了'}});\n"
                "const think = __stream[__stream.length-1].innerHTML;\n"
                "__stream.length = 0;\n"
                "renderEvent({kind:'narrative', ts:'2026-09-27 02:57:00',\n"
                "  data:{text:'SOL 破 124 或', finish_reason:'length'}});\n"
                "const narr = __stream[__stream.length-1].innerHTML;\n"
                "__stream.length = 0;\n"
                "renderEvent({kind:'tool_call', ts:'2026-09-27 07:30:12',\n"
                "  data:{tool:'fs_write', args:JSON.stringify({path:'notes.md', content:'abcdefghij'})}});\n"
                "const call = __stream[__stream.length-1].innerHTML;\n"
                "__stream.length = 0;\n"
                "renderEvent({kind:'wake_policy_changed', ts:'2026-09-27 07:30:13',\n"
                "  data:{by:'human'}});\n"
                "const wake = __stream[__stream.length-1].innerHTML;\n"
                "__RESULT = JSON.stringify({think:think, narr:narr, call:call, wake:wake});")
            out = json.loads(jsrun3.run(probe))
            check("markdown 真渲染成块级结构（不是把 ### 原样打出来）",
                  '<div class="md-h' in out["think"] and "### " not in out["think"])
            check("思维链的正文还在", "BTC" in out["think"])
            check("被截的叙事出 ✂ 标记", "✂" in out["narr"])
            check("被截的参数照原样留着，不吞字",
                  "abcdefghij" in out["call"] and "fs_write" in out["call"])
            check("唤醒策略变更只剩「谁改的」",
                  "人类改的" in out["wake"] and "阈值" not in out["wake"])

    print("\n[14] 留言框那句话（2026-09-27：placeholder 承诺「立即唤醒」，两半都是假的）")
    # 这条守的不是功能，是**一句真话**。面板上那句话原来是
    # 「发送后将立即唤醒并作为最高优先级指令」—— 睡着时叫不醒（`decide()` 只认
    # 登记过的 `request_wake`），清醒时也只是把 `wake_event` 置一下、没到兜底间隔
    # 照样不跑。而它真正的语义是确定的：**下一轮真的跑起来的心跳**开头注入。
    #
    # 为什么值得一条测试：假话的表现是"看着像送到了"（事件流里那行
    # human_instruction 确实在），而人是靠那句话决定要不要再点「立即唤醒」的。
    # 只查 `<input id="instr">` 那个 placeholder **属性本身**，不查全文 ——
    # 上头那段注释里就引用了原来那句假话（解释"它错在哪"），
    # 全文匹配会被自己的注释绊倒。（跟 live.html 那次同一个教训的两个面：
    # 那次是"注释把审计搞脏"，这次是"注释把检查搞红"。）
    ph = re.search(r'<input id="instr"[^>]*placeholder="([^"]*)"', html)
    check("找得到留言框的 placeholder", bool(ph))
    if ph:
        check("placeholder 里不再承诺「立刻醒」",
              "立即唤醒并" not in ph.group(1) and "将立即唤醒" not in ph.group(1),
              f"实际：{ph.group(1)}")
        check("placeholder 讲了它真正的语义（最高优先级指令 + 不会因此立刻醒）",
              "最高优先级" in ph.group(1) and "不会" in ph.group(1), f"实际：{ph.group(1)}")
    check("有 instrHint()，而且跟着睡眠状态走", "function instrHint(" in js)
    check("instrHint 真被 renderWake 调了（不然它就是一段没人用的死代码）",
          'const hint = instrHint(w);' in js and '$("#instr").placeholder = hint;' in js)
    try:
        import quickjs  # noqa: F401
    except ImportError:
        SKIPPED.append("[14] 留言框文案（缺 quickjs）")
        print("  ~ 跳过：本机没装 quickjs")
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("jsrun_hint", here / "jsrun.py")
        jsrun_h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(jsrun_h)
        probe = ("__RESULT = JSON.stringify({"
                 "睡: instrHint({sleep:{sleeping:true}}),"
                 "醒: instrHint({sleep:{sleeping:false}}),"
                 "空: instrHint(null)});")
        got = json.loads(jsrun_h.run(probe))
        check("睡着时的那句点明了「留言不会叫醒它」", "不会叫醒" in got["睡"], got["睡"])
        check("睡着时的那句指了正确的出路（「立即唤醒」）", "立即唤醒" in got["睡"])
        check("清醒时不再承诺「立刻醒」", "立刻醒" in got["醒"] or "不会" in got["醒"])
        check("没配唤醒模块时不炸、也不撒谎", isinstance(got["空"], str) and got["空"])

    print("\n" + "=" * 68)
    if FAILS:
        print(f"  失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"    · {f}")
        print("=" * 68)
        return 1
    print("  全部通过")
    if SKIPPED:
        print("  ⚠ 但有 %d 节被跳过（跳过不算通过）：%s"
              % (len(SKIPPED), "、".join(SKIPPED)))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
