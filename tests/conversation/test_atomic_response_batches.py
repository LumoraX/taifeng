"""Responses terminal 输出的 JSONL 原子 batch 可见性测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng.conversation import JsonlMessageStore, assistant_message, reasoning
from taifeng.conversation.store import (
    AtomicBatchMessageStore,
    BatchAppendAck,
    BatchConflictError,
)

if TYPE_CHECKING:
    from pathlib import Path


async def _loaded(store: JsonlMessageStore, thread_id: str):
    """收集 legacy async iterator，便于断言冷恢复可见历史。"""
    stream = await store.load_thread(thread_id)
    return [item async for item in stream]


def test_jsonl_store_declares_atomic_batch_capability(tmp_path: Path) -> None:
    """默认 JSONL store 必须显式满足 Responses 原子提交协议。"""
    assert isinstance(JsonlMessageStore(tmp_path), AtomicBatchMessageStore)


@pytest.mark.asyncio
async def test_atomic_response_batch_is_visible_only_after_commit(tmp_path: Path) -> None:
    """完整 begin/items/commit 在冷恢复时一次性发布。"""
    store = JsonlMessageStore(tmp_path)
    thread_id = await store.create_thread()
    items = [
        reasoning("", summary="检查图片", thread_id=thread_id),
        assistant_message("库存编号 A-17", thread_id=thread_id, model="gpt-5.6"),
    ]

    ack = await store.append_atomic_batch(items, batch_id="sample-1")

    assert ack.batch_id == "sample-1"
    assert ack.already_committed is False
    loaded = await _loaded(JsonlMessageStore(tmp_path), thread_id)
    assert [item.kind for item in loaded] == ["reasoning", "assistant_message"]
    assert all(item.metadata["commit_batch_id"] == "sample-1" for item in loaded)


@pytest.mark.asyncio
async def test_orphan_response_batch_is_invisible_after_restart(tmp_path: Path) -> None:
    """缺少 commit frame 的 terminal batch 不得向冷恢复暴露半个响应。"""
    store = JsonlMessageStore(tmp_path)
    thread_id = await store.create_thread()
    await store.append_atomic_batch(
        [assistant_message("不完整", thread_id=thread_id, model="gpt-5.6")],
        batch_id="sample-orphan",
    )
    path = tmp_path / f"{thread_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    assert await _loaded(JsonlMessageStore(tmp_path), thread_id) == []


@pytest.mark.asyncio
async def test_orphan_retry_with_new_frame_becomes_visible_once(tmp_path: Path) -> None:
    """孤儿物理 attempt 后，同一 stable batch 可重试并只发布一次。"""
    store = JsonlMessageStore(tmp_path)
    thread_id = await store.create_thread()
    item = assistant_message("完成", thread_id=thread_id, model="gpt-5.6")
    await store.append_atomic_batch([item], batch_id="sample-retry")
    path = tmp_path / f"{thread_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    retried = await JsonlMessageStore(tmp_path).append_atomic_batch(
        [item], batch_id="sample-retry"
    )

    assert retried.already_committed is False
    loaded = await _loaded(JsonlMessageStore(tmp_path), thread_id)
    assert [entry.id for entry in loaded] == [item.id]


@pytest.mark.asyncio
async def test_corrupt_framed_item_never_falls_back_to_bare_visibility(
    tmp_path: Path,
) -> None:
    """frame 中一行损坏后，其余 commit-tagged items 也不能被当成旧 bare 行。"""
    store = JsonlMessageStore(tmp_path)
    thread_id = await store.create_thread()
    await store.append_atomic_batch(
        [
            reasoning("", summary="private", thread_id=thread_id),
            assistant_message("不得暴露", thread_id=thread_id, model="gpt-5.6"),
        ],
        batch_id="sample-corrupt",
    )
    path = tmp_path / f"{thread_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = "not-json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert await _loaded(JsonlMessageStore(tmp_path), thread_id) == []


@pytest.mark.asyncio
async def test_atomic_batch_retry_is_idempotent_and_conflict_is_stable(tmp_path: Path) -> None:
    """同 digest 返回 already committed；同 batch id 不同内容稳定冲突。"""
    store = JsonlMessageStore(tmp_path)
    thread_id = await store.create_thread()
    original = assistant_message("完成", thread_id=thread_id, model="gpt-5.6")
    first = await store.append_atomic_batch([original], batch_id="sample-stable")
    duplicate = await store.append_atomic_batch([original], batch_id="sample-stable")

    assert first.already_committed is False
    assert duplicate.already_committed is True
    with pytest.raises(BatchConflictError):
        await store.append_atomic_batch(
            [assistant_message("不同", thread_id=thread_id, model="gpt-5.6")],
            batch_id="sample-stable",
        )
    loaded = await _loaded(store, thread_id)
    assert [entry.id for entry in loaded] == [original.id]


@pytest.mark.asyncio
async def test_atomic_batch_conflict_is_serialized_across_writer_instances(
    tmp_path: Path,
) -> None:
    """两个 writer 对同一 batch 的不同内容只能有一个获得 durable ack。"""
    first = JsonlMessageStore(tmp_path)
    thread_id = await first.create_thread()
    second = JsonlMessageStore(tmp_path)
    item_a = assistant_message("first", thread_id=thread_id, model="gpt-5.6")
    item_b = assistant_message("second", thread_id=thread_id, model="gpt-5.6")

    results = await asyncio.gather(
        first.append_atomic_batch([item_a], batch_id="same-batch"),
        second.append_atomic_batch([item_b], batch_id="same-batch"),
        return_exceptions=True,
    )
    await first.close()
    await second.close()

    assert sum(isinstance(value, BatchAppendAck) for value in results) == 1
    assert sum(isinstance(value, BatchConflictError) for value in results) == 1


# ---------------------------------------------------------------------------
# torn tail（wave1 task 6）：crash 遗留半行不得吞掉后续 durable ack 过的整批
# ---------------------------------------------------------------------------

from taifeng.conversation.jsonl_atomic import (  # noqa: E402
    OrphanCommittedItemError,
    read_state,
)
from taifeng.conversation.transcript import JsonlMessageWriter  # noqa: E402


class _RecordingSink:
    """收集 EventMsg 的测试 sink。"""

    def __init__(self) -> None:
        self.events: list = []

    async def handle(self, ev) -> None:  # noqa: ANN001
        self.events.append(ev)


def _append_torn_tail(path: Path) -> None:
    """同步 helper：在文件末尾追加一条没有换行的半截 JSON（模拟 crash）。"""
    with path.open("a", encoding="utf-8") as f:
        f.write('{"kind":"assistant_message","id":"torn","pay')


@pytest.mark.asyncio
async def test_atomic_batch_after_torn_tail_is_still_visible(tmp_path: Path) -> None:
    """半行之后 append_atomic_batch → items 可见、batch 已提交、半行独立成一条 corrupt。"""
    store = JsonlMessageStore(tmp_path)
    thread_id = await store.create_thread()
    from taifeng.conversation import user_message
    await store.append(user_message("hello", thread_id=thread_id))
    path = tmp_path / f"{thread_id}.jsonl"
    _append_torn_tail(path)

    items = [
        assistant_message("after-crash", thread_id=thread_id, model="gpt-5.6"),
        reasoning("", summary="r", thread_id=thread_id),
    ]
    ack = await JsonlMessageStore(tmp_path).append_atomic_batch(items, batch_id="b1")
    assert ack.already_committed is False

    state = read_state(path)
    assert [it.kind for it in state.items] == [
        "user_message", "assistant_message", "reasoning",
    ]
    assert "b1" in state.committed
    assert len(state.corrupt) == 1  # 只有那条半行


@pytest.mark.asyncio
async def test_orphan_committed_item_is_reported_as_corrupt(tmp_path: Path) -> None:
    """batch 外携带 commit_batch_id 的 item 记为 corrupt 并发事件，不再静默跳过。"""
    sink = _RecordingSink()
    writer = JsonlMessageWriter(tmp_path, sink=sink)
    thread_id = await writer.create_thread(entry_skill_id="entry")
    path = tmp_path / f"{thread_id}.jsonl"
    orphan = assistant_message("orphan", thread_id=thread_id, model="gpt-5.6")
    orphan = orphan.model_copy(
        update={"metadata": {**orphan.metadata, "commit_batch_id": "b9"}},
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(orphan.model_dump_json() + "\n")

    state = read_state(path)
    assert state.items == ()
    assert len(state.corrupt) == 1
    assert isinstance(state.corrupt[0][1], OrphanCommittedItemError)

    loaded = await writer.load_history(thread_id)
    assert loaded == []
    kinds = [e.msg.kind for e in sink.events]
    assert kinds.count("transcript_skipped_corrupt_line") == 1
