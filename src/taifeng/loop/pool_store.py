"""EnginePool 的 store 组装：hook 转发的 store 包装、store 绑定解析与工厂组件准备。

从 ``pool.py`` 原样搬出（W7.1 拆文件，零行为变更）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import ResponseItem, ThreadInfo, ThreadMetadata
from taifeng.conversation.store import AtomicBatchMessageStore, BatchAppendAck, MessageStore
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.errors import UnsupportedPersistenceCapabilityError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from taifeng.conversation.hook_runner import HookRunner
    from taifeng.conversation.protocols import ThreadDirectory

class _HookEmittingStore(MessageStore):
    """MessageStore 代理 —— 所有写操作完成后 spawn IndexHook 后台 task（fire-and-forget）。

    EnginePool 在用户传入 ``index_hook`` 时用本代理包装真实 store，让 engine / turn 透明走 hook。
    业务对协议无感（仍是 MessageStore），但所有 ``create_thread`` / ``append`` 都会触发对应 hook。
    """

    def __init__(
        self,
        *,
        inner: MessageStore,
        runner: HookRunner,
        directory: ThreadDirectory,
        custom_directory: object | None = None,
        index_hook: object | None = None,
    ) -> None:
        self._inner = inner
        self._runner = runner
        self._directory = directory
        self._audit_custom_directory = custom_directory
        self._audit_index_hook = index_hook

    async def create_thread(
        self,
        *,
        cwd: str | None = None,
        entry_skill_id: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        thread_id = await self._inner.create_thread(
            cwd=cwd, entry_skill_id=entry_skill_id, source=source, extra=extra
        )
        # 拉取刚写入的 metadata 作为 hook 入参（兼容封装内 JsonlMessageStore
        # 已 upsert 到 directory）。
        meta = await self._directory.get_metadata(thread_id)
        if meta is None:
            # 极少数情况（用户传入自定义 store 不 upsert 元数据）：合成最小 metadata
            import time as _t
            meta = ThreadMetadata(
                thread_id=thread_id,
                created_at=_t.time(),
                updated_at=_t.time(),
                entry_skill_id=entry_skill_id or "general",
                source=source or "user",
                tags=(),
                extra={"cwd": cwd} if cwd is not None else {},
            )
        self._runner.spawn_on_thread_created(meta)
        return thread_id

    async def append(self, item: ResponseItem) -> None:
        await self._inner.append(item)
        self._runner.spawn_on_message_appended(item.thread_id, [item])

    async def append_batch(self, items: list[ResponseItem]) -> None:
        if not items:
            return
        await self._inner.append_batch(items)
        # 按 thread 分组发 hook
        by_thread: dict[str, list[ResponseItem]] = {}
        for it in items:
            by_thread.setdefault(it.thread_id, []).append(it)
        for tid, group in by_thread.items():
            self._runner.spawn_on_message_appended(tid, group)

    async def append_atomic_batch(
        self,
        items: Sequence[ResponseItem],
        *,
        batch_id: str,
    ) -> BatchAppendAck:
        """把 Responses 原子提交委派给 inner，并在新提交后触发 hook。"""
        if not isinstance(self._inner, AtomicBatchMessageStore):
            raise UnsupportedPersistenceCapabilityError(
                "wrapped store does not support atomic response batches"
            )
        ack = await self._inner.append_atomic_batch(items, batch_id=batch_id)
        if ack.already_committed:
            return ack
        by_thread: dict[str, list[ResponseItem]] = {}
        for item in items:
            by_thread.setdefault(item.thread_id, []).append(item)
        for thread_id, group in by_thread.items():
            self._runner.spawn_on_message_appended(thread_id, group)
        return ack

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        return await self._inner.load_thread(thread_id)

    async def list_threads(
        self, *, cwd: str | None = None, limit: int = 50
    ) -> list[ThreadInfo]:
        return await self._inner.list_threads(cwd=cwd, limit=limit)

    async def select_resume_path(self, cwd: str) -> str | None:
        return await self._inner.select_resume_path(cwd)

    async def audited_projection_marker(self, thread_id: str) -> object | None:
        """把 metadata-only audited marker 检查委派给默认 JSONL store。"""
        if type(self._inner) is not JsonlMessageStore:
            return None
        return await self._inner.audited_projection_marker(thread_id)

    async def close(self) -> None:
        # 不在此处 shutdown runner —— 由 pool.close 统一调度（先 await hook，后关 store）
        await self._inner.close()

__all__ = [
    "_HookEmittingStore",
]
