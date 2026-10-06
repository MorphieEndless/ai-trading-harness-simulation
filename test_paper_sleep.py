#!/usr/bin/env python3
"""mcp-paper 睡眠账本的测试 —— 打真代码、**不碰真账本**。

为什么要有这一份：睡眠配额是整个项目里唯一"Agent 改不到"的成本闸门，
配额放在 SQLite 里就是为了这个。而 2026-09-27 那次改动动了 `sleep_start` /
`sleep_end` 的事务边界（加 `BEGIN IMMEDIATE` 做幂等）—— 那是权威账本上的写操作，
没有测试守着太危险。

做法：起一个 **一次性 DB**（`PAPER_DB=/tmp/...`），把 `server.py` 当模块导进来，
直接调那几个函数。所以它验的是镜像里真正在跑的那份代码，而真账本一个字节都不动。

跑法（在 165 上，或者有 docker 权限的机器上）：
  python3 test_paper_sleep.py
容器没起来时打印「跳过」并以 0 退出。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

CONTAINER = "trader-mcp-paper"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


# 在容器里跑的探针。注意：它只跟一个临时 DB 打交道。
PROBE = r'''
import json, os, sqlite3, sys
sys.path.insert(0, "/app")
import server

print(json.dumps({"db": server.DB_PATH}), flush=True)
server.init_db()

r1 = server.sleep_start("第一次")
r2 = server.sleep_start("第二次（重复调用）")
s_dup = server.sleep_state()
r3 = server.sleep_start("第三次（还在睡时又来一次）")
r4 = server.sleep_end("测试结束")
s_end = server.sleep_state()
r5 = server.sleep_end("不在睡的时候再结束一次")

con = sqlite3.connect(server.DB_PATH)
rows = con.execute(
    "SELECT id, ended_at IS NULL AS open, reason FROM sleep_log ORDER BY id"
).fetchall()

print(json.dumps({
    "first_ok": r1.get("ok"),
    "second_ok": r2.get("ok"),
    "second_msg": r2.get("message"),
    "third_ok": r3.get("ok"),
    "dup_sleeping": s_dup.get("sleeping"),
    "end_ok": r4.get("ok"),
    "after_end_sleeping": s_end.get("sleeping"),
    "second_end_ok": r5.get("ok"),
    "rows": [list(r) for r in rows],
    "open_rows": sum(1 for r in rows if r[1]),
}, ensure_ascii=False))
'''


def main() -> int:
    print("=" * 68)
    print("mcp-paper 睡眠账本 · 一次性 DB 上的真代码测试")
    print("=" * 68)

    if not shutil.which("docker"):
        print("  ⚠ 跳过 —— 这台机器上没有 docker。")
        return 0
    ps = subprocess.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$",
                         "--format", "{{.Names}}"], capture_output=True, text=True, timeout=60)
    if CONTAINER not in (ps.stdout or ""):
        print(f"  ⚠ 跳过 —— {CONTAINER} 没在跑。")
        return 0

    db_path = "/tmp/sleep-test-$$.db"
    r = subprocess.run(
        ["docker", "exec", "-i", "-e", f"PAPER_DB={db_path}", "-u", "trader", CONTAINER,
         "python3", "-"],
        input=PROBE, capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        print(f"  ✗ 探针没跑起来\n{(r.stdout or '')[-800:]}\n{(r.stderr or '')[-800:]}")
        return 1

    lines = [ln for ln in out.splitlines() if ln.strip().startswith("{")]
    head = json.loads(lines[0])
    data = json.loads(lines[-1])
    check("一次性 DB 真的被用上了（没碰 /data/db/trader.db）",
          head["db"].startswith("/tmp/"), head["db"])
    check("第一次 sleep_start → ok", data["first_ok"] is True)
    check("第二次 sleep_start → 明确拒绝（消息里点名）",
          data["second_ok"] is False and "已经在睡眠" in (data["second_msg"] or ""),
          str(data["second_msg"]))
    check("第三次 likewise", data["third_ok"] is False)
    check("重复调用期间状态仍是「睡眠中」", data["dup_sleeping"] is True)
    check("sleep_end → ok", data["end_ok"] is True)
    check("结束之后不再是睡眠态", data["after_end_sleeping"] is False)
    check("不在睡时再 end → 明确拒绝", data["second_end_ok"] is False)
    check("账本里只有一行（重复调用没插出脏行）", len(data["rows"]) == 1, str(data["rows"]))
    check("收尾后没有开着的行", data["open_rows"] == 0, str(data["rows"]))

    subprocess.run(["docker", "exec", "-u", "trader", CONTAINER, "rm", "-f", db_path],
                   capture_output=True, text=True, timeout=60)

    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "; ".join(FAILS))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
