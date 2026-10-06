"""进程入口：装配所有部件，起 WebUI，跑心跳调度。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time

import uvicorn

from .agent import Agent
from .config import cfg
from .events import EventBus
from .llm import LLM
from .mcp_hub import MCPHub, ServerCfg
from .sandbox import available as landlock_abi, set_non_dumpable
from .tools_local import LocalTools
from .wake import PriceMonitor, WakeController
from .web import create_app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
)
log = logging.getLogger("trader.main")


class Runtime:
    def __init__(self) -> None:
        self.cfg = cfg
        self.bus = EventBus(os.path.join(cfg.log_dir, "events.jsonl"))
        self.hub = MCPHub(self._servers())
        self.local = LocalTools(cfg)
        self.llm = LLM(cfg)
        self.wake_event = asyncio.Event()
        # 唤醒策略与睡眠配额：调度器唯一的时间判据来源。
        # 三类唤醒（定时兜底 / 人类阈值 / Agent 自唤醒）都在这里汇合，
        # 睡眠也在这里把关 —— 详见 wake.py 顶部。
        self.wake = WakeController(cfg, self.hub, self.bus, self.wake_event)
        self.monitor = PriceMonitor(self.wake)
        self.agent = Agent(cfg, self.hub, self.local, self.llm, self.bus, self.wake)
        self._stopped = False

    def _servers(self) -> list[ServerCfg]:
        servers = [
            ServerCfg("market", cfg.market_mcp_url),
            ServerCfg("paper", cfg.paper_mcp_url),
        ]
        if cfg.exa_mcp_url:
            servers.append(ServerCfg("exa", cfg.exa_mcp_url))
        return servers

    async def hub_json(self, tool: str, args: dict | None = None):
        raw = await self.hub.call(tool, args or {})
        # 返回空列表的工具（如无成交时的 get_trade_history）拿到的是一段空文本，
        # 统一归一成 [] 而不是 {"raw": "(空结果)"}，免得面板上显示成异常。
        if isinstance(raw, str) and raw.strip() == "(空结果)":
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"raw": raw}

    # ------------------------------------------------------------------ 调度
    async def _idle(self, seconds: float) -> None:
        """静默等待。期间若有人叫醒（人类按钮 / 价格触发），立刻返回重判。

        上限 600 秒是刻意的：睡眠中可能一等等上几小时，但我们不希望
        「人类点了唤醒按钮」和「价格触发」被压在一个长 sleep 里等着。
        """
        self.wake_event.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self.wake_event.wait(), timeout=max(2.0, min(seconds, 600.0)))

    async def scheduler(self) -> None:
        # 启动后先等 20 秒再跑第一轮：让 MCP 连接和 WebUI 稳定下来，
        # 也留出时间让你在面板上看到 "已就绪"。
        await asyncio.sleep(20)
        while not self._stopped:
            try:
                if self.agent.paused:
                    self.agent.set_next_beat(None)
                    self.wake.clear_pending()
                    await self._idle(5)
                    continue

                # 睡眠账本的权威在 mcp-paper（brain 里没有任何地方是 Agent 改不到的），
                # 所以每次判时间之前都重新拉一次。
                await self.wake.refresh_sleep()
                d = self.wake.decide()

                if d.force_sleep:
                    # 强制入睡：不跑 LLM，所以这一步是省钱的而不是花钱的
                    self.bus.emit("system", message=f"强制入睡：{d.reason}")
                    await self.wake.enter_sleep(d.reason)
                    self.agent.set_next_beat(None)
                    continue

                if not d.run:
                    self.agent.set_next_beat(None)
                    if self.wake.has_pending():
                        continue            # 有触发排队，立刻重判，别阻塞在等待里
                    await self._idle(self.wake.next_check_seconds())
                    continue

                # 不在活跃时段就别跑。（睡眠记账已经在上面的 decide 里处理完了。）
                hour = time.localtime().tm_hour
                if d.trigger != "sleep_max" and not cfg.active_now(hour):
                    self.wake.clear_pending()
                    self.agent.set_next_beat(time.time() + 600)
                    self.bus.emit("skipped",
                                  reason=f"不在活跃时段 {cfg.active_hours}（当前 {hour} 点）")
                    await self._idle(600)
                    continue

                self.wake_event.clear()
                self.wake.consume(d)
                self.agent.set_next_beat(None)
                await self.agent.run_beat(trigger=d.trigger, wake_note=d.reason)
                # 刷新参考价：价格阈值一律以「上一轮结束时」为基准，
                # 所以触发过一轮就不会连着响，不需要额外的去重逻辑。
                self.wake.note_beat_done(await self.monitor.snapshot())
                self.agent.set_next_beat(self.wake.fallback_deadline())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("调度循环异常")
                self.bus.emit("error", message=f"调度器异常：{exc}")
                await asyncio.sleep(30)

    def stop(self) -> None:
        self._stopped = True
        self.wake_event.set()


async def amain() -> None:
    runtime = Runtime()
    os.makedirs(runtime.cfg.log_dir, exist_ok=True)
    os.makedirs(runtime.cfg.workspace_dir, exist_ok=True)
    os.makedirs(os.path.join(runtime.cfg.workspace_dir, "notes"), exist_ok=True)

    runtime.bus.emit("system", message="系统启动中…", model=runtime.cfg.llm_model or "(未配置)")

    # --- 沙箱自检：只报告，不拦启动 ---
    # Landlock 给 run_shell 的子进程上文件系统规则；set_non_dumpable 把
    # /proc/<本进程>/environ 对同 uid 关上（不然子进程一句 cat 就把 key 抄走了）。
    # 这两条都是**物理机制**，所以启动时必须知道它们到底生效没有 ——
    # 静默降级过的安全措施比没有更危险。
    abi = landlock_abi()
    if not abi:
        log.warning("Landlock 不可用（内核 < 5.13 或被 LSM 关掉了）：run_shell 只能受限降级")
        runtime.bus.emit("error", message="Landlock 不可用：run_shell 的文件系统沙箱挂不上")
    if set_non_dumpable():
        log.info("已设置 non-dumpable：/proc/<pid>/environ 对同 uid 进程关闭")
    else:
        runtime.bus.emit("error", message="set_non_dumpable 失败：/proc 里仍可能读到进程环境")
    runtime.bus.emit("system", message="沙箱自检",
                     landlock_abi=abi or None,
                     shell_sandbox=runtime.cfg.shell_sandbox,
                     non_dumpable=True)

    try:
        await runtime.hub.start()
    except Exception as exc:
        log.exception("MCP 初始化失败")
        runtime.bus.emit("error", message=f"MCP 初始化失败：{exc}")

    status = runtime.hub.server_status()
    runtime.bus.emit("system", message="MCP 就绪情况", servers=status)
    log.info("MCP 状态: %s", status)

    app = create_app(runtime)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=runtime.cfg.web_port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=120,
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runtime.stop)

    tasks = [
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(runtime.scheduler(), name="scheduler"),
        # 价格监控：不花 token（内网 HTTP，不经过 LLM），
        # 所以它 30 秒跑一次也不心疼。
        asyncio.create_task(runtime.monitor.run(), name="price-monitor"),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        runtime.stop()
        await runtime.hub.stop()
        for t in tasks:
            t.cancel()
        runtime.bus.emit("system", message="系统已停止")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
