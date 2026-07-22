"""Journal projector 测试共享的 DTO factory 与可观察 stores。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.conversation.journal.canonical import model_canonical_data
from taifeng.conversation.journal.framing import encode_batch
from taifeng.conversation.journal.models import (
    ActorRef,
    JournalAck,
    JournalEnvelope,
    JournalRecord,
)
from taifeng.conversation.journal.records import serialize_response_item
from taifeng.conversation.transcript import JsonlMessageStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.conversation.journal.projector import ProjectionResult
    from taifeng.conversation.models import ResponseItem

_NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
_ZERO_HASH = "0" * 64


def _conversation_record(item: ResponseItem, *, record_id: str) -> JournalRecord:
    """把测试 item 包装成明确的 conversation_item record。"""
    payload = serialize_response_item(item, source_record_id=f"source_{record_id}")
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="conversation_item",
        actor=ActorRef(kind="system", source="test"),
        payload=model_canonical_data(payload),
        thread_id=item.thread_id,
    )


def _domain_record(*, record_id: str) -> JournalRecord:
    """构造 ack 内允许存在的非 conversation 领域 record。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="turn_completed",
        actor=ActorRef(kind="system", source="test"),
        payload={"payload_version": 1, "status": "complete"},
        thread_id="thr_1",
    )


def _encoded(
    records: tuple[JournalRecord, ...], *, expected_seq: int = 3
) -> tuple[tuple[JournalEnvelope, ...], JournalAck]:
    """通过真实 frame codec 构造匹配的 envelopes/ack。"""
    batch = encode_batch(
        records,
        batch_id=f"batch_{expected_seq}_{len(records)}",
        expected_seq=expected_seq,
        writer_epoch=2,
        previous_hash=_ZERO_HASH,
        recorded_at=_NOW,
    )
    return batch.envelopes, batch.ack


class _MemoryProjectionStore:
    """可观察写效果并注入部分失败的最小投影 store。"""

    def __init__(self) -> None:
        self.items: dict[str, list[ResponseItem]] = {}
        self.append_calls = 0
        self.create_calls = 0
        self.fail_after_first_once = False
        self.fail_before_write_once = False
        self.fail_threads: set[str] = set()
        self.fail_load_threads: set[str] = set()
        self._projection_locks: dict[str, anyio.Lock] = {}
        self._projection_states: dict[str, tuple[ProjectionResult, int | None]] = {}

    def projection_lock(self, thread_id: str) -> anyio.Lock:
        """让同一 fake store 上的多个 projector 共享 thread 锁。"""
        return self._projection_locks.setdefault(thread_id, anyio.Lock())

    async def ensure_projection_thread(self, thread_id: str) -> None:
        """内存 fake 不需要修复 metadata 文件。"""

    def projection_state(
        self, thread_id: str
    ) -> tuple[ProjectionResult | None, int | None]:
        """读取 store-owned projection state。"""
        return self._projection_states.get(thread_id, (None, None))

    def update_projection_state(
        self,
        thread_id: str,
        result: ProjectionResult,
        blocked_seq: int | None,
    ) -> None:
        """在共享锁内更新 store-owned projection state。"""
        self._projection_states[thread_id] = (result, blocked_seq)

    async def create_projection_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """记录 bootstrap 效果。"""
        self.create_calls += 1
        if thread_id in self.items:
            raise FileExistsError(thread_id)
        self.items[thread_id] = []
        return thread_id

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """按需在首条写入后失败，模拟非原子 materialization。"""
        self.append_calls += 1
        if self.fail_before_write_once:
            self.fail_before_write_once = False
            raise OSError("injected pre-write projection failure")
        if items and items[0].thread_id in self.fail_threads:
            raise OSError("injected projection failure")
        if self.fail_after_first_once:
            self.fail_after_first_once = False
            first = items[0]
            self.items.setdefault(first.thread_id, []).append(first)
            raise OSError("injected partial projection failure")
        for item in items:
            self.items.setdefault(item.thread_id, []).append(item)

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """返回当前持久化 history 的异步快照。"""
        if thread_id in self.fail_load_threads:
            raise OSError("injected projection load failure")
        snapshot = list(self.items.get(thread_id, ()))

        async def _items() -> AsyncIterator[ResponseItem]:
            for item in snapshot:
                yield item

        return _items()


class _YieldingProjectionStore(_MemoryProjectionStore):
    """append 前让出调度，稳定暴露 load-then-append 并发竞争。"""

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """让并发 project 有机会同时观察旧 history。"""
        self.append_calls += 1
        await anyio.lowlevel.checkpoint()
        for item in items:
            self.items.setdefault(item.thread_id, []).append(item)


class _YieldingJsonlMessageStore(JsonlMessageStore):
    """使用真实 JSONL，仅在 append 前让出调度以放大跨 projector 竞争。"""

    def __init__(self, threads_dir: Path) -> None:
        super().__init__(threads_dir)
        self.append_calls = 0

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """让两个 projector 有机会在落盘前分别完成 history 检查。"""
        self.append_calls += 1
        await anyio.lowlevel.checkpoint()
        await super().append_batch(items)


class _FailingDirectory:
    """在 JSONL exclusive-create 后拒绝 metadata 注册。"""

    def __init__(self) -> None:
        self.upsert_calls = 0

    async def upsert_metadata(self, metadata: object) -> None:
        """模拟 derived directory 暂时不可用。"""
        self.upsert_calls += 1
        raise OSError("injected directory failure")

    async def close(self) -> None:
        """满足 store close 路径。"""


async def _history(store: _MemoryProjectionStore, thread_id: str) -> list[ResponseItem]:
    """收集 fake store 的异步 history。"""
    return [item async for item in await store.load_thread(thread_id)]


async def _create_explicit_projection(store: JsonlMessageStore) -> str:
    """用完整审计 metadata 创建固定测试 projection。"""
    return await store.create_projection_thread(
        thread_id="thr_explicit",
        cwd="/work",
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_session_id": "ses_1",
            "journal_schema_version": 1,
        },
    )
