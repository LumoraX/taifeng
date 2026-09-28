"""审计 resume 纯函数扫描：未结算 effect 判定与 root history 重建。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from taifeng.conversation.journal.framing import encode_batch
from taifeng.conversation.journal.models import ActorRef, JournalEnvelope, JournalRecord
from taifeng.loop.audit_resume_scan import (
    find_unsettled_effects,
    rebuild_root_history,
    root_thread_id,
)

_NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _record(
    record_id: str,
    record_type: str,
    payload: dict[str, object] | None = None,
    *,
    operation_id: str | None = None,
    thread_id: str | None = "thr_root",
) -> JournalRecord:
    """构造最小 record（扫描只读 type / id / operation / payload 引用）。"""
    return JournalRecord(
        session_id="ses",
        record_id=record_id,
        record_type=record_type,
        actor=ActorRef(kind="system", source="test"),
        payload=dict(payload or {}),
        operation_id=operation_id,
        thread_id=thread_id,
    )


def _envelopes(*records: JournalRecord) -> tuple[JournalEnvelope, ...]:
    """经真实 codec 编码为连续 envelopes。"""
    return encode_batch(
        records,
        batch_id="b",
        expected_seq=0,
        writer_epoch=1,
        previous_hash="0" * 64,
        recorded_at=_NOW,
    ).envelopes


def test_find_unsettled_effects_llm_request_without_checkpoint_is_listed() -> None:
    """无 checkpoint 的 LLM attempt 视为 UNKNOWN。"""
    envelopes = _envelopes(
        _record("req_1", "llm_request_committed"),
        _record("req_2", "llm_request_committed"),
        _record("cp_2", "llm_response_checkpoint", {"request_record_id": "req_2", "status": "complete"}),
    )

    assert find_unsettled_effects(envelopes) == ("req_1",)


def test_find_unsettled_effects_unknown_outcome_is_listed() -> None:
    """durable 为 unknown 的终态同样需要人工对账。"""
    envelopes = _envelopes(
        _record("intent", "tool_intent_committed"),
        _record("outcome", "tool_outcome_committed", {"intent_record_id": "intent", "status": "unknown"}),
    )

    assert find_unsettled_effects(envelopes) == ("outcome",)


def test_find_unsettled_effects_skill_and_submission_pairs() -> None:
    """skill 按 operation 配对 finished；submission 按 accepted_record_id 配对 applied。"""
    envelopes = _envelopes(
        _record("sel_a", "skill_selected", operation_id="op_a"),
        _record("fin_a", "skill_dispatch_finished", {"status": "success"}, operation_id="op_a"),
        _record("sel_b", "skill_selected", operation_id="op_b"),
        _record("acc_1", "submission_accepted"),
        _record("app_1", "submission_applied", {"accepted_record_id": "acc_1"}),
        _record("acc_2", "submission_accepted"),
    )

    assert find_unsettled_effects(envelopes) == ("sel_b", "acc_2")


def test_find_unsettled_effects_settled_journal_returns_empty() -> None:
    """全部配对的 Journal 无待对账项。"""
    envelopes = _envelopes(
        _record("req", "llm_request_committed"),
        _record("cp", "llm_response_checkpoint", {"request_record_id": "req", "status": "error"}),
    )

    assert find_unsettled_effects(envelopes) == ()


def test_find_unsettled_effects_outcome_without_reference_raises() -> None:
    """终态缺引用字段是 Journal 契约违约，显式报错而非当作已结算。"""
    envelopes = _envelopes(_record("outcome", "tool_outcome_committed", {"status": "success"}))

    with pytest.raises(ValueError, match="intent_record_id"):
        find_unsettled_effects(envelopes)


def test_root_thread_id_without_initialization_batch_raises() -> None:
    """不以初始化三记录开头的 Journal 无法定位 root thread。"""
    with pytest.raises(ValueError):
        root_thread_id(_envelopes(_record("x", "conversation_item")))


def test_rebuild_root_history_empty_journal_has_zero_watermarks() -> None:
    """无对话项时 history 为空、水位为 0、turn index 从 0 开始。"""
    history = rebuild_root_history(_envelopes(_record("a", "session_started")), "thr_root")

    assert (history.items, history.first_seq, history.last_seq, history.next_turn_index) == (
        (), None, 0, 0,
    )
