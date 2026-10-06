#!/usr/bin/env python3
"""把面板的 <script> **整段**放进真 JS 引擎跑（带一套极简 DOM 桩）。

# 为什么不是"把函数抠出来"

抠函数要自己判断"这个函数到哪结束"，而那段判断本身就有坑：
`fmtCallArgs` 里有 `/[\n\r\t]|\\[nrt]/` 这种**正则字面量**，
第一版扫描器把正则里的 `"` 当成了字符串开头，于是顺着后面的
`<div class="more">` 一路吞下去，把半个文件都抠进了"函数"里 ——
跑起来报的是 `window is not defined`，看起来像环境问题，其实是抠错了。

整段跑就没有这个问题：那些 `document.querySelector("#instr").addEventListener`
之类的顶层语句，给一套**自动造节点的桩**就能全过。

# 它是什么样的测试

- **顶层全部执行**：`mkRestore()` / `renderMarket()` / 所有事件绑定真的跑一遍
  （所以"初始化少了一句"这类错它也能抓）。
- **异步全部挂起**：`fetch` 返回一个永不 settle 的 Promise。
  于是 tick / loadMarket / loadHistory 都停在那一行，不产生副作用也不报未处理拒绝。
- 想验什么，就往 `#stream` 里 `renderEvent(...)` 一条，然后读那张卡。

# 用法

    python3 .tmp/jsrun.py --js .tmp/cases/args_probe.js
    python3 .tmp/jsrun.py --cases .tmp/cases/args_live.jsonl --call 'fmtCallArgs(args)'

装了 quickjs 才行（`pip install quickjs`）；没装就打印"跳过"，退出码 3。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
# PANEL_HTML=... 可以换一份来跑（给 test_frontend.py 的自我校验用：
# 把面板改坏一份，确认这些检查真的会报红，而不是永远绿的摆设）。
CANDIDATES = ([pathlib.Path(os.environ["PANEL_HTML"])] if os.environ.get("PANEL_HTML")
              else [ROOT / "brain" / "static" / "index.html",
                    ROOT / "static" / "index.html",
                    pathlib.Path("/app/static/index.html")])

# ---------------------------------------------------------------- DOM 桩
STUB = r"""
var __logs = [], __nodes = {}, __stream = [];
function __cl(el, init){ var s = new Set(String(init||"").split(/\s+/).filter(Boolean));
  return { add:(...x)=>x.forEach(v=>s.add(v)), remove:(...x)=>x.forEach(v=>s.delete(v)),
           contains:(v)=>s.has(v), toggle:(v)=>s.has(v)?(s.delete(v),false):(s.add(v),true),
           _set:s, toString:()=>[...s].join(" ") }; }
function __node(tag){
  const n = {
    tagName: String(tag||"div").toUpperCase(), _kids: [], _html: "", _text: "",
    classList: __cl(null, ""), style: {}, dataset: {}, attrs: {}, onclick: null,
    scrollHeight: 0, clientHeight: 0, scrollTop: 0, clientWidth: 320, scrollWidth: 0,
    appendChild(c){ this._kids.push(c); if(this === __nodes["#stream"]) __stream.push(c);
                    c.parentNode = this; return c; },
    insertAdjacentHTML(pos, h){ this._html += h; },
    querySelector(sel){ if(!this._q) this._q = {}; if(!this._q[sel]) this._q[sel] = __node("div");
                        return this._q[sel]; },
    querySelectorAll(){ return []; },
    setAttribute(k,v){ this.attrs[k]=v; }, getAttribute(k){ return this.attrs[k] ?? null; },
    addEventListener(){}, removeEventListener(){}, remove(){ this._removed = true; },
    focus(){}, blur(){}, getBoundingClientRect(){ return {width:320,height:100,top:0,left:0}; },
  };
  Object.defineProperty(n, "className", { get(){ return n.classList.toString(); },
    set(v){ n.classList = __cl(n, v); } });
  Object.defineProperty(n, "innerHTML", { get(){ return n._html; },
    set(v){ n._html = String(v); n._kids = []; } });
  Object.defineProperty(n, "textContent", { get(){ return n._text; },
    set(v){ n._text = String(v); } });
  return n;
}
var document = {
  querySelector(sel){ if(!__nodes[sel]) __nodes[sel] = __node(sel.replace(/[#.].*/, "d"));
                      return __nodes[sel]; },
  querySelectorAll(){ return []; },
  createElement(t){ return __node(t); },
  addEventListener(){}, body: __node("body"),
};
var window = { addEventListener(){}, innerWidth: 1280, innerHeight: 800 };
var localStorage = { _d:{}, getItem(k){ return this._d[k] ?? null; },
                     setItem(k,v){ this._d[k]=String(v); }, removeItem(k){ delete this._d[k]; } };
var navigator = { userAgent: "quickjs" };
var alert = function(){}; var confirm = function(){ return false; };
var fetch = function(){ return new Promise(function(){}); };     /* 永不 settle */
var EventSource = function(){ this.onmessage = null; this.onerror = null; };
function setInterval(){ return 0; } function clearInterval(){}
function setTimeout(){ return 0; } function clearTimeout(){}
function requestAnimationFrame(){ return 0; }
var __RESULT = null;
"""


def find_html() -> pathlib.Path:
    for p in CANDIDATES:
        if p.is_file():
            return p
    sys.exit("找不到 index.html")


def script_block() -> str:
    html = find_html().read_text(encoding="utf-8")
    m = re.search(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not m:
        sys.exit("index.html 里没有内联 <script>")
    return m.group(1)


def build_code(extra: str) -> str:
    """DOM 桩 + 面板脚本原文 + 追加的探针。"""
    return STUB + "\n/* ===== 面板脚本原文 ===== */\n" + script_block() + "\n" + extra


def run(extra: str, result_name: str = "__RESULT"):
    """把面板脚本和探针一起跑起来，返回探针写的那个变量。

    给 test_frontend.py 的 [10] 用 —— 它要断言的是**真 JS 的行为**，
    不是把逻辑抄成 Python 再跑一遍。
    """
    import quickjs
    ctx = quickjs.Context()
    ctx.add_callable("__print", lambda *xs: None)
    ctx.eval('var console={log:()=>{},warn:()=>{},error:()=>{},info:()=>{},debug:()=>{}};')
    ctx.eval(build_code(extra))
    return ctx.get(result_name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--js", help="追加的探针 JS（里面可以设 __RESULT）")
    ap.add_argument("--call", help="对每份输入求值的表达式（配合 --cases）")
    ap.add_argument("--cases", help="每行一个 JSON 字符串的用例文件")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--html", help="换一份 index.html 来跑（默认用仓库里那份）")
    a = ap.parse_args()

    if a.html:
        os.environ["PANEL_HTML"] = a.html

    try:
        import quickjs
    except ImportError:
        print("没有 quickjs（pip install quickjs），跳过。", file=sys.stderr)
        return 3

    extra = ""
    if a.cases:
        items = [json.loads(l) for l in
                 pathlib.Path(a.cases).read_text(encoding="utf-8").splitlines() if l.strip()]
        extra += "const __CASES = " + json.dumps(items, ensure_ascii=False) + ";\n"
    if a.js:
        extra += pathlib.Path(a.js).read_text(encoding="utf-8")
    elif a.call:
        if not a.cases:
            sys.exit("--call 要配 --cases")
        extra += ("const __OUT = [];\n"
                  "for(const args of __CASES){ __OUT.push(" + a.call + "); }\n"
                  "__RESULT = __OUT;\n")
    pathlib.Path("/tmp/panel_run.js").write_text(build_code(extra), encoding="utf-8")
    res = run(extra)
    if res is not None:
        print(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
