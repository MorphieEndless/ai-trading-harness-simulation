import json
import pathlib
import subprocess
import sys
from collections import Counter

ROOT = pathlib.Path("/opt/ai-trader")
env = (ROOT / ".env").read_text(encoding="utf-8")
pw = ""
for line in env.splitlines():
    if line.startswith("WEBUI_PASSWORD="):
        pw = line.split("=", 1)[1].strip()

out = subprocess.run(
    ["curl", "-sS", "-m", "25", "-u", f"trader:{pw}", "http://127.0.0.1:18080/api/state"],
    capture_output=True, text=True,
).stdout

try:
    s = json.loads(out)
except Exception as e:
    print("状态读取失败:", e, out[:200])
    sys.exit(1)

st = s.get("status", {}) or {}
a = s.get("account", {}) or {}
pf = s.get("performance", {}) or {}

print("--- Agent ---")
for k, label in [("beats", "心跳数"), ("last_beat_at", "上次心跳开始"), ("next_beat_at", "下次心跳"),
                 ("next_beat_seconds", "倒计时(秒)"), ("tokens_used", "累计token"),
                 ("last_error", "最近错误")]:
    print(f"  {label:<14} {st.get(k)}")
print(f"  {'工具数':<14} {st.get('tool_count')}   MCP: " +
      ", ".join(f"{n}({v['tools']})" for n, v in (st.get("mcp") or {}).items()))

sub = st.get("subagent") or {}
if sub.get("enabled"):
    print(f"  {'子代理':<14} {len(sub.get('models') or [])} 个模型 · 默认 {sub.get('default_model')} · 并发上限 {sub.get('max_concurrency')}")
    print(f"  {'':<14} 调用 {sub.get('calls')} 次 · 本轮 {sub.get('per_beat_used')}/{sub.get('max_per_beat')} · "
          f"当前在跑 {sub.get('active')} · 失败 {sub.get('errors')} 次")
    for m in (sub.get("models") or []):
        tk = (sub.get("tokens_by_model") or {}).get(m["id"], 0)
        mark = "★" if m["id"] == sub.get("default_model") else " "
        print(f"  {mark} {m['id']:<24} key={m.get('key_source','?'):<22} {tk} token")
    if sub.get("last_error"):
        print(f"  {'子代理错误':<14} {sub['last_error']}")
    if not sub.get("tools_supported", True):
        print(f"  {'':<14} ⚠ 已降级为纯文本模式（端点不支持工具调用）")
else:
    print(f"  {'子代理':<14} 未配置（delegate 工具未暴露给主 Agent）")

per = st.get("persona") or {}
print(f"  {'人格':<14} " + (f"已加载 {per.get('chars')} 字符（{per.get('file')}）"
                             if per.get("loaded") else "未设置（使用默认性格）"))

print("--- 账户 ---")
if a.get("error"):
    print("  ", a["error"])
else:
    print(f"  权益 {a.get('equity')}   现金 {a.get('cash')}   收益率 {a.get('total_return_pct')}%")
    print(f"  未实现 {a.get('unrealized_pnl')}   已实现 {a.get('realized_pnl')}   手续费 {a.get('fees_paid')}")
    pos = a.get("positions") or []
    for p in pos:
        print(f"    {p.get('symbol'):<10} 数量 {p.get('qty')}  成本 {p.get('avg_cost')}  "
              f"现价 {p.get('mark')}  浮盈 {p.get('unrealized_pnl')}  止损 {p.get('stop_loss')}")
    if not pos:
        print("    (空仓)")
if a.get("WARNING"):
    print("  ⚠ ", a["WARNING"])

print("--- 成交 ---")
tr = s.get("trades")
if isinstance(tr, dict):
    print("  ", tr.get("error") or tr)
    tr = []
for t in (tr or [])[:8]:
    print(f"  {t.get('ts')} {t.get('side'):<4} {t.get('symbol'):<10} "
          f"${t.get('notional')} pnl={t.get('realized_pnl')}")
if not tr:
    print("  (无)")

print("--- 事件统计 ---")
rows = [json.loads(l) for l in (ROOT / "data/logs/events.jsonl")
        .read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
print(" ", dict(Counter(r["kind"] for r in rows)))
