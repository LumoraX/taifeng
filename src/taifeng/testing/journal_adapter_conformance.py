"""SessionJournal 存储与锁适配器的一致性检查（ADR 0114）。

``SyncFileAdapter`` / ``WriterLockAdapter`` 是「换存储、不换 core」的两个注入点。装进
``JsonlSessionJournalCore`` 后跑 ``journal_core_cases()`` 是完整的验收；这里的检查直接对着适配器的
每个方法，出问题时定位更直接。适配器是同步的、由 core 派发到线程池，检查照同样的方式调用。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.conversation.journal.errors import CommitNotStartedError
from taifeng.conversation.journal.writer_lock import WriterLockBusyError
from taifeng.testing.journal_conformance import (
    ConformanceCase,
    ConformanceFailure,
    _check,
    _expect,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from taifeng.conversation.journal.file_io import SyncFileAdapter
    from taifeng.conversation.journal.writer_lock import WriterLockAdapter


class JournalStorageHarness(Protocol):
    """``SyncFileAdapter`` / ``WriterLockAdapter`` 检查的接入点。一个 harness 对应一份独立的存储。"""

    def new_storage(self) -> SyncFileAdapter:
        """接在同一份存储上的一个新适配器实例。"""
        ...

    def new_lock(self) -> WriterLockAdapter:
        """接在同一套互斥设施上的一个新锁适配器实例。"""
        ...

    def path(self, name: str) -> Path:
        """一个 Journal（或锁）的键；core 用 ``<root>/<session_id>.journal.jsonl`` 这样的路径。"""
        ...


async def _in_thread[T](call: Callable[[], T]) -> T:
    """适配器是同步的、由 core 派发到线程池；检查照同样的方式调用。"""
    return await asyncio.to_thread(call)


async def _storage_create_then_read(harness: JournalStorageHarness) -> None:
    storage, path = harness.new_storage(), harness.path("ses_a.journal.jsonl")
    await _in_thread(lambda: storage.create_exclusive(path, b"line-1\n"))
    _check(await _in_thread(lambda: storage.read_bytes(path)) == b"line-1\n",
           "read_bytes returns exactly what create_exclusive wrote")
    other = harness.new_storage()
    _check(await _in_thread(lambda: other.read_bytes(path)) == b"line-1\n",
           "another adapter instance sees the created journal")


async def _storage_create_is_exclusive(harness: JournalStorageHarness) -> None:
    storage, path = harness.new_storage(), harness.path("ses_a.journal.jsonl")
    await _in_thread(lambda: storage.create_exclusive(path, b"first\n"))
    raised = await _expect(
        (FileExistsError, CommitNotStartedError),
        _in_thread(lambda: harness.new_storage().create_exclusive(path, b"second\n")),
        "create_exclusive over an existing journal",
    )
    if isinstance(raised, CommitNotStartedError):
        _check(isinstance(raised.error, FileExistsError),
               "CommitNotStartedError must wrap FileExistsError when the journal already exists")
    _check(await _in_thread(lambda: storage.read_bytes(path)) == b"first\n",
           "a refused create must leave the existing journal untouched")


async def _storage_append_is_ordered(harness: JournalStorageHarness) -> None:
    storage, path = harness.new_storage(), harness.path("ses_a.journal.jsonl")
    await _in_thread(lambda: storage.create_exclusive(path, b"a\n"))
    await _in_thread(lambda: storage.append_durable(path, b"b\n"))
    await _in_thread(lambda: harness.new_storage().append_durable(path, b"c\n"))
    _check(await _in_thread(lambda: storage.read_bytes(path)) == b"a\nb\nc\n",
           "appends accumulate in order and are visible across instances")


async def _storage_missing_journal(harness: JournalStorageHarness) -> None:
    storage = harness.new_storage()
    await _expect(
        FileNotFoundError,
        _in_thread(lambda: storage.read_bytes(harness.path("ses_missing.journal.jsonl"))),
        "read_bytes on a journal that was never created",
    )


async def _storage_journals_are_independent(harness: JournalStorageHarness) -> None:
    storage = harness.new_storage()
    one, two = harness.path("ses_one.journal.jsonl"), harness.path("ses_two.journal.jsonl")
    await _in_thread(lambda: storage.create_exclusive(one, b"1\n"))
    await _in_thread(lambda: storage.create_exclusive(two, b"2\n"))
    await _in_thread(lambda: storage.append_durable(one, b"1b\n"))
    _check(await _in_thread(lambda: storage.read_bytes(one)) == b"1\n1b\n", "journal one grew")
    _check(await _in_thread(lambda: storage.read_bytes(two)) == b"2\n", "journal two is untouched")


def journal_storage_cases() -> tuple[ConformanceCase, ...]:
    """``SyncFileAdapter`` 的一致性检查。"""
    return (
        ConformanceCase("storage_create_then_read", "独占创建后整读得到同样的字节，别的实例可见", _storage_create_then_read),
        ConformanceCase("storage_create_is_exclusive", "已存在时创建失败（FileExistsError），原内容不变", _storage_create_is_exclusive),
        ConformanceCase("storage_append_is_ordered", "追加按顺序累积，跨实例可见", _storage_append_is_ordered),
        ConformanceCase("storage_missing_journal", "读不存在的 Journal 抛 FileNotFoundError", _storage_missing_journal),
        ConformanceCase("storage_journals_are_independent", "各 Journal 互不影响", _storage_journals_are_independent),
    )


# ---------------------------------------------------------------------------
# WriterLockAdapter
# ---------------------------------------------------------------------------


async def _lock_excludes_a_second_holder(harness: JournalStorageHarness) -> None:
    path = harness.path("ses_a.journal.lock")
    holder = harness.new_lock()
    handle = await _in_thread(lambda: holder.acquire(path))
    await _expect(
        WriterLockBusyError, _in_thread(lambda: harness.new_lock().acquire(path)),
        "acquiring a lock held by another instance",
    )
    await _expect(
        WriterLockBusyError, _in_thread(lambda: holder.acquire(path)),
        "acquiring a lock twice on the same instance",
    )
    await _in_thread(lambda: holder.release(handle))
    second = harness.new_lock()
    regained = await _in_thread(lambda: second.acquire(path))
    await _in_thread(lambda: second.release(regained))


async def _locks_are_per_session(harness: JournalStorageHarness) -> None:
    lock = harness.new_lock()
    one = await _in_thread(lambda: lock.acquire(harness.path("ses_one.journal.lock")))
    two = await _in_thread(lambda: lock.acquire(harness.path("ses_two.journal.lock")))
    await _in_thread(lambda: lock.release(one))
    await _expect(
        WriterLockBusyError,
        _in_thread(lambda: harness.new_lock().acquire(harness.path("ses_two.journal.lock"))),
        "a lock that is still held after releasing another one",
    )
    await _in_thread(lambda: lock.release(two))


def writer_lock_cases() -> tuple[ConformanceCase, ...]:
    """``WriterLockAdapter`` 的一致性检查。"""
    return (
        ConformanceCase("lock_excludes_a_second_holder", "同一把锁同时只有一个持有者（非阻塞，立即报 Busy），释放后可再取", _lock_excludes_a_second_holder),
        ConformanceCase("locks_are_per_session", "不同 Session 的锁互不影响", _locks_are_per_session),
    )


async def run_cases(
    cases: Sequence[ConformanceCase], harness_factory: Callable[[], Any],
) -> dict[str, str]:
    """不用测试框架时的跑法：每个 case 用一个新 harness，返回 {case 名: 失败原因}（空 = 全过）。

    ``harness_factory`` 每次调用须给出一份全新的存储；可以是同步函数，也可以返回 awaitable。
    """
    failures: dict[str, str] = {}
    for case in cases:
        harness = harness_factory()
        if asyncio.iscoroutine(harness):
            harness = await harness
        try:
            await case.run(harness)
        except ConformanceFailure as failure:
            failures[case.name] = str(failure)
    return failures


__all__ = [
    "JournalStorageHarness",
    "journal_storage_cases",
    "run_cases",
    "writer_lock_cases",
]
