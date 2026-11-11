"""MessageStore 协议 —— 业务侧可替换。

参照：codex codex-rs/rollout/src/recorder.rs
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from taifeng.conversation.models import ThreadInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from taifeng.conversation.models import ResponseItem


@runtime_checkable
class MessageStore(Protocol):
    """会话存储协议。

    默认实现：``JsonlMessageStore``（追加写 + SQLite 旁路索引）
    业务实现示例：``PostgresMessageStore``（直接落 DB，无 resume）
    """

    async def append(self, item: ResponseItem) -> None:
        """追加单条消息。热路径，必须 < 5ms。"""
        ...

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """批量追加。可优化 fsync。"""
        ...

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """按 thread_id 加载全部消息。"""
        ...

    async def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int = 50,
    ) -> list[ThreadInfo]:
        """按 cwd 过滤列出最近 thread。"""
        ...

    async def create_thread(
        self,
        *,
        cwd: str | None = None,
        entry_skill_id: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """创建新 thread，返回 thread_id。

        ``extra``：JSON-safe 业务/内核元数据，落入 ``ThreadMetadata.extra``（K7：子 skill
        派发时记 ``parent_thread_id`` / ``spawn_depth``，使 resume 可重导谱系与深度）。
        """
        ...

    async def select_resume_path(self, cwd: str) -> str | None:
        """按 cwd 启发式选择 resume thread_id。"""
        ...

    async def close(self) -> None:
        """关闭底层资源（文件句柄 / 数据库连接）。"""
        ...


@dataclass(frozen=True, slots=True)
class BatchAppendAck:
    """原子 batch 完成 durable ack 后返回的稳定结果。"""

    batch_id: str
    digest: str
    item_ids: tuple[str, ...]
    already_committed: bool = False


class BatchConflictError(ValueError):
    """同一 stable batch id 已对应其他内容。"""


class EphemeralStoreClosedError(RuntimeError):
    """短命存储已经关闭，不能继续接受或读取数据。"""


@runtime_checkable
class AtomicBatchMessageStore(Protocol):
    """Responses terminal 输出所需的 crash-atomic 可见性能力。"""

    async def append_atomic_batch(
        self,
        items: Sequence[ResponseItem],
        *,
        batch_id: str,
    ) -> BatchAppendAck:
        """完整 batch durable 后返回；相同 batch/content 重试必须幂等。"""
        ...


@dataclass
class _EphemeralThread:
    """短命 thread 的进程内状态。"""

    thread_id: str
    cwd: str | None
    entry_skill_id: str
    source: str
    created_at: float
    updated_at: float
    items: list[ResponseItem] = field(default_factory=list)


class EphemeralAtomicBatchMessageStore(MessageStore, AtomicBatchMessageStore):
    """只存在当前存储实例内的原子消息存储。

    该实现不写文件、数据库或外部缓存，也不提供崩溃恢复承诺。它用于明确声明
    ``Responses`` 的不可恢复短命执行边界；关闭时立即清空全部 thread、消息和批次记录。
    """

    def __init__(self) -> None:
        """创建空的短命存储实例。"""
        self._threads: dict[str, _EphemeralThread] = {}
        self._batches: dict[str, tuple[str, tuple[str, ...], tuple[ResponseItem, ...]]] = {}
        self._closed = False

    def _ensure_open(self) -> None:
        """拒绝在关闭后继续使用实例。"""
        if self._closed:
            raise EphemeralStoreClosedError("ephemeral message store is closed")

    def _thread(self, thread_id: str) -> _EphemeralThread:
        """读取现有 thread，避免静默创建错误的会话。"""
        self._ensure_open()
        try:
            return self._threads[thread_id]
        except KeyError as error:
            raise KeyError(f"thread not found: {thread_id}") from error

    @staticmethod
    def _clone(item: ResponseItem) -> ResponseItem:
        """复制消息，避免调用方修改已提交的短命事实。"""
        return item.model_copy(deep=True)

    async def create_thread(
        self,
        *,
        cwd: str | None = None,
        entry_skill_id: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """创建只存在于当前实例的 thread；``extra`` 仅为兼容协议而接受。"""
        del extra
        self._ensure_open()
        now = time.time()
        thread_id = f"thr_ephemeral_{secrets.token_hex(8)}"
        self._threads[thread_id] = _EphemeralThread(
            thread_id=thread_id,
            cwd=cwd,
            entry_skill_id=entry_skill_id or "general",
            source=source or "user",
            created_at=now,
            updated_at=now,
        )
        return thread_id

    async def append(self, item: ResponseItem) -> None:
        """追加单条消息；短命实现不触碰任何持久化介质。"""
        thread = self._thread(item.thread_id)
        thread.items.append(self._clone(item))
        thread.updated_at = time.time()

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """在内存中追加普通消息组。"""
        if not items:
            return
        grouped: dict[str, list[ResponseItem]] = {}
        for item in items:
            grouped.setdefault(item.thread_id, []).append(item)
        for thread_id, group in grouped.items():
            thread = self._thread(thread_id)
            thread.items.extend(self._clone(item) for item in group)
            thread.updated_at = time.time()

    async def append_atomic_batch(
        self,
        items: Sequence[ResponseItem],
        *,
        batch_id: str,
    ) -> BatchAppendAck:
        """原子提交 Responses 终态批次，并在当前实例内提供幂等重试。"""
        self._ensure_open()
        if not batch_id:
            raise ValueError("batch_id must be non-empty")
        materialized = [self._clone(item) for item in items]
        if not materialized:
            raise ValueError("atomic batch must contain at least one item")
        thread_ids = {item.thread_id for item in materialized}
        if len(thread_ids) != 1:
            raise ValueError("atomic batch items must belong to one thread")
        thread_id = next(iter(thread_ids))
        self._thread(thread_id)
        from taifeng.conversation.jsonl_atomic import describe_batch

        descriptor = describe_batch(materialized, batch_id)
        existing = self._batches.get(batch_id)
        if existing is not None:
            old_digest, old_ids, _ = existing
            if (old_digest, old_ids) != (descriptor.digest, descriptor.item_ids):
                raise BatchConflictError(f"batch id conflict: {batch_id}")
            return BatchAppendAck(
                batch_id=batch_id,
                digest=old_digest,
                item_ids=old_ids,
                already_committed=True,
            )
        committed = tuple(materialized)
        self._batches[batch_id] = (descriptor.digest, descriptor.item_ids, committed)
        self._threads[thread_id].items.extend(self._clone(item) for item in committed)
        self._threads[thread_id].updated_at = time.time()
        return BatchAppendAck(
            batch_id=batch_id,
            digest=descriptor.digest,
            item_ids=descriptor.item_ids,
        )

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """按提交顺序读取当前实例中的 thread 快照。"""
        thread = self._thread(thread_id)
        snapshot = [self._clone(item) for item in thread.items]

        async def _items() -> AsyncIterator[ResponseItem]:
            for item in snapshot:
                yield item

        return _items()

    async def list_threads(self, *, cwd: str | None = None, limit: int = 50) -> list[ThreadInfo]:
        """返回当前实例中的 thread，按最近活动时间倒序。"""
        self._ensure_open()
        if limit < 0:
            raise ValueError("limit must be non-negative")
        threads = [thread for thread in self._threads.values() if cwd is None or thread.cwd == cwd]
        threads.sort(key=lambda thread: thread.updated_at, reverse=True)
        return [
            ThreadInfo(
                thread_id=thread.thread_id,
                cwd=thread.cwd,
                created_at=datetime.fromtimestamp(thread.created_at, tz=UTC),
                last_activity_at=datetime.fromtimestamp(thread.updated_at, tz=UTC),
                item_count=len(thread.items),
                entry_skill_id=thread.entry_skill_id,
                source=thread.source,
            )
            for thread in threads[:limit]
        ]

    async def select_resume_path(self, cwd: str) -> str | None:
        """选择当前实例中指定 cwd 最近活动的 thread。"""
        rows = await self.list_threads(cwd=cwd, limit=1)
        return rows[0].thread_id if rows else None

    async def close(self) -> None:
        """清空短命状态并关闭实例；不留下可恢复材料。"""
        self._threads.clear()
        self._batches.clear()
        self._closed = True


class EphemeralMessageStore(EphemeralAtomicBatchMessageStore):
    """短命消息存储的简洁公共名称。"""
