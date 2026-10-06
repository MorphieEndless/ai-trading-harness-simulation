"""大脑本地工具：工作区文件读写 + 受限 shell。

沙箱事实（不靠提示词约束，靠容器本身 + Landlock）：
  * 根文件系统只读，唯一的可写挂着落点是 /data/workspace（以及 /tmp 的 tmpfs）。
  * 工作区之外没有挂载任何东西 —— 纸面账本 /data/db 根本没进这个容器。
  * run_shell 的子进程带一套 **Landlock 文件系统规则**（见 sandbox.py）：
    只有工作区、/tmp、系统只读目录在名单里，其余一律 EACCES ——
    包括 /data/logs（叙事层手账）和 /proc。

    这条是 2026-09-27 补的。在那之前只有 `cwd=`，实测过的两个洞：
      ① `cat /data/logs/journal.md` 读得到 → 叙事层的"单向性"在 shell 路上是漏的
      ② `cat /proc/1/environ` 读得到 → 清空的是**子进程**的环境变量，
         brain 自己那份初始环境块还躺在 /proc 里，同 uid 可读，明文密钥
    「清空环境变量」这个说法曾经写在文档里当保证，其实只挡住了 `env` 这一条路。
  * 子进程的环境变量白名单在 sandbox.child_env() 里，是唯一的事实来源。
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from . import sandbox
from .config import Config

#: 沙箱启动器。它挂好规则再 execve 到真正的命令 ——
#: 用独立进程而不是 preexec_fn，是为了不在 fork 之后跑 Python 代码
#: （多线程进程里那是明令不安全的东西）。
_SANDBOX_LAUNCHER = os.path.join(
    os.path.dirname(os.path.abspath(sandbox.__file__)), "sandbox.py")


class LocalTools:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        os.makedirs(cfg.workspace_dir, exist_ok=True)

    # ------------------------------------------------------------------ 路径越界防护
    def _safe(self, rel: str) -> str:
        base = os.path.realpath(self.cfg.workspace_dir)
        raw = str(rel or "").strip() or "."
        target = os.path.realpath(os.path.join(base, raw.lstrip("/")))
        if target != base and not target.startswith(base + os.sep):
            raise ValueError(f"路径越界：只能访问工作区 {base} 内的文件（收到 {rel!r}）")
        return target

    def _rel(self, path: str) -> str:
        base = os.path.realpath(self.cfg.workspace_dir)
        return os.path.relpath(path, base)

    # ------------------------------------------------------------------ 工具表
    def schemas(self) -> list[dict]:
        tools = [
            {
                "name": "fs_list",
                "description": "列出工作区里某个目录的内容。path 相对于工作区根目录，默认 '.'。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对工作区的目录路径，如 'notes'"},
                    },
                },
            },
            {
                "name": "fs_read",
                "description": (
                    "读取工作区里的一个文本文件，用于回顾你自己之前写的笔记与数据集。\n"
                    "对会不断增长的日志类文件（例如 market_log.md），"
                    "请用 tail_lines 只读末尾一小段 —— 整篇读回来会拖慢并推高成本，"
                    "而旧记录你通常并不需要。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对工作区的文件路径"},
                        "max_chars": {"type": "integer", "description": "最多返回多少字符（默认 8000）"},
                        "tail_lines": {
                            "type": "integer",
                            "description": "只读最后 N 行（0 = 读全文）。日志类文件建议给 60~120。",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "fs_write",
                "description": (
                    "把内容写入工作区文件（覆盖）。这是你的长期记忆，跨心跳持久化。"
                    "建议把观察、判断依据、待验证的假设写进 notes/ 目录。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对工作区的文件路径，如 'notes/thesis.md'"},
                        "content": {"type": "string", "description": "要写入的完整内容"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "fs_append",
                "description": "追加内容到工作区文件末尾（不存在则创建）。适合逐条记录日志。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        ]

        if self.cfg.enable_shell:
            tools.append({
                "name": "run_shell",
                "description": (
                    "在隔离容器内执行一条 shell 命令，工作目录为工作区根。"
                    "适合用 python3 做自建计算、处理你保存的 CSV 等。"
                    "注意：容器根文件系统只读，而子进程还挂着一层文件系统沙箱 ——"
                    "只有工作区和 /tmp 能读写，系统目录只读，其余路径（包括 /data/logs、"
                    "/proc、/etc/shadow）一律拒绝访问。别去试，试了只是白花一次调用。"
                    "命令超时会返回工具报错。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
                    "required": ["command"],
                },
            })

        return [
            {"type": "function", "function": {**t, "parameters": t["parameters"]}}
            for t in tools
        ]

    # ------------------------------------------------------------------ 派发
    async def call(self, name: str, args: dict[str, Any]) -> str:
        if name == "run_shell" and not self.cfg.enable_shell:
            return "[已禁用] run_shell 在当前配置下被关闭。"
        fn = getattr(self, f"_tool_{name}", None)
        if fn is None:
            return f"[未知本地工具] {name}"
        try:
            return await fn(**args)
        except TypeError as exc:
            return f"[参数错误] {exc}"
        except Exception as exc:
            return f"[工具报错] {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ 实现
    async def _tool_fs_list(self, path: str = ".") -> str:
        target = self._safe(path)
        if not os.path.exists(target):
            return f"[不存在] {path}"
        if os.path.isfile(target):
            return f"{self._rel(target)}  (文件, {os.path.getsize(target)} 字节)"
        lines: list[str] = []
        for entry in sorted(os.listdir(target)):
            full = os.path.join(target, entry)
            if os.path.isdir(full):
                lines.append(f"{entry}/")
            else:
                lines.append(f"{entry}  ({os.path.getsize(full)} B)")
        return "\n".join(lines) if lines else "(空目录)"

    async def _tool_fs_read(self, path: str, max_chars: int = 8000,
                            tail_lines: int = 0) -> str:
        """读文件。tail_lines > 0 时只读最后 N 行 —— 对不断增长的日志非常重要，
        否则每轮心跳都把整篇旧记录读回来，上下文越滚越大、成本逐轮上涨。
        """
        target = self._safe(path)
        if not os.path.isfile(target):
            return f"[不存在] {path}"
        size = os.path.getsize(target)
        if tail_lines and tail_lines > 0:
            from collections import deque
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                lines = list(deque(f, maxlen=max(1, min(int(tail_lines), 2000))))
            data = "".join(lines)
            note = f"（已按 tail_lines={tail_lines} 只取末尾部分，文件共 {size} 字节）"
        else:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(max_chars + 1)
            note = ""
        if len(data) > max_chars:
            # 截断时保留末尾：日志类文件最新内容在最后面
            data = "…（前面已省略）\n" + data[-max_chars:]
            note = note or f"（文件共 {size} 字节，此处只显示末尾 {max_chars} 字符）"
        body = data or "(空文件)"
        return f"{body}\n\n{note}" if note else body

    async def _tool_fs_write(self, path: str, content: str) -> str:
        target = self._safe(path)
        os.makedirs(os.path.dirname(target) or self.cfg.workspace_dir, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已写入 {self._rel(target)}（{len(content)} 字符）"

    async def _tool_fs_append(self, path: str, content: str) -> str:
        target = self._safe(path)
        os.makedirs(os.path.dirname(target) or self.cfg.workspace_dir, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(content)
        return f"已追加 {len(content)} 字符到 {self._rel(target)}"

    async def _tool_run_shell(self, command: str) -> str:
        if not self.cfg.enable_shell:
            return "[已禁用] run_shell 在当前配置下被关闭。"
        workspace = self.cfg.workspace_dir
        env = sandbox.child_env(workspace)
        # 沙箱模式由人类在 .env 里定（auto / off / require），
        # 走白名单传进去 —— 不然它读不到自己的开关。
        env["SHELL_SANDBOX"] = self.cfg.shell_sandbox
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, _SANDBOX_LAUNCHER,
                "--workspace", workspace,
                "--", "/bin/sh", "-c", command,
                cwd=workspace,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # 关键：环境变量白名单。子进程因此读不到 LLM_API_KEY、
                # SUBAGENT_API_KEY_* 等一切凭证 —— 这也是子代理密钥
                # 只走环境变量、不写进 config 文件的原因。
                # 注意这一条**只挡住 `env`**：brain 自己的初始环境块还在
                # /proc/<pid>/environ 里，那条路由 Landlock（拒 /proc）
                # 和 main.py 的 set_non_dumpable() 一起关掉。
                env=env,
            )
        except Exception as exc:
            return f"[启动失败] {exc}"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.shell_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"[超时] 命令超过 {self.cfg.shell_timeout} 秒被终止：{command[:200]}"
        text = (out or b"").decode("utf-8", "replace")
        limit = 12000
        if len(text) > limit:
            text = text[:limit] + f"\n…（输出截断，共 {len(text)} 字符）"
        if proc.returncode == 78:
            # 沙箱启动器自己的退出码：规则挂不上（require 模式）或目标命令起不来。
            # 和"命令本身失败"区分开，免得把沙箱故障读成脚本报错。
            return f"[沙箱拒绝执行]\n{text}"
        return f"exit={proc.returncode}\n{text}" if text.strip() else f"exit={proc.returncode}（无输出）"
