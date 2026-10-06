"""打印最近一次心跳的完整叙事：思维链 + 工具调用 + 最终结论。"""
import json
import pathlib
import sys
from collections import deque

ROOT = pathlib.Path("/opt/ai-trader")
n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
lines = [l for l in (ROOT / "data/logs/events.jsonl")
         .read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
rows = [json.loads(l) for l in lines[-n:]]

for e in rows:
    k = e.get("kind")
    d = e.get("data", {})
    ts = (e.get("ts") or "")[11:]
    if k == "beat_start":
        print(f"\n{'='*70}\n[{ts}] ▶ 心跳 #{d.get('beat')} 开始（{d.get('trigger')}）\n{'='*70}")
    elif k == "thinking":
        t = (d.get("text") or "").strip().replace("\n", " ")
        print(f"[{ts}] 💭 思考: {t[:600]}{'…' if len(t) > 600 else ''}")
    elif k == "tool_call":
        print(f"[{ts}] 🔧 调用 {d.get('tool')}  {str(d.get('args'))[:200]}")
    elif k == "tool_result":
        r = str(d.get("result") or "").replace("\n", " ")
        print(f"[{ts}]    ↳ {r[:220]}{'…' if len(r) > 220 else ''}")
    elif k == "agent_text":
        print(f"[{ts}] 🗣  发言: {(d.get('text') or '')[:700]}")
    elif k == "beat_summary":
        print(f"\n[{ts}] 📋 本轮结论（{d.get('duration')}s）:\n{(d.get('text') or '')[:2500]}\n")
    elif k == "trade":
        print(f"[{ts}] 💰 成交 {d.get('side')} {d.get('symbol')} @ {d.get('price')} 理由={d.get('reason')}")
    elif k == "error":
        print(f"[{ts}] ❌ {d.get('message')}")
    elif k == "beat_end":
        print(f"[{ts}] ⏹  心跳结束 ok={d.get('ok')} 耗时={d.get('duration')}s")
    elif k == "system":
        print(f"[{ts}] ⚙  {d.get('message')}")
