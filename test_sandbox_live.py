#!/usr/bin/env python3
"""部署后的沙箱验收 —— 打的不是源码，是**正在跑的那个容器**。

为什么要有这一份：Landlock 是内核行为，源码里写着"我会挂规则"不代表线上真挂上了。
内核版本不够、被人 SHELL_SANDBOX=off 关掉、镜像里烤的是旧代码 —— 这三种情况
纯逻辑测试全都看不见。所以每次动过 run_shell 之后，都在线上再打一遍。

它走的是**真路线**：`trader.tools_local.LocalTools.call("run_shell", ...)`，
和 Agent 心跳里调的是同一个函数。

跑法（在 165 上，或者有 docker 权限的机器上）：
  python3 test_sandbox_live.py
容器没起来时会打印「跳过」并以 0 退出 —— 不把"没测"伪装成"通过"。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys

CONTAINER = "trader-brain"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def docker(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


#: 各种"被拒绝"的样子。刻意不止 Permission denied —— 拒你的可能是不同的一层：
#:   Permission denied    ← Landlock（EACCES），或者 Unix 文件权限
#:   Read-only file system ← 容器根文件系统只读（EROFS）
#:   Operation not permitted ← no_new_privs / 内核
#: 测的是"这件事被拒绝了"，不是"被哪一层拒绝的"。想单独验某一层，去第六节看分层。
DENY_WORDS = ("Permission denied", "Operation not permitted", "Read-only file system",
              "沙箱拒绝", "PermissionError")


def _exit_code(out: str) -> int | None:
    m = re.search(r"exit=(\d+)", out)
    return int(m.group(1)) if m else None


def shell_via_tool(command: str) -> str:
    """通过 LocalTools 的真调用路径跑一条 shell 命令。"""
    src = (
        "import asyncio\n"
        "from trader.config import Config\n"
        "from trader.tools_local import LocalTools\n"
        "print(asyncio.run(LocalTools(Config()).call('run_shell', "
        f"{{'command': {command!r}}})))\n"
    )
    r = docker("exec", "-u", "trader", "-w", "/app", CONTAINER, "python3", "-c", src)
    return (r.stdout or "") + (r.stderr or "")


def main() -> int:
    print("=" * 68)
    print("run_shell 沙箱 · 线上验收（打的是容器里正在跑的那份代码）")
    print("=" * 68)

    if not shutil.which("docker"):
        print("  ⚠ 跳过 —— 这台机器上没有 docker。")
        return 0
    ps = docker("ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}")
    if CONTAINER not in (ps.stdout or ""):
        print(f"  ⚠ 跳过 —— {CONTAINER} 没在跑，验收不了。")
        return 0

    # 前置：进程是不是 non-dumpable（这个和 Landlock 是两条独立的防线）
    print("  （两道防线：Landlock 管文件系统，non-dumpable 管 /proc —— 分别验）")

    print("\n[1] 必须被拒绝（走真路线：LocalTools.call('run_shell', ...)）")
    for name, cmd in [
        ("读叙事手账 journal.md", "head -c 40 /data/logs/journal.md"),
        ("写 /data/logs", "echo x >> /data/logs/zzz-live-probe"),
        ("读 /proc/1/environ（凭证）", "cat /proc/1/environ"),
        ("列根目录 /", "ls /"),
        ("往 /etc 写（这一条其实是只读根 FS 挡的，不是 Landlock）",
         "echo x > /etc/zzz-live-probe"),
    ]:
        out = shell_via_tool(cmd)
        code = _exit_code(out)
        denied = code not in (0, None) and any(w in out for w in DENY_WORDS)
        check(name, denied, out.strip()[:160])

    print("\n[1b] 第二道防线：non-dumpable（**不经过** run_shell 也读不到 brain 的环境）")
    # 这条走的是 docker exec —— 一个和 brain 同 uid 的兄弟进程，完全绕开 Landlock。
    # 挡住它的只有 prctl(PR_SET_DUMPABLE, 0)。两件事分开验，才知道是哪一道在响。
    r = docker("exec", "-u", "trader", CONTAINER, "sh", "-c",
               "head -c 40 /proc/1/environ")
    out = (r.stdout or "") + (r.stderr or "")
    check("同 uid 的兄弟进程读 /proc/1/environ → 拒绝",
          r.returncode != 0 and "Permission denied" in out, out.strip()[:160])
    check("确认读到的不是内容（不能出现 KEY=）", "KEY=" not in out, out.strip()[:160])

    print("\n[2] 必须能跑（沙箱不能把正常活儿一起掐死）")
    for name, cmd in [
        ("python3 计算", "python3 -c \"print(sum(range(10)))\""),
        ("工作区写读", "echo hello > /data/workspace/.sbx-live && cat /data/workspace/.sbx-live"),
        ("清理现场", "rm -f /data/workspace/.sbx-live"),
        ("读自己所在目录", "ls /data/workspace | head -3"),
    ]:
        out = shell_via_tool(cmd)
        check(name, "exit=0" in out, out.strip()[:160])

    print("\n[3] 沙箱自己有没有在骗人")
    r = docker("exec", "-u", "trader", CONTAINER, "sh", "-c",
               "python3 -c \"import sys;sys.path.insert(0,'/app');"
               "from trader import sandbox;print('ABI', sandbox.available());"
               "print('MODE', __import__('trader.config',fromlist=['cfg']).cfg.shell_sandbox)\"")
    out = (r.stdout or "") + (r.stderr or "")
    print(f"  {out.strip()}")
    check("Landlock 在容器里可用（ABI > 0）", "ABI 0" not in out, out.strip()[:160])
    check("不是被 SHELL_SANDBOX=off 关掉的", "MODE off" not in out, out.strip()[:160])

    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "; ".join(FAILS))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
