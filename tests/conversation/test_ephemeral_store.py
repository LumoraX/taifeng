"""短命 Responses 原子存储的行为契约。"""

from __future__ import annotations

import pytest

from taifeng.conversation import (
    AtomicBatchMessageStore,
    BatchConflictError,
    EphemeralAtomicBatchMessageStore,
    EphemeralStoreClosedError,
    EphemeralStoreLimitError,
    EphemeralStoreLimits,
    MessageStore,
    assistant_message,
    user_message,
)


async def _items(store: EphemeralAtomicBatchMessageStore, thread_id: str) -> list:
    """读取短命 thread 快照。"""
    iterator = await store.load_thread(thread_id)
    return [item async for item in iterator]


@pytest.mark.asyncio
async def test_atomic_batch_is_idempotent_and_all_or_nothing() -> None:
    """同批次重试不重复写入，冲突内容拒绝。"""
    store = EphemeralAtomicBatchMessageStore()
    thread_id = await store.create_thread(cwd="/tmp/test")
    items = [
        user_message("hello", thread_id=thread_id),
        assistant_message("world", thread_id=thread_id, model="sim"),
    ]

    first = await store.append_atomic_batch(items, batch_id="sample-1")
    retry = await store.append_atomic_batch(items, batch_id="sample-1")

    assert first.item_ids == tuple(item.id for item in items)
    assert retry.already_committed is True
    assert await _items(store, thread_id) == items
    with pytest.raises(BatchConflictError):
        await store.append_atomic_batch(
            [assistant_message("changed", thread_id=thread_id, model="sim")],
            batch_id="sample-1",
        )
    assert await _items(store, thread_id) == items


@pytest.mark.asyncio
async def test_public_protocols_and_cross_thread_batches() -> None:
    """短命实现可直接注入 EnginePool，跨 thread 批次在提交前拒绝。"""
    store = EphemeralAtomicBatchMessageStore()
    assert isinstance(store, MessageStore)
    assert isinstance(store, AtomicBatchMessageStore)
    first = await store.create_thread()
    second = await store.create_thread()
    with pytest.raises(ValueError, match="one thread"):
        await store.append_atomic_batch(
            [user_message("a", thread_id=first), user_message("b", thread_id=second)],
            batch_id="cross-thread",
        )
    assert await _items(store, first) == []
    assert await _items(store, second) == []


@pytest.mark.asyncio
async def test_returned_items_are_deep_snapshots() -> None:
    """读取与提交都隔离可变 payload。"""
    store = EphemeralAtomicBatchMessageStore()
    thread_id = await store.create_thread()
    item = user_message("hello", thread_id=thread_id, attachments=[{"name": "a"}])
    await store.append(item)
    item.payload["attachments"][0]["name"] = "caller-mutated"
    loaded = await _items(store, thread_id)
    loaded[0].payload["attachments"][0]["name"] = "reader-mutated"
    reread = await _items(store, thread_id)
    assert reread[0].payload["attachments"][0]["name"] == "a"


@pytest.mark.asyncio
async def test_close_clears_state_and_rejects_future_use() -> None:
    """关闭后不留下可恢复消息。"""
    store = EphemeralAtomicBatchMessageStore()
    thread_id = await store.create_thread()
    await store.append(user_message("secret", thread_id=thread_id))
    await store.close()

    with pytest.raises(EphemeralStoreClosedError):
        await store.load_thread(thread_id)
    with pytest.raises(EphemeralStoreClosedError):
        await store.create_thread()


@pytest.mark.asyncio
async def test_thread_listing_and_resume_are_instance_local() -> None:
    """列表与 resume 只观察当前短命实例。"""
    store = EphemeralAtomicBatchMessageStore()
    first = await store.create_thread(cwd="a", entry_skill_id="one")
    second = await store.create_thread(cwd="b", entry_skill_id="two")
    await store.append(user_message("latest", thread_id=second))

    page = await store.list_threads(cwd="b")
    assert [item.thread_id for item in page] == [second]
    assert page[0].item_count == 1
    assert await store.select_resume_path("a") == first
    assert await store.select_resume_path("missing") is None


@pytest.mark.asyncio
async def test_resource_limits_reject_thread_and_item_growth() -> None:
    """线程数与单线程消息数超限时，旧状态保持不变。"""
    store = EphemeralAtomicBatchMessageStore(
        limits=EphemeralStoreLimits(max_threads=1, max_items_per_thread=1)
    )
    first = await store.create_thread()
    with pytest.raises(EphemeralStoreLimitError, match="max_threads"):
        await store.create_thread()
    await store.append(user_message("one", thread_id=first))
    with pytest.raises(EphemeralStoreLimitError, match="max_items_per_thread"):
        await store.append(user_message("two", thread_id=first))
    assert [item.payload["text"] for item in await _items(store, first)] == ["one"]


@pytest.mark.asyncio
async def test_resource_limit_rejects_bytes_before_atomic_commit() -> None:
    """字节上限在 batch 写入前校验，不能留下半批消息。"""
    store = EphemeralAtomicBatchMessageStore(
        limits=EphemeralStoreLimits(max_total_bytes=1)
    )
    thread_id = await store.create_thread()
    items = [user_message("payload", thread_id=thread_id)]
    with pytest.raises(EphemeralStoreLimitError, match="max_total_bytes"):
        await store.append_atomic_batch(items, batch_id="too-large")
    assert await _items(store, thread_id) == []


@pytest.mark.asyncio
async def test_close_resets_resource_usage() -> None:
    """close 清空资源计数，实例关闭后不保留容量状态。"""
    store = EphemeralAtomicBatchMessageStore()
    thread_id = await store.create_thread()
    await store.append(user_message("payload", thread_id=thread_id))
    assert store.usage().total_items == 1
    await store.close()
    assert store.usage().total_items == 0


@pytest.mark.asyncio
async def test_atomic_retry_is_idempotent_even_when_capacity_is_full() -> None:
    """同 batch 重试先返回幂等确认，不被当前容量拒绝遮蔽。"""
    store = EphemeralAtomicBatchMessageStore(
        limits=EphemeralStoreLimits(max_items_per_thread=1, max_total_items=1)
    )
    thread_id = await store.create_thread()
    item = user_message("one", thread_id=thread_id)
    first = await store.append_atomic_batch([item], batch_id="full")
    retry = await store.append_atomic_batch([item], batch_id="full")
    assert first.item_ids == retry.item_ids
    assert retry.already_committed


@pytest.mark.asyncio
async def test_batch_limits_are_checked_across_threads() -> None:
    """普通 batch 的实例总上限覆盖跨 thread 的聚合增量。"""
    store = EphemeralAtomicBatchMessageStore(
        limits=EphemeralStoreLimits(max_total_items=1)
    )
    first = await store.create_thread()
    second = await store.create_thread()
    with pytest.raises(EphemeralStoreLimitError, match="max_total_items"):
        await store.append_batch([
            user_message("a", thread_id=first),
            user_message("b", thread_id=second),
        ])
    assert await _items(store, first) == []
    assert await _items(store, second) == []
