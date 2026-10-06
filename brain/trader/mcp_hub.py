"""MCP 聚合层。

把多个 MCP server 的工具汇成一份 OpenAI function-calling 工具表，
名字统一加 `<server>__` 前缀避免冲突。

所有 MCP 连接（anyio 上下文）都活在同一个专属 asyncio 任务里，
调用方只通过队列投递请求 —— 这样退出上下文时不会跨任务报错，
也便于单点重连，不用每次调用都重新握手。
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("trader.mcp")


@dataclass
class ServerCfg:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


def _join_blocks(chunks: list[str]) -> str:
    """把 MCP 返回的多个 text block 合成一段。

    ★ 这里有个坑，是实测出来的：

      **FastMCP 会把它返回的 list 拆成「一个元素一个 text block」。**
      返回 `[{"symbol":"BTC"},{"symbol":"ETH"}]`，拿到的不是一个 JSON 数组，
      而是两个 block，各自是一段 pretty-print 的对象。

      而 hub 原来是把它们用 `\\n` 拼起来的，于是得到：

          {
            "symbol": "BTCUSDT",
            "price": 84000
          }
          {
            "symbol": "ETHUSDT",
            ...

      这**不是合法 JSON**，下游每一处 `json.loads` 都会失败。
      更阴的是失败方式：`_safe_call` 遇到解析失败会安静降级成 `[]`，
      于是"返回空列表"和"解析失败"在调用方看起来完全一样，
      整类工具的返回值被静默吞掉，一点报错都看不到。

      所以这里做一次重装配：每个 block 单独能解析成 JSON 的话，
      就把它们装回一个数组。不能解析（比如普通文本）就原样拼接。
    """
    if not chunks:
        return "(空结果)"
    if len(chunks) == 1:
        return chunks[0]
    parsed: list[Any] = []
    for c in chunks:
        try:
            parsed.append(json.loads(c))
        except (json.JSONDecodeError, TypeError, ValueError):
            return "\n".join(chunks)        # 不是 JSON 块，按普通文本处理
    return json.dumps(parsed, ensure_ascii=False)


class MCPHub:
    def __init__(self, servers: list[ServerCfg]) -> None:
        self.servers = {s.name: s for s in servers}
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._tools: dict[str, dict[str, Any]] = {}
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._healthy: dict[str, bool] = {}

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._supervisor(), name="mcp-hub")
        await asyncio.wait_for(self._ready.wait(), timeout=120)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _supervisor(self) -> None:
        try:
            for name in self.servers:
                await self._connect(name)
            self._ready.set()
            assert self._queue is not None
            while True:
                req = await self._queue.get()
                if req is None:
                    break
                name, args, fut = req
                if fut.cancelled():
                    continue
                try:
                    result = await self._invoke(name, args)
                    if not fut.done():
                        fut.set_result(result)
                except Exception as exc:  # 单个调用失败不能拖垮整个 hub
                    if not fut.done():
                        fut.set_exception(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("mcp hub 异常退出: %s", exc)
            self._ready.set()
        finally:
            for name in list(self._stacks):
                await self._disconnect(name)

    async def _connect(self, name: str) -> None:
        cfg = self.servers[name]
        stack = AsyncExitStack()
        try:
            res = await stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=cfg.headers or None)
            )
            read, write = res[0], res[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = (await session.list_tools()).tools
        except Exception as exc:
            try:
                await stack.aclose()
            except Exception:
                pass
            self._healthy[name] = False
            log.error("连接 MCP[%s] %s 失败: %s", name, cfg.url, exc)
            return

        self._stacks[name] = stack
        self._sessions[name] = session
        self._healthy[name] = True
        for t in listed:
            namespaced = f"{name}__{t.name}"
            self._tools[namespaced] = {
                "server": name,
                "tool": t.name,
                "schema": {
                    "type": "function",
                    "function": {
                        "name": namespaced,
                        "description": (t.description or t.name)[:1024],
                        "parameters": t.inputSchema or {"type": "object", "properties": {}},
                    },
                },
            }
        log.info("MCP[%s] 已连接，注册 %d 个工具", name, len(listed))

    async def _disconnect(self, name: str) -> None:
        stack = self._stacks.pop(name, None)
        self._sessions.pop(name, None)
        if stack:
            try:
                await stack.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------ 调用
    async def _invoke(self, namespaced: str, args: dict) -> str:
        meta = self._tools.get(namespaced)
        if not meta:
            raise KeyError(f"未知工具 {namespaced}")
        server, tool = meta["server"], meta["tool"]

        if not self._healthy.get(server):
            await self._disconnect(server)
            await self._connect(server)

        session = self._sessions.get(server)
        if session is None:
            raise RuntimeError(f"MCP server [{server}] 当前不可用")
        try:
            result = await session.call_tool(tool, arguments=args or {})
        except Exception:
            # 断线重连一次再试
            await self._disconnect(server)
            await self._connect(server)
            session = self._sessions.get(server)
            if session is None:
                raise
            result = await session.call_tool(tool, arguments=args or {})

        chunks: list[str] = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
            else:
                chunks.append(f"<{getattr(block, 'type', 'content')}>")
        out = _join_blocks(chunks)
        if getattr(result, "isError", False):
            return f"[工具报错] {out}"
        return out

    async def call(self, namespaced: str, args: dict | None = None) -> str:
        if not self._queue:
            raise RuntimeError("MCPHub 尚未启动")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((namespaced, args or {}, fut))
        return await fut

    # ------------------------------------------------------------------ 工具表
    def openai_tools(self) -> list[dict]:
        return [t["schema"] for t in self._tools.values()]

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def server_status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for t in self._tools.values():
            counts[t["server"]] = counts.get(t["server"], 0) + 1
        return {
            name: {"connected": self._healthy.get(name, False), "tools": counts.get(name, 0)}
            for name in self.servers
        }
