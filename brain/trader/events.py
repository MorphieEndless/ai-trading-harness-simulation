"""事件总线：内存环形缓冲（给 SSE 实时流）+ append-only JSONL（给历史复盘）。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, log_path: str, buffer_size: int = 3000) -> None:
        self.log_path = log_path
        self._buf: deque[dict] = deque(maxlen=buffer_size)
        self._subs: set[asyncio.Queue] = set()
        self._seq = 0
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        self._load_tail()

    def _load_tail(self, n: int = 400) -> None:
        """重启后仍能在面板上看到最近的历史。"""
        if not os.path.exists(self.log_path):
            return
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in deque(f, maxlen=n):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._buf.append(ev)
                    self._seq = max(self._seq, int(ev.get("id") or 0))
        except OSError:
            pass

    def emit(self, kind: str, **data: Any) -> dict:
        with self._lock:
            self._seq += 1
            ev = {
                "id": self._seq,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "data": data,
            }
        self._buf.append(ev)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass
        return ev

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def history(self, limit: int = 300) -> list[dict]:
        return list(self._buf)[-max(1, limit):]

    @property
    def seq(self) -> int:
        return self._seq
