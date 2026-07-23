"""Conversation projection 的 Journal Session identity 绑定回归测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from taifeng.conversation.journal.projector import (
    JournalConversationProjector,
    ProjectionOrderError,
)
from taifeng.conversation.models import user_message
from taifeng.conversation.transcript import JsonlMessageStore
from tests.conversation.journal.projector_test_support import (
    _NOW,
    _conversation_record,
    _encoded,
    _MemoryProjectionStore,
)

if TYPE_CHECKING:
    from pathlib import Path


def _session_projection_batch(
    session_id: str,
    *,
    thread_id: str = "thr_explicit",
) -> tuple[Any, Any]:
    """构造指定 Journal Session identity 的单 item 投影 batch。"""
    item = user_message(text="one", thread_id=thread_id).model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    record = _conversation_record(item, record_id="rec_1").model_copy(
        update={"session_id": session_id}
    )
    return _encoded((record,))


@pytest.mark.anyio
@pytest.mark.parametrize("projection_handle", ["same", "other", "restart"])
async def test_real_projection_rejects_journal_session_identity_mismatch_before_write(
    tmp_path: Path,
    projection_handle: str,
) -> None:
    """同 thread 不得接收另一个 Journal Session 的 durable batch。"""
    bootstrap_store = JsonlMessageStore(tmp_path)
    await JournalConversationProjector(bootstrap_store).bootstrap_thread(
        thread_id="thr_explicit",
        cwd="/work",
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_session_id": "ses_A",
            "journal_schema_version": 1,
        },
    )
    if projection_handle == "same":
        projection_store = bootstrap_store
    elif projection_handle == "other":
        projection_store = JsonlMessageStore(tmp_path)
    else:
        await bootstrap_store.close()
        projection_store = JsonlMessageStore(tmp_path)
    path = tmp_path / "thr_explicit.jsonl"
    before = path.read_bytes()
    envelopes, ack = _session_projection_batch("ses_B")

    with pytest.raises(ProjectionOrderError, match="Journal Session"):
        await JournalConversationProjector(projection_store).project(envelopes, ack)

    history = [item async for item in await projection_store.load_thread("thr_explicit")]
    assert path.read_bytes() == before
    assert history == []
    assert projection_store.projection_state("thr_explicit") == (None, None)
    if projection_store is not bootstrap_store:
        await projection_store.close()
    if projection_handle != "restart":
        await bootstrap_store.close()


@pytest.mark.anyio
async def test_matching_projection_session_identity_projects_normally(tmp_path: Path) -> None:
    """ack/envelope Session 与 audited metadata 一致时保持正常投影。"""
    store = JsonlMessageStore(tmp_path)
    await JournalConversationProjector(store).bootstrap_thread(
        thread_id="thr_explicit",
        cwd=None,
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_session_id": "ses_A",
            "journal_schema_version": 1,
        },
    )
    envelopes, ack = _session_projection_batch("ses_A")

    result = await JournalConversationProjector(store).project(envelopes, ack)

    history = [item async for item in await store.load_thread("thr_explicit")]
    assert result.stale is False
    assert [item.id for item in history] == ["item_1"]
    path = tmp_path / "thr_explicit.jsonl"
    before = path.read_bytes()
    before_state = store.projection_state("thr_explicit")
    mismatched = _session_projection_batch("ses_B")
    with pytest.raises(ProjectionOrderError, match="Journal Session"):
        await JournalConversationProjector(store).project(*mismatched)
    unchanged = [item async for item in await store.load_thread("thr_explicit")]
    assert path.read_bytes() == before
    assert unchanged == history
    assert store.projection_state("thr_explicit") == before_state
    await store.close()


@pytest.mark.anyio
async def test_missing_projection_session_metadata_fails_before_repair(
    tmp_path: Path,
) -> None:
    """directory 缺少 expected Session 时不得写 item 或修复缺失 JSONL。"""
    store = JsonlMessageStore(tmp_path)
    await store.create_projection_thread(
        thread_id="thr_explicit",
        cwd=None,
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_schema_version": 1,
        },
    )
    path = tmp_path / "thr_explicit.jsonl"
    path.unlink()
    envelopes, ack = _session_projection_batch("ses_A")

    with pytest.raises(ProjectionOrderError, match="Journal Session"):
        await JournalConversationProjector(store).project(envelopes, ack)

    assert not path.exists()
    assert store.projection_state("thr_explicit") == (None, None)
    await store.close()


@pytest.mark.anyio
async def test_fake_projection_store_uses_session_identity_protocol() -> None:
    """fake store 可显式配置 expected identity 并保持协议兼容。"""
    store = _MemoryProjectionStore()
    store.expected_session_ids["thr_explicit"] = "ses_A"
    matching = _session_projection_batch("ses_A")
    mismatched = _session_projection_batch("ses_B")
    projector = JournalConversationProjector(store)

    accepted = await projector.project(*matching)
    before = list(store.items["thr_explicit"])
    before_state = store.projection_state("thr_explicit")
    with pytest.raises(ProjectionOrderError, match="Journal Session"):
        await projector.project(*mismatched)

    assert accepted.stale is False
    assert store.items["thr_explicit"] == before
    assert store.projection_state("thr_explicit") == before_state
    assert store.append_calls == 1
