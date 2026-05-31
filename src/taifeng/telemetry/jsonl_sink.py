"""JsonlSink —— 把 EventMsg 序列化为 JSONL，便于运维管道消费。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from taifeng.loop.engine import AgentEngine
from taifeng.loop.event import EventMsg


class JsonlSink:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def handle(self, ev: EventMsg) -> None:
        line = json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    async def attach(self, engine: AgentEngine) -> None:
        async for ev in engine.subscribe_all():
            await self.handle(ev)


def attach_jsonl_sink(engine: AgentEngine, path: str | Path) -> asyncio.Task[None]:
    sink = JsonlSink(path)
    return asyncio.create_task(sink.attach(engine))
