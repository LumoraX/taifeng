"""`_RwLock` 写者等待期被取消 —— 计数须回退，读者不得被幽灵写者永久阻塞。

背景：`acquire_write` 先 `_waiting_writers += 1` 再 `await cond.wait()`；等待中被
cancel 时若不回退计数，`acquire_read` 的 `while _waiting_writers > 0` 会让此后所有
读者永久排队。`ToolCallRuntime` 是 pool 级单例，等于全 pool 的 parallel_safe 工具挂死。
"""

from __future__ import annotations

import asyncio
import contextlib

from taifeng.tool.runtime import _RwLock


async def test_cancelled_waiting_writer_does_not_block_future_readers() -> None:
    """读者持锁 → 写者等待中被 cancel → 读者释放 → 新读者须立即拿到读锁。"""
    lock = _RwLock()
    await lock.acquire_read()  # R1 持读锁，逼写者进入等待

    async def _writer() -> None:
        await lock.acquire_write()
        await lock.release_write()

    writer_task = asyncio.create_task(_writer())
    await asyncio.sleep(0)  # 让写者跑到 cond.wait()
    assert lock._waiting_writers == 1

    writer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await writer_task

    await lock.release_read()  # R1 释放

    # 新读者 R2：若写者计数残留，这里会永久阻塞
    await asyncio.wait_for(lock.acquire_read(), timeout=1.0)
    await lock.release_read()
    assert lock._waiting_writers == 0


async def test_uncancelled_writer_still_blocks_readers_until_released() -> None:
    """正常路径不变：写者等待期间新读者排队，写者拿到并释放后读者才通过。"""
    lock = _RwLock()
    await lock.acquire_read()

    async def _writer() -> None:
        await lock.acquire_write()
        await asyncio.sleep(0.05)
        await lock.release_write()

    writer_task = asyncio.create_task(_writer())
    await asyncio.sleep(0)
    reader_started = asyncio.Event()

    async def _reader() -> None:
        reader_started.set()
        await lock.acquire_read()
        await lock.release_read()

    reader_task = asyncio.create_task(_reader())
    await reader_started.wait()
    await asyncio.sleep(0)
    assert not reader_task.done()  # 写者排队中，读者被挡

    await lock.release_read()  # R1 释放 → 写者进 → 写者出 → 读者进
    await asyncio.wait_for(asyncio.gather(writer_task, reader_task), timeout=1.0)
    assert lock._waiting_writers == 0
