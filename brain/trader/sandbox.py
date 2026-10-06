"""Landlock 文件系统沙箱 —— 给 run_shell 的子进程上一副真的手铐。

============================================================================
为什么需要它（README/HANDOVER 里的 A1）
============================================================================

`run_shell` 原来只设了 cwd，没有 chroot。实测（2026-09-27，线上容器，uid 10001）：

    $ head -3 /data/logs/journal.md        → 读得到
    $ echo x >> /data/logs/zzz             → 写得进
    $ cat /proc/1/environ | grep KEY       → 读得到，而且是明文

第二条是这次探针**新发现**的，比原来记的那条更严重：

    清空环境变量（env={...}）只清**子进程自己要拿到的**那份。
    brain 进程自己的初始环境块还躺在内核里，`/proc/<pid>/environ` 就是它。
    子进程和 brain 同 uid → 一句 `cat /proc/1/environ` 就能把 LLM_API_KEY、
    SUBAGENT_API_KEY_* 全部抄走。

所以「密钥只在 brain 进程环境里，Agent 拿不到」这句话，在 shell 这条路上是假的 ——
和「手账放 logs 所以它读不到」是同一个错误：**把约定当成了物理保证。**

后果两条，一句话概括：**brain 能读能写的地方，run_shell 也能读能写。**

  1. 叙事层的「单向性」在这条路上是漏的（`cat journal.md` 把叙事读回操盘上下文）。
  2. 凭证从 /proc 漏出去。

============================================================================
为什么是 Landlock
============================================================================

chroot 需要 CAP_SYS_CHROOT，bubblewrap 需要 unprivileged userns ——
容器 `cap_drop: ALL`，而且 userns 被 Docker 的 seccomp 拦着（实测
`unshare(CLONE_NEWUSER)` → EACCES）。**Landlock 是这里唯一可用的那把刀**：
非特权 LSM，只要 `no_new_privs` 就能用，内核 5.13+。
实测线上：内核 6.1.0 → Landlock ABI = 2。

============================================================================
设计
============================================================================

**默认拒绝。** 先声明「我要管住哪些权限」（handled_access_fs），然后手写一张
极短的白名单，只给这些地方开权限：

    工作区           全部权限（这是它的纸笔）
    /tmp             全部权限（临时文件）
    /usr /bin /sbin /lib* /etc /opt /app  只读 + 可执行（解释器和库）
    /dev             读写（/dev/null、/dev/urandom）
    /sys             只读

没被规则覆盖的路径一律 EACCES。刻意不在名单里的：

    /data/logs        ← 叙事层手账、事件日志。这就是本节要堵的那个洞
    /proc             ← environ 凭证泄漏。不给它开，python 也照样跑得动
    /data /root /home /var /run

Landlock 只有「允许」没有「拒绝」，所以正确姿势不是去禁 /proc，
而是**一开始就别给它**。

它自己不做 I/O，只负责在 exec 之前把规则挂上去 —— 挂完之后 execve 到 /bin/sh，
限制跟着进程走，连孙子进程一起管住。

============================================================================
怎么用
============================================================================

作为库：

    from .sandbox import apply_landlock, available
    apply_landlock(cfg.workspace_dir)      # 在**子进程**里调用，先挂规则再 exec

作为命令（tools_local 走的就是这条）：

    python3 /app/trader/sandbox.py --workspace /data/workspace -- sh -c 'ls'

    SHELL_SANDBOX=off     紧急关闭（只对工具输出留一行警告，不静默）
    SHELL_SANDBOX=require 挂不上就拒绝执行（最严；默认 auto 会降级 + 警告）
"""
from __future__ import annotations

import ctypes
import os
import sys

# --------------------------------------------------------------------------- 常量
# asm-generic 的号，x86_64 与 aarch64 相同。
SYS_CREATE_RULESET = 444
SYS_ADD_RULE = 445
SYS_RESTRICT_SELF = 446

CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH = 1

PR_SET_NO_NEW_PRIVS = 38

# LANDLOCK_ACCESS_FS_*
EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13       # ABI 2 (Linux 5.19)
TRUNCATE = 1 << 14    # ABI 3 (Linux 6.2)
IOCTL_DEV = 1 << 15   # ABI 5 (Linux 6.10)

_ALL_FS = (EXECUTE | WRITE_FILE | READ_FILE | READ_DIR | REMOVE_DIR | REMOVE_FILE
           | MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM)

#: 只读 + 可执行。解释器、共享库、locale 都在这里。
READONLY_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
                  "/etc", "/opt", "/app", "/sys")

#: 需要读写但不该有创建权限的（设备节点、shm）。
READWRITE_ROOTS = ("/dev",)

#: 全权目录 —— 只有工作区和 /tmp。
def writable_roots(workspace_dir: str) -> tuple[str, ...]:
    return (workspace_dir, "/tmp")


libc = ctypes.CDLL(None, use_errno=True)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    # 内核里是 __attribute__((packed))，不打这个标志结构体大小会算错
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _fs_rights(abi: int) -> int:
    """按 ABI 版本挑出内核认得的那些位。

    传了它不认识的位，create_ruleset 直接 EINVAL —— 所以这里必须按版本降级，
    不能在老内核上一把梭全传。
    """
    rights = _ALL_FS
    if abi >= 2:
        rights |= REFER
    if abi >= 3:
        rights |= TRUNCATE
    if abi >= 5:
        rights |= IOCTL_DEV
    return rights


def available() -> int:
    """返回 Landlock ABI 版本号；0 = 内核不支持。"""
    try:
        r = libc.syscall(ctypes.c_long(SYS_CREATE_RULESET),
                         ctypes.c_void_p(None),
                         ctypes.c_size_t(0),
                         ctypes.c_uint32(CREATE_RULESET_VERSION))
    except Exception:
        return 0
    return int(r) if r >= 0 else 0


def _err() -> str:
    return os.strerror(ctypes.get_errno())


def apply_landlock(workspace_dir: str, *, strict_paths: bool = False) -> int:
    """给**当前进程**挂上 Landlock 规则。返回生效的 ABI 版本。

    必须在 execve 之前调用。调用之后这个进程（及其所有后代）就只能碰白名单里的路径。

    strict_paths=False 时，白名单里不存在的目录直接跳过 —— 不同镜像里
    /lib64 这类目录时有时无，不该因此拒绝启动。规则本身挂不上则抛异常。
    """
    abi = available()
    if abi == 0:
        raise RuntimeError("Landlock 不可用（内核 < 5.13 或被 LSM 关掉了）")

    rights = _fs_rights(abi)

    attr = _RulesetAttr(handled_access_fs=rights)
    ruleset_fd = libc.syscall(ctypes.c_long(SYS_CREATE_RULESET),
                              ctypes.byref(attr),
                              ctypes.c_size_t(ctypes.sizeof(attr)),
                              ctypes.c_uint32(0))
    if ruleset_fd < 0:
        raise RuntimeError(f"landlock_create_ruleset 失败：{_err()}")

    full = rights
    ro = EXECUTE | READ_FILE | READ_DIR
    rw = READ_FILE | READ_DIR | WRITE_FILE | TRUNCATE

    rules: list[tuple[str, int]] = []
    for p in writable_roots(workspace_dir):
        rules.append((p, full))
    for p in READWRITE_ROOTS:
        rules.append((p, rw))
    for p in READONLY_ROOTS:
        rules.append((p, ro))

    try:
        for path, access in rules:
            if not os.path.isdir(path):
                if strict_paths:
                    raise RuntimeError(f"白名单目录不存在：{path}")
                continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                pfa = _PathBeneathAttr(allowed_access=access & rights, parent_fd=fd)
                # 号 + 三个参数。flags 这个 0 不能省 —— 少传一个参数时
                # syscall() 会把寄存器里的垃圾当 flags，报出来是 EINVAL，
                # 看起来像"规则不合法"，其实是调用姿势错了。（2026-09-27 踩过）
                r = libc.syscall(ctypes.c_long(SYS_ADD_RULE),
                                 ctypes.c_int(ruleset_fd),
                                 ctypes.c_int(RULE_PATH_BENEATH),
                                 ctypes.byref(pfa),
                                 ctypes.c_uint32(0))
                if r < 0:
                    raise RuntimeError(f"landlock_add_rule({path}) 失败：{_err()}")
            finally:
                os.close(fd)

        # 没有它 restrict_self 会 EPERM。也顺手把 setuid 提权这条路堵死。
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise RuntimeError(f"prctl(PR_SET_NO_NEW_PRIVS) 失败：{_err()}")

        r = libc.syscall(ctypes.c_long(SYS_RESTRICT_SELF),
                         ctypes.c_int(ruleset_fd),
                         ctypes.c_uint32(0))
        if r < 0:
            raise RuntimeError(f"landlock_restrict_self 失败：{_err()}")
    finally:
        os.close(ruleset_fd)

    return abi


def child_env(workspace_dir: str) -> dict[str, str]:
    """子进程唯一该看到的环境。

    这是**唯一的事实来源** —— tools_local 用它去起 wrapper，wrapper 再用它
    去 execve 真正的命令。两道都过一遍是有意的：凭证不泄漏这件事不该依赖
    「调用方记得清空环境」这种约定。实测过一遍：直接 `python3 sandbox.py ...`
    而不带 env 时，继承下来的环境里原样躺着 LLM_API_KEY。
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": workspace_dir,
        "PWD": workspace_dir,
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def set_non_dumpable() -> bool:
    """让 /proc/<本进程>/environ 对同 uid 的其它进程关上。

    Landlock 管不到 /proc（那上面挂了它的规则就废了），所以凭证这条要靠
    另一个机制：进程 `dumpable=0` 之后，/proc/<pid>/{environ,maps,mem,...}
    的 ptrace 检查会要求 CAP_SYS_PTRACE，同 uid 的兄弟进程也就读不到了。

    父进程执行一次就够了，而且不影响自己读自己。
    """
    PR_SET_DUMPABLE = 4
    try:
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    """python3 sandbox.py --workspace DIR -- <cmd> [args...]

    把规则挂上，然后 execve 到目标命令。挂不上时按 SHELL_SANDBOX 决定怎么办：
      auto（默认）  → 降级执行，但在 stderr 留一行给调用方抓
      off           → 完全不挂，执行
      require       → 拒绝执行，退出码 78
    """
    mode = (os.environ.get("SHELL_SANDBOX") or "auto").strip().lower()

    if "--" not in argv:
        print("用法：sandbox.py --workspace DIR -- <cmd> [args...]", file=sys.stderr)
        return 64
    sep = argv.index("--")
    head, cmd = argv[:sep], argv[sep + 1:]
    if not cmd:
        print("sandbox.py: '--' 后面没有命令", file=sys.stderr)
        return 64

    workspace = "/data/workspace"
    for i, a in enumerate(head):
        if a == "--workspace" and i + 1 < len(head):
            workspace = head[i + 1]

    if mode != "off":
        try:
            apply_landlock(workspace)
        except Exception as exc:
            if mode == "require":
                print(f"[沙箱] 挂载失败，按 SHELL_SANDBOX=require 拒绝执行：{exc}", file=sys.stderr)
                return 78
            # auto：降级但不静默 —— 这一行会被 run_shell 拼进工具返回值里
            print(f"[沙箱] ⚠ Landlock 未生效（{exc}），本次命令在无文件系统约束下执行",
                  file=sys.stderr)
    elif mode == "off":
        print("[沙箱] ⚠ SHELL_SANDBOX=off，本次命令未受约束", file=sys.stderr)

    try:
        # 连环境一起换掉。wrapper 自己可能是从一份"干净"的父进程环境里起来的，
        # 也可能不是 —— 这里不再假设，直接给死。
        os.execvpe(cmd[0], cmd, child_env(workspace))
    except Exception as exc:  # 目标不存在等
        print(f"[沙箱] 启动失败：{exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
