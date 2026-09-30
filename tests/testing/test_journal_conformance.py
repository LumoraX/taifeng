"""SessionJournal 后端一致性检查（ADR 0114）跑在内核自带的实现上。

同一套检查验收三种后端形态，证明它对「换 core」与「换存储」两条路都成立：

- 默认的 ``JsonlSessionJournalCore``（本机文件 + ``flock``）——检查与参考实现一致；
- ``JsonlSessionJournalCore`` 装上内存里的 ``SyncFileAdapter`` / ``WriterLockAdapter``——换存储；
- ``InMemorySessionJournalCore``——换 core（只用公开的构件写成）。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from taifeng.conversation.journal.errors import CommitNotStartedError
from taifeng.conversation.journal.file_io import DefaultSyncFileAdapter
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.memory import InMemoryJournalStorage, InMemorySessionJournalCore
from taifeng.conversation.journal.writer_lock import FcntlWriterLockAdapter, WriterLockBusyError
from taifeng.testing import (
    ConformanceCase,
    ConformanceFailure,
    journal_core_cases,
    journal_storage_cases,
    run_cases,
    writer_lock_cases,
)

if TYPE_CHECKING:
    from pathlib import Path


class _SharedBytes:
    """内存里的「共享存储」：代表数据库 / 对象存储，多个适配器实例接在同一份上。"""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.locks: set[str] = set()
        self.guard = threading.Lock()


class _MemoryFileAdapter:
    """``SyncFileAdapter`` 的内存实现：键取路径的文件名。"""

    def __init__(self, shared: _SharedBytes) -> None:
        self._shared = shared

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        with self._shared.guard:
            if path.name in self._shared.blobs:
                raise CommitNotStartedError(FileExistsError(path.name))
            self._shared.blobs[path.name] = payload

    def read_bytes(self, path: Path) -> bytes:
        with self._shared.guard:
            if path.name not in self._shared.blobs:
                raise FileNotFoundError(path.name)
            return self._shared.blobs[path.name]

    def append_durable(self, path: Path, payload: bytes) -> None:
        with self._shared.guard:
            self._shared.blobs[path.name] = self._shared.blobs.get(path.name, b"") + payload


class _MemoryLockAdapter:
    """``WriterLockAdapter`` 的内存实现。"""

    def __init__(self, shared: _SharedBytes) -> None:
        self._shared = shared

    def acquire(self, path: Path) -> object:
        with self._shared.guard:
            if path.name in self._shared.locks:
                raise WriterLockBusyError(path)
            self._shared.locks.add(path.name)
            return path.name

    def release(self, handle: object) -> None:
        with self._shared.guard:
            self._shared.locks.remove(str(handle))


class _JsonlHarness:
    """默认实现：本机文件 + flock。``close()`` 只释放 writer，不写记录，即「进程消失」。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def new_core(self) -> JsonlSessionJournalCore:
        return JsonlSessionJournalCore(self._root)

    async def abandon(self, core: Any) -> None:
        await core.close()


class _AdaptedJsonlHarness(_JsonlHarness):
    """换存储：内核的 core + 外部的存储与锁适配器，不落任何本机文件。"""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.shared = _SharedBytes()

    async def new_core(self) -> JsonlSessionJournalCore:
        return JsonlSessionJournalCore(
            self._root,
            sync_file_adapter=_MemoryFileAdapter(self.shared),
            writer_lock_adapter=_MemoryLockAdapter(self.shared),
        )


class _MemoryCoreHarness:
    """换 core：多个实例接在同一份内存存储上。"""

    def __init__(self) -> None:
        self._storage = InMemoryJournalStorage()

    async def new_core(self) -> InMemorySessionJournalCore:
        return InMemorySessionJournalCore(self._storage)

    async def abandon(self, core: Any) -> None:
        await core.close()


_CORE_CASES = journal_core_cases()


@pytest.mark.parametrize("case", _CORE_CASES, ids=lambda case: case.name)
async def test_jsonl_core_conforms(case: ConformanceCase, tmp_path: Path) -> None:
    await case.run(_JsonlHarness(tmp_path))


@pytest.mark.parametrize("case", _CORE_CASES, ids=lambda case: case.name)
async def test_jsonl_core_on_external_storage_conforms(case: ConformanceCase, tmp_path: Path) -> None:
    harness = _AdaptedJsonlHarness(tmp_path / "unused")
    await case.run(harness)
    # 存储与锁都在适配器里：本机目录没有被创建，锁也都还了
    assert not (tmp_path / "unused").exists()
    if case.name != "unknown_session_cannot_be_opened":
        assert harness.shared.blobs


@pytest.mark.parametrize("case", _CORE_CASES, ids=lambda case: case.name)
async def test_in_memory_core_conforms(case: ConformanceCase) -> None:
    await case.run(_MemoryCoreHarness())


class _LocalStorageHarness:
    """默认的文件适配器与 flock。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def new_storage(self) -> DefaultSyncFileAdapter:
        return DefaultSyncFileAdapter()

    def new_lock(self) -> FcntlWriterLockAdapter:
        return FcntlWriterLockAdapter()

    def path(self, name: str) -> Path:
        return self._root / name


class _MemoryStorageHarness:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._shared = _SharedBytes()

    def new_storage(self) -> _MemoryFileAdapter:
        return _MemoryFileAdapter(self._shared)

    def new_lock(self) -> _MemoryLockAdapter:
        return _MemoryLockAdapter(self._shared)

    def path(self, name: str) -> Path:
        return self._root / name


@pytest.mark.parametrize("harness_type", [_LocalStorageHarness, _MemoryStorageHarness])
@pytest.mark.parametrize(
    "case", (*journal_storage_cases(), *writer_lock_cases()), ids=lambda case: case.name,
)
async def test_storage_and_lock_adapters_conform(
    case: ConformanceCase, harness_type: Any, tmp_path: Path,
) -> None:
    await case.run(harness_type(tmp_path))


def test_case_names_are_unique_and_described() -> None:
    cases = (*_CORE_CASES, *journal_storage_cases(), *writer_lock_cases())
    assert len({case.name for case in cases}) == len(cases)
    assert all(case.description for case in cases)
    assert len(_CORE_CASES) >= 20


# ---------------------------------------------------------------------------
# 检查本身要能查出问题：拿几个有缺陷的后端来验
# ---------------------------------------------------------------------------


class _NoCasCore(InMemorySessionJournalCore):
    """缺陷：不做 expected_seq 的 CAS。"""

    async def append_batch(self, records: Any, *, lease: Any, expected_seq: int) -> Any:
        tail = len(self._storage.sessions[records[0].session_id].envelopes)
        return await super().append_batch(records, lease=lease, expected_seq=tail)


class _NoFencingCore(InMemorySessionJournalCore):
    """缺陷：接管不要求原 writer 已释放。"""

    async def open_existing(self, session_id: str, *, writer_id: str, operation_id: str) -> Any:
        session = self._storage.sessions.get(session_id)
        if session is not None:
            session.holder = None
        return await super().open_existing(
            session_id, writer_id=writer_id, operation_id=operation_id)


class _LastWriteWinsCore(InMemorySessionJournalCore):
    """缺陷：并发追加各自按「读到的尾」落库，后写的盖掉先写的位置（没有事务）。"""

    async def append_batch(self, records: Any, *, lease: Any, expected_seq: int) -> Any:
        import asyncio

        from taifeng.conversation.journal.backend import seal_batch

        stored = self._storage.sessions[records[0].session_id]
        tail_seq, tail_hash = stored.tail_seq, stored.tail_hash
        await asyncio.sleep(0)  # 读与写之间让出：别的追加插了进来
        sealed = seal_batch(
            records, previous_seq=tail_seq, previous_hash=tail_hash,
            writer_epoch=lease.writer_epoch)
        stored.envelopes.extend(sealed.envelopes)
        return sealed.ack


class _ReorderingCore(InMemorySessionJournalCore):
    """缺陷：读取不按 seq 顺序。"""

    async def load(self, session_id: str, *, after_seq: int = 0) -> Any:
        envelopes = [e async for e in super().load(session_id, after_seq=after_seq)]
        for envelope in reversed(envelopes):
            yield envelope


@pytest.mark.parametrize(
    ("broken", "caught_by"),
    [
        (_NoCasCore, {"stale_expected_seq_conflicts"}),
        (_NoFencingCore, {"second_writer_is_refused"}),
        (_LastWriteWinsCore, {"concurrent_appends_commit_once", "complete_retry_is_idempotent"}),
        (_ReorderingCore, {"create_writes_initialization", "load_honours_after_seq"}),
    ],
)
async def test_the_suite_catches_broken_backends(broken: Any, caught_by: set[str]) -> None:
    def harness() -> Any:
        storage = InMemoryJournalStorage()

        class _Harness:
            async def new_core(self) -> Any:
                return broken(storage)

            async def abandon(self, core: Any) -> None:
                await core.close()

        return _Harness()

    failures = await run_cases(_CORE_CASES, harness)
    assert caught_by <= set(failures), failures


async def test_run_cases_reports_nothing_for_a_conforming_backend() -> None:
    assert await run_cases(_CORE_CASES, _MemoryCoreHarness) == {}


async def test_a_failure_names_what_was_expected() -> None:
    def harness() -> Any:
        storage = InMemoryJournalStorage()

        class _Harness:
            async def new_core(self) -> Any:
                return _NoCasCore(storage)

            async def abandon(self, core: Any) -> None:
                await core.close()

        return _Harness()

    failures = await run_cases(_CORE_CASES, harness)
    assert "expected JournalConflictError" in failures["stale_expected_seq_conflicts"]
    assert issubclass(ConformanceFailure, AssertionError)


def test_the_reference_backend_uses_public_names_only() -> None:
    """参考实现只 import 公共 API 里有的名字：外部包照着它写，不需要碰内部模块。"""
    import ast
    import inspect

    import taifeng.experimental
    from taifeng.conversation.journal import memory

    public = set(taifeng.experimental.__all__)
    imported = {
        alias.name
        for node in ast.walk(ast.parse(inspect.getsource(memory)))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("taifeng")
        for alias in node.names
    }
    assert imported - public == set(), sorted(imported - public)


def test_testing_package_exports_resolve() -> None:
    import taifeng.testing

    for name in taifeng.testing.__all__:
        assert getattr(taifeng.testing, name) is not None
