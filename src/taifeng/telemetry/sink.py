"""TelemetrySink 协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from taifeng.loop.event import EventMsg


@runtime_checkable
class TelemetrySink(Protocol):
    """异步处理 EventMsg 的协议。"""

    async def handle(self, ev: EventMsg) -> None:
        ...
