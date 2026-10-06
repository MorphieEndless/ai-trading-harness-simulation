#!/usr/bin/env python3
"""run_shell 沙箱的测试。不联网、不花钱。

守的是这几条（每一条都对应一次真实的踩坑，不是想象出来的边界）：

  1. 白名单里**不能出现** /proc 和 /data。
     前者是凭证：brain 自己的初始环境块在 /proc/<pid>/environ 里，读到了就是
     明文 LLM_API_KEY；后者是叙事层「单向性」的命门（journal.md）。
  2. 子进程的环境变量里不能有任何凭证 —— 而且是「不管调用方怎么起我」都不能有。
     这个洞实测踩过：只靠调用方传 env 的话，直接 `python3 sandbox.py ...` 起
     wrapper 时，继承下来的环境里原样躺着 LLM_API_KEY。
  3. 权限位要按 ABI 降级。多传一位内核不认识的，create_ruleset 直接 EINVAL ——
     在 6.1（ABI 2）上传 TRUNCATE 的后果是"整层沙箱静默失效"。
  4. 规则真挂上之后：工作区 / /tmp 能读写，/proc 和 / 读不到。
  5. 参数错了要报错退出，不能当"没有命令"静默通过。

本机没有 Landlock 时，第 4 段打印「跳过」而不是「通过」——
把跳过伪装成通过，是这一类测试最容易骗自己的地方。

跑法：bash runtests.sh sandbox
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"  ⚠ 跳过 {name} —— {why}")


def _find_sandbox() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "brain" / "trader" / "sandbox.py",
                 here / "trader" / "sandbox.py",
                 pathlib.Path("/t/trader/sandbox.py")):
        if cand.is_file():
            return cand
    sys.exit("找不到 sandbox.py")


SANDBOX_PATH = _find_sandbox()
spec = importlib.util.spec_from_file_location("sandbox_under_test", SANDBOX_PATH)
sbx = importlib.util.module_from_spec(spec)
sys.modules["sandbox_under_test"] = sbx
spec.loader.exec_module(sbx)


def _run_in_sandboxed_child(module, workspace: str, path: str, mode: str,
                            strict: bool = False) -> str:
    """fork 一个孩子，挂上规则，试一次读写；返回 ok / denied:<异常名> / apply-failed:.."""
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:                                    # 孩子
        os.close(rfd)
        try:
            module.apply_landlock(workspace, strict_paths=strict)
        except Exception as exc:
            os.write(wfd, f"apply-failed:{exc}".encode())
            os._exit(0)
        try:
            if mode == "read":
                with open(path, "rb") as fh:
                    fh.read(16)
            elif mode == "list":
                os.listdir(path)
            elif mode == "write":
                with open(path, "w") as fh:
                    fh.write("x")
            else:
                os.write(wfd, b"bad-mode")
                os._exit(0)
            os.write(wfd, b"ok")
        except Exception as exc:
            os.write(wfd, f"denied:{type(exc).__name__}".encode())
        os._exit(0)
    os.close(wfd)
    data = b""
    while True:
        chunk = os.read(rfd, 4096)
        if not chunk:
            break
        data += chunk
    os.close(rfd)
    os.waitpid(pid, 0)
    return data.decode("utf-8", "replace")


def main() -> int:
    print("=" * 68)
    print("run_shell 沙箱（Landlock）· 测试")
    print("=" * 68)
    print(f"  sandbox.py = {SANDBOX_PATH}")
    src = SANDBOX_PATH.read_text(encoding="utf-8")

    # ============================================================ 1 白名单
    print("\n[1] 白名单：该在哪、不该在哪")
    ro, rw = set(sbx.READONLY_ROOTS), set(sbx.READWRITE_ROOTS)
    check("/proc 不在名单里（那是凭证泄漏口）",
          "/proc" not in ro | rw, str(sorted(ro | rw)))
    check("/data 不在名单里（叙事手账 + 事件日志在那儿）",
          not any(p == "/data" or p.startswith("/data/") for p in ro | rw))
    check("根目录 / 不在名单里（在的话整层等于没挂）", "/" not in ro | rw)
    check("解释器与共享库在名单里（不然 python3 起不来）",
          {"/usr", "/bin", "/lib"}.issubset(ro), str(sorted(ro)))
    check("全权目录只有工作区和 /tmp",
          set(sbx.writable_roots("/data/workspace")) == {"/data/workspace", "/tmp"})
    check("工作区路径跟着参数走（换个工作区也不能漏）",
          sbx.writable_roots("/x/y") == ("/x/y", "/tmp"))

    # ============================================================ 2 环境变量（静态）
    print("\n[2] 子进程环境变量：结构上就不带凭证")
    env = sbx.child_env("/data/workspace")
    banned = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
    check("键名里没有 KEY/TOKEN/SECRET/PASSWORD",
          not any(b in k.upper() for k in env for b in banned), str(sorted(env)))
    check("值里没有 sk- 形状的东西", not any("sk-" in v for v in env.values()))
    check("HOME 与 PWD 指向工作区", env.get("HOME") == env.get("PWD") == "/data/workspace")
    check("有 PATH 和 TMPDIR=/tmp", bool(env.get("PATH")) and env.get("TMPDIR") == "/tmp")

    # ============================================================ 3 环境变量（动态，真踩过）
    print("\n[3] 父进程带着密钥起 wrapper —— 不许漏下去（这条是回归测试）")
    probe_env = dict(os.environ, LLM_API_KEY="sk-CANARY-DO-NOT-LEAK")
    for mode in ("off", "auto"):
        probe_env["SHELL_SANDBOX"] = mode
        r = subprocess.run(
            [sys.executable, str(SANDBOX_PATH), "--workspace", "/tmp", "--",
             "/bin/sh", "-c", "env"],
            capture_output=True, text=True, timeout=60, env=probe_env)
        out = (r.stdout or "") + (r.stderr or "")
        check(f"SHELL_SANDBOX={mode}：env 里没有那把密钥", "CANARY" not in out, out[:200])
        check(f"SHELL_SANDBOX={mode}：连变量名都不出现", "LLM_API_KEY" not in out, out[:200])

    # ============================================================ 4 ABI 降级
    print("\n[4] 权限位按 ABI 降级")
    r1, r2, r3, r4, r5 = (sbx._fs_rights(n) for n in (1, 2, 3, 4, 5))
    check("ABI1 = 13 个基础位，不含 REFER/TRUNCATE/IOCTL_DEV",
          r1 == sbx._ALL_FS and not (r1 & (sbx.REFER | sbx.TRUNCATE | sbx.IOCTL_DEV)), hex(r1))
    check("ABI2 加上 REFER（线上 6.1 → ABI 2）", r2 == r1 | sbx.REFER, hex(r2))
    check("ABI3 加上 TRUNCATE", r3 == r2 | sbx.TRUNCATE, hex(r3))
    check("ABI4 不加 fs 位（它加了 network）", r4 == r3, f"{hex(r3)} vs {hex(r4)}")
    check("ABI5 加上 IOCTL_DEV", r5 == r3 | sbx.IOCTL_DEV, hex(r5))

    # ============================================================ 5 静态
    print("\n[5] 静态：不引依赖、写法没退化")
    tree = ast.parse(src)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module.split(".")[0])
    check("只 import 标准库",
          imports <= {"ctypes", "os", "sys", "__future__"}, str(sorted(imports)))
    check("没有相对 import（它要能被当脚本直接跑）",
          not any(isinstance(n, ast.ImportFrom) and n.level > 0 for n in ast.walk(tree)))
    check("add_rule 传了 flags（少传一个参数 = EINVAL，看起来像规则不合法）",
          bool(re.search(r"byref\(pfa\),\s*ctypes\.c_uint32\(0\)", src)))
    check("先 no_new_privs 再 restrict_self（顺序反了 EPERM）",
          src.index("libc.prctl(PR_SET_NO_NEW_PRIVS")
          < src.index("libc.syscall(ctypes.c_long(SYS_RESTRICT_SELF)"))

    # ============================================================ 6 CLI
    print("\n[6] CLI 参数")
    r = subprocess.run([sys.executable, str(SANDBOX_PATH), "--workspace", "/tmp"],
                       capture_output=True, text=True, timeout=30)
    check("没有 '--' → 退出码 64", r.returncode == 64, f"rc={r.returncode}")
    r = subprocess.run([sys.executable, str(SANDBOX_PATH), "--workspace", "/tmp", "--"],
                       capture_output=True, text=True, timeout=30)
    check("'--' 后面没命令 → 退出码 64", r.returncode == 64, f"rc={r.returncode}")
    r = subprocess.run(
        [sys.executable, str(SANDBOX_PATH), "--workspace", "/tmp", "--", "/bin/echo", "hi"],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, SHELL_SANDBOX="off"))
    check("正常路径能穿透到目标命令",
          r.returncode == 0 and "hi" in (r.stdout or ""),
          f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

    # ============================================================ 7 真挂上去
    print("\n[7] 端到端：fork → 挂规则 → 试各种读写")
    abi = sbx.available()
    if abi == 0:
        skip("Landlock 端到端", "本机内核不支持（< 5.13，或被 LSM 关掉了）")
    else:
        print(f"  （Landlock ABI = {abi}）")
        wsdir = tempfile.mkdtemp(prefix="sbx-ws-")
        inside = os.path.join(wsdir, "note.md")
        with open(inside, "w") as f:
            f.write("hello")
        tmpfile = os.path.join(tempfile.gettempdir(), "sbx-tmp-probe.txt")
        with open(tmpfile, "w") as f:
            f.write("tmp")

        def probe(path, mode="read"):
            return _run_in_sandboxed_child(sbx, wsdir, path, mode)

        got = probe(inside)
        check("工作区里读文件 → 允许", got == "ok", got)
        got = probe(os.path.join(wsdir, "new.txt"), "write")
        check("工作区里写文件 → 允许", got == "ok", got)
        got = probe(tmpfile)
        check("/tmp 里读文件 → 允许", got == "ok", got)
        got = probe("/proc/1/environ")
        check("/proc/1/environ（凭证）→ 拒绝", got.startswith("denied"), got)
        got = probe("/", "list")
        check("列根目录 / → 拒绝", got.startswith("denied"), got)
        got = probe("/etc/sbx-should-not-exist", "write")
        check("往 /etc 写 → 拒绝", got.startswith("denied"), got)
        got = probe("/etc/passwd")
        check("读 /etc/passwd → 允许（系统只读目录要开着）", got == "ok", got)

        # strict_paths：白名单目录不存在时该报错，而不是安静地少挂一条规则
        got = _run_in_sandboxed_child(sbx, "/definitely-not-a-workspace-xyz", inside,
                                      "read", strict=True)
        check("strict 语义：工作区不存在时 apply 会抛（不是静默跳过）",
              got.startswith("apply-failed"), got)

    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "; ".join(FAILS))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
