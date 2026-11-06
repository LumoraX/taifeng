"""终端树的有界显示、订阅缺口与慢输出隔离测试。"""
from __future__ import annotations

import asyncio
import io
import threading
from typing import Any

import pytest

from taifeng.loop.engine_types import DeliveredEvent
from taifeng.telemetry.execution_projection import ExecutionProjection
from taifeng.telemetry.terminal_tree import TerminalTreeSink

from .test_execution_projection import event


def test_empty_rows_controls_unicode_and_unknown_parent() -> None:
    """无输入可读，字段不允许注入控制序列，缺父级不伪造关系。"""
    sink = TerminalTreeSink(out=io.StringIO(), max_rows=3)
    assert sink.render()
    with pytest.raises(ValueError):
        TerminalTreeSink(max_rows=0)
    sink.projection.handle(event('turn_started', parent='missing'))
    ev = event('turn_started', 1, node='中文\x1b[2J\x00\x85' + 'x' * 10000)
    sink.projection.handle(ev)
    text = sink.render()
    assert '中文' in text
    assert '\x1b' not in text and '\x85' not in text and '\x00' not in text
    assert len(text.splitlines()) <= 3
    assert len(text) < 1200
    assert '父级未知' in text


def test_deep_and_cyclic_trees_are_bounded() -> None:
    """渲染不递归，环与超深树都在行数限额内结束。"""
    p = ExecutionProjection(max_nodes=1500)
    for i in range(1100):
        p.handle(event('turn_started', i, node=str(i), parent=str(i - 1) if i else '1099'))
    sink = TerminalTreeSink(projection=p, max_rows=8)
    assert len(sink.render().splitlines()) <= 8
    assert '截断' in sink.render()


async def test_non_tty_appends_without_ansi_and_tty_redraws() -> None:
    """只有明确交互终端才重绘。"""
    out = io.StringIO()
    sink = TerminalTreeSink(out=out)
    await sink.handle(event('turn_started'))
    await sink.handle(event('execution_updated', 1, phase='model', context_tokens=100,
                            context_window=1000, context_measured=False))
    assert '\x1b' not in out.getvalue()
    assert '估算' in out.getvalue()
    interactive = io.StringIO()
    tty = TerminalTreeSink(out=interactive, interactive=True)
    await tty.handle(event('turn_started'))
    await tty.handle(event('turn_completed', 1))
    assert '\x1b[' in interactive.getvalue()


async def test_envelopes_detect_initial_delivery_loss() -> None:
    """使用完整信封流，首条 delivery_seq 缺口同样可见。"""
    class Engine:
        """只提供完整信封订阅，避免误用过滤事件。"""

        async def subscribe_all_envelopes(self) -> Any:
            """首条序号故意跳过。"""
            yield DeliveredEvent(event('turn_started'), 2)
            yield DeliveredEvent(event('turn_completed', 1), 3)

    sink = TerminalTreeSink(out=io.StringIO())
    await sink.attach(Engine())  # type: ignore[arg-type]
    assert sink.projection.incomplete
    assert '不完整' in sink.render()


async def test_slow_output_does_not_block_and_cancellation_abandons_write() -> None:
    """底层写入在线程中，取消不等待它完成且不安排后续 flush。"""
    entered, release = threading.Event(), threading.Event()

    class Slow(io.StringIO):
        """可控阻塞的输出设备。"""

        def write(self, text: str) -> int:
            """等待测试释放，模拟慢终端。"""
            entered.set()
            release.wait(3)
            return super().write(text)

    sink = TerminalTreeSink(out=Slow())
    task = asyncio.create_task(sink.handle(event('turn_started')))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        # write 未结束，但事件循环仍可推进；取消须立即收敛。
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.3)
    finally:
        release.set()


async def test_concurrent_handles_serialize_writes() -> None:
    """多个调用者共用 sink 时输出也不会重叠。"""
    class Output(io.StringIO):
        """记录底层并发写入数。"""

        active = 0
        maximum = 0

        def write(self, text: str) -> int:
            """单次写入期间检查串行承诺。"""
            import time
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            time.sleep(0.01)
            result = super().write(text)
            self.active -= 1
            return result

    out = Output()
    sink = TerminalTreeSink(out=out)
    await asyncio.gather(*(sink.handle(event('turn_started', i, node=str(i))) for i in range(5)))
    assert out.maximum == 1


async def test_attach_cancellation_does_not_request_another_envelope() -> None:
    """订阅在写入时被取消后，不再从 engine 拉取下一条。"""
    entered, release = threading.Event(), threading.Event()
    read = []

    class Engine:
        """记录消费边界的模拟完整订阅。"""

        async def subscribe_all_envelopes(self) -> Any:
            """每次取下一条都会登记。"""
            for i in range(2):
                read.append(i)
                yield DeliveredEvent(event('turn_started', i), i)

    class Slow(io.StringIO):
        """阻塞第一次写入直到测试完成。"""

        def write(self, text: str) -> int:
            """让出控制权给后台线程取消验证。"""
            entered.set()
            release.wait(3)
            return super().write(text)

    sink = TerminalTreeSink(out=Slow())
    task = asyncio.create_task(sink.attach(Engine()))  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.3)
        assert read == [0]
    finally:
        release.set()
