"""FanoutSink：多 sink 分发与故障隔离（telemetry-fanout-sink）。"""

from __future__ import annotations

import asyncio

import pytest

from taifeng.loop.event import EngineLog, EventMsg
from taifeng.telemetry.fanout import FanoutSink
from taifeng.telemetry.sink import TelemetrySink


class _Collect:
    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    async def handle(self, ev: EventMsg) -> None:
        self._log.append(self._name)


class _Boom:
    async def handle(self, ev: EventMsg) -> None:
        raise RuntimeError("down")


def _ev() -> EventMsg:
    return EventMsg(submission_id="*", msg=EngineLog(data={"m": "x"}))


def test_is_a_telemetry_sink() -> None:
    assert isinstance(FanoutSink(), TelemetrySink)


async def test_delivers_in_join_order() -> None:
    log: list[str] = []
    fanout = FanoutSink(_Collect("host", log))
    fanout.add(_Collect("a", log))
    fanout.add(_Collect("b", log))
    await fanout.handle(_ev())
    assert log == ["host", "a", "b"]


async def test_member_failure_is_isolated() -> None:
    log: list[str] = []
    fanout = FanoutSink(_Boom())
    fanout.add(_Collect("a", log))
    await fanout.handle(_ev())  # 不抛
    assert log == ["a"]


async def test_cancellation_propagates() -> None:
    class _Cancel:
        async def handle(self, ev: EventMsg) -> None:
            raise asyncio.CancelledError

    fanout = FanoutSink(_Cancel())
    with pytest.raises(asyncio.CancelledError):
        await fanout.handle(_ev())


async def test_duplicate_member_rejected() -> None:
    sink = _Collect("a", [])
    fanout = FanoutSink(sink)
    with pytest.raises(ValueError):
        fanout.add(sink)


async def test_handle_removes_member_idempotently() -> None:
    log: list[str] = []
    fanout = FanoutSink()
    handle = fanout.add(_Collect("a", log))
    await handle.aclose()
    await handle.aclose()
    await fanout.handle(_ev())
    assert log == []
    assert fanout.members == ()


async def test_membership_change_during_delivery_affects_next_round_only() -> None:
    log: list[str] = []
    fanout = FanoutSink()
    late = _Collect("late", log)

    class _Adder:
        async def handle(self, ev: EventMsg) -> None:
            log.append("adder")
            if late not in fanout.members:
                fanout.add(late)

    fanout.add(_Adder())
    await fanout.handle(_ev())
    assert log == ["adder"]
    await fanout.handle(_ev())
    assert log == ["adder", "adder", "late"]


async def test_empty_fanout_is_noop() -> None:
    await FanoutSink().handle(_ev())
