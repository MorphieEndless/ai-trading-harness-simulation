"""统计每轮心跳里子代理 / 运行时提醒 / 工具调用的使用情况。"""
import collections
import json
import pathlib
import sys

p = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data/logs/events.jsonl")
rows = [json.loads(l) for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
print("总事件", len(rows))
print(dict(collections.Counter(r["kind"] for r in rows)))
print()

beats = []
cur = None
for r in rows:
    if r["kind"] == "beat_start":
        cur = {"n": r["data"].get("beat"), "ts": r["ts"], "ev": collections.Counter(), "dur": None}
        beats.append(cur)
    if cur is not None:
        cur["ev"][r["kind"]] += 1
        if r["kind"] == "beat_end":
            cur["dur"] = r["data"].get("duration")

print("%-7s %-9s %5s %5s %5s %5s %7s" % ("beat", "start", "sub", "nudge", "tool", "think", "秒"))
for b in beats:
    e = b["ev"]
    print("%-7s %-9s %5d %5d %5d %5d %7s" % (
        b["n"], b["ts"][11:19], e.get("subagent_start", 0), e.get("cost_nudge", 0),
        e.get("tool_call", 0), e.get("thinking", 0), b["dur"]))

print()
print("=== 子代理明细 ===")
for r in rows:
    d = r["data"]
    t = r["ts"][11:19]
    if r["kind"] == "subagent_start":
        print(t, "派发", d.get("model"), "|", str(d.get("want") or d.get("task") or "")[:90])
    elif r["kind"] == "subagent_end":
        print(" " * 8, "->", d.get("model"), d.get("duration"), "s ok=", d.get("ok"),
              "答案", len(str(d.get("answer") or "")), "字")
    elif r["kind"] == "cost_nudge":
        print(t, "!! 运行时提醒：第", d.get("calls"), "次工具调用")
