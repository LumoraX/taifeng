"""FanoutSink —— 把一个事件依次投递给多个 TelemetrySink（telemetry-fanout-sink）。

``EnginePool.create(sink=...)`` 只接受一个 sink；插件要观察 pool 级事件，需要一个可增删成员的
sink。成员之间故障隔离：观察者失败不应让上游（索引钩子 runner、持久化层）失败。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from taifeng.loop.event import EventMsg
    from taifeng.telemetry.sink import TelemetrySink

logger = logging.getLogger(__name__)


class _MemberHandle:
    """移除一个 fan-out 成员；``aclose`` 幂等。"""

    __slots__ = ("_closed", "_fanout", "_sink")

    def __init__(self, fanout: FanoutSink, sink: TelemetrySink) -> None:
        self._fanout = fanout
        self._sink = sink
        self._closed = False

    async def aclose(self) -> None:
        """从 fan-out 中移除。"""
        if self._closed:
            return
        self._closed = True
        self._fanout._remove(self._sink)  # noqa: SLF001


class FanoutSink:
    """按加入顺序把事件投递给全部成员的 ``TelemetrySink``。

    用法：
        fanout = FanoutSink(host_sink)
        handle = fanout.add(other_sink)
        ...
        await handle.aclose()
    """

    def __init__(self, *initial: TelemetrySink) -> None:
        """构造 fan-out；``initial`` 是不可经 handle 移除的初始成员（如宿主 sink）。"""
        self._members: list[TelemetrySink] = []
        for sink in initial:
            self._append(sink)

    @property
    def members(self) -> tuple[TelemetrySink, ...]:
        """当前成员（按投递顺序）。"""
        return tuple(self._members)

    def add(self, sink: TelemetrySink) -> _MemberHandle:
        """加入成员，返回可撤销 handle。

        Raises:
            ValueError: 同一对象已是成员。
        """
        self._append(sink)
        return _MemberHandle(self, sink)

    async def handle(self, ev: EventMsg) -> None:
        """依次投递；单个成员抛错只记日志，取消照常传播。"""
        for sink in tuple(self._members):  # 投递中增删不影响本次
            try:
                await sink.handle(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 观察者故障隔离
                logger.warning(
                    "telemetry sink %s failed on %s: %s",
                    type(sink).__name__, ev.msg.kind, type(exc).__name__,
                )

    def _append(self, sink: TelemetrySink) -> None:
        if any(member is sink for member in self._members):
            raise ValueError(f"sink {type(sink).__name__} is already a fan-out member")
        self._members.append(sink)

    def _remove(self, sink: TelemetrySink) -> None:
        self._members = [member for member in self._members if member is not sink]


__all__ = ["FanoutSink"]
