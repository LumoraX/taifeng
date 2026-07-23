"""Conversation projection generation-reset 完整性回归测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from taifeng.conversation.journal.projector import (
    JournalConversationProjector,
    ProjectionResult,
)
from taifeng.conversation.models import user_message
from taifeng.conversation.transcript import JsonlMessageStore
from tests.conversation.journal.projector_test_support import (
    _NOW,
    _conversation_record,
    _create_explicit_projection,
    _encoded,
)

if TYPE_CHECKING:
    from pathlib import Path


def _batch(seq: int, *, text: str | None = None) -> tuple[Any, Any]:
    """构造一个指定 Journal seq 的 conversation batch。"""
    item = user_message(text=text or str(seq), thread_id="thr_explicit").model_copy(
        update={"id": f"item_{seq}", "created_at": _NOW}
    )
    return _encoded(
        (_conversation_record(item, record_id=f"rec_{seq}"),),
        expected_seq=seq - 1,
    )


async def _seed(store: JsonlMessageStore, sequences: tuple[int, ...]) -> None:
    """创建 audited projection 并物化指定 conversation coverage。"""
    await _create_explicit_projection(store)
    projector = JournalConversationProjector(store)
    for seq in sequences:
        result = await projector.project(*_batch(seq))
        assert result.stale is False


@pytest.mark.anyio
@pytest.mark.parametrize("replay_handle", ["same", "other"])
async def test_generation_replay_rejects_missing_expected_middle_item(
    tmp_path: Path,
    replay_handle: str,
) -> None:
    """有旧 healthy coverage 时，seq4 后直接 seq6 不得伪造完整恢复。"""
    first_store = JsonlMessageStore(tmp_path)
    await _seed(first_store, (4, 5, 6))
    other_store = JsonlMessageStore(tmp_path) if replay_handle == "other" else None
    replay_store = other_store or first_store
    projector = JournalConversationProjector(replay_store)
    (tmp_path / "thr_explicit.jsonl").unlink()

    first = await projector.project(*_batch(4))
    skipped = await projector.project(*_batch(6))

    history = [item async for item in await replay_store.load_thread("thr_explicit")]
    window = replay_store.projection_replay_window("thr_explicit")
    assert first.projected_seq == skipped.projected_seq == 4
    assert skipped.stale is True
    assert skipped.failure_class == "generation_replay_mismatch"
    assert [item.id for item in history] == ["item_4"]
    assert window is not None and window.progress == 4
    if other_store is not None:
        await other_store.close()
    await first_store.close()


@pytest.mark.anyio
async def test_generation_replay_rejects_changed_expected_content(tmp_path: Path) -> None:
    """expected id 相同但内容变化时不得写入或推进 replay window。"""
    store = JsonlMessageStore(tmp_path)
    await _seed(store, (4, 5, 6))
    projector = JournalConversationProjector(store)
    (tmp_path / "thr_explicit.jsonl").unlink()
    await projector.project(*_batch(4))

    changed = await projector.project(*_batch(5, text="changed"))

    history = [item async for item in await store.load_thread("thr_explicit")]
    window = store.projection_replay_window("thr_explicit")
    assert changed.stale is True
    assert changed.failure_class == "generation_replay_mismatch"
    assert [item.id for item in history] == ["item_4"]
    assert window is not None and window.progress == 4
    await store.close()


@pytest.mark.anyio
async def test_generation_replay_preserves_legal_domain_sequence_gap(tmp_path: Path) -> None:
    """旧 healthy coverage 本就只有 seq4/6 时，合法 domain gap 仍可完整恢复。"""
    store = JsonlMessageStore(tmp_path)
    await _seed(store, (4, 6))
    projector = JournalConversationProjector(store)
    (tmp_path / "thr_explicit.jsonl").unlink()

    first = await projector.project(*_batch(4))
    completed = await projector.project(*_batch(6))

    history = [item async for item in await store.load_thread("thr_explicit")]
    assert first.projected_seq == 4
    assert completed == ProjectionResult(
        thread_id="thr_explicit",
        projected_seq=6,
        stale=False,
    )
    assert [item.id for item in history] == ["item_4", "item_6"]
    assert store.projection_replay_window("thr_explicit") is None
    await store.close()


@pytest.mark.anyio
async def test_restart_without_old_snapshot_accepts_complete_journal_replay(tmp_path: Path) -> None:
    """最后 handle 关闭后无旧 coverage，只依赖 caller 顺序遍历完整 Journal。"""
    store = JsonlMessageStore(tmp_path)
    await _seed(store, (4, 5, 6))
    await store.close()
    (tmp_path / "thr_explicit.jsonl").unlink()
    reopened = JsonlMessageStore(tmp_path)
    projector = JournalConversationProjector(reopened)

    results = [await projector.project(*_batch(seq)) for seq in (4, 5, 6)]

    history = [item async for item in await reopened.load_thread("thr_explicit")]
    assert all(not result.stale for result in results)
    assert results[-1].projected_seq == 6
    assert [item.id for item in history] == ["item_4", "item_5", "item_6"]
    await reopened.close()


@pytest.mark.anyio
async def test_reset_with_nonhealthy_old_snapshot_fails_closed(tmp_path: Path) -> None:
    """旧 snapshot 对应 stale state 时不得猜测 expected coverage。"""
    store = JsonlMessageStore(tmp_path)
    await _seed(store, (4, 5, 6))
    store.update_projection_state(
        "thr_explicit",
        ProjectionResult("thr_explicit", 6, stale=True, failure_class="injected"),
        None,
    )
    (tmp_path / "thr_explicit.jsonl").unlink()

    result = await JournalConversationProjector(store).project(*_batch(4))

    history = [item async for item in await store.load_thread("thr_explicit")]
    assert result.stale is True
    assert history == []
    assert store.projection_replay_window("thr_explicit") is None
    await store.close()
