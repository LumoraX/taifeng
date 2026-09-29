"""tool_recovery_committed payload 契约与 core 冷读（ADR 0070）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from taifeng.conversation.journal import (
    TOOL_RECOVERY_RECORD_TYPE,
    ActorRef,
    JournalHealth,
    JournalIdentities,
    JournalRecordFactory,
    RootThreadDescriptor,
    SessionDescriptor,
    ToolRecoveryCommittedV1,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore

if TYPE_CHECKING:
    from pathlib import Path


def _payload(**overrides: Any) -> dict[str, Any]:
    """悬空 intent 经回查确认已完成的合法 payload。"""
    payload: dict[str, Any] = {
        "intent_record_id": "intent-1",
        "call_id": "call-1",
        "name": "remote_write",
        "effect_kind": "reconcilable",
        "basis": "reconcile",
        "verdict": "completed",
        "reconcile_status": "completed",
        "output": "done",
        "is_error": False,
        "recovery_operation_id": "ses:resume:ab",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"basis": "effect_kind", "verdict": "retry_safe", "reconcile_status": None},
        {"basis": "operator", "verdict": "provided", "reconcile_status": "unknown"},
        {"basis": "operator", "verdict": "aborted", "reconcile_status": None},
        {"verdict": "not_executed", "reconcile_status": "not_executed",
         "outcome_record_id": "outcome-1", "output": None, "is_error": None},
        {"basis": "operator", "verdict": "aborted", "reconcile_status": "completed",
         "outcome_record_id": "outcome-1", "output": None, "is_error": None},
    ],
)
def test_tool_recovery_payload_accepts_valid_shapes(overrides: dict[str, Any]) -> None:
    """依据 / 结论合法组合与「是否补写结果」一致时接受。"""
    parsed = ToolRecoveryCommittedV1.model_validate(_payload(**overrides))

    assert parsed.payload_version == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"basis": "effect_kind"}, "not allowed"),
        ({"basis": "operator", "verdict": "retry_safe"}, "not allowed"),
        ({"reconcile_status": "unknown"}, "reconcile_status"),
        ({"is_error": None}, "both present"),
        ({"output": None, "is_error": None}, "dangling intent"),
        ({"outcome_record_id": "outcome-1"}, "second output"),
        ({"outcome_record_id": "outcome-1", "output": None, "is_error": None},
         "cannot settle"),
        ({"payload_version": 2}, "payload_version"),
        ({"unexpected": 1}, "unexpected"),
    ],
)
def test_tool_recovery_payload_rejects_invalid_shapes(
    overrides: dict[str, Any], message: str,
) -> None:
    """错配 / 缺字段 / 未知键 / 未知版本一律拒绝（新记录严格校验，冷读 fail closed）。"""
    with pytest.raises(ValidationError, match=message):
        ToolRecoveryCommittedV1.model_validate(_payload(**overrides))


@pytest.mark.anyio
async def test_core_verify_accepts_recovery_record_and_cold_reads_it(tmp_path: Path) -> None:
    """core strict verify 接受恢复记录；另一 core 实例冷读后 payload 逐字段一致。"""
    core = JsonlSessionJournalCore(tmp_path)
    created = await core.create_session(SessionDescriptor(
        session_id="ses",
        creation_operation_id="ses:create",
        writer_id="w",
        root_thread=RootThreadDescriptor(thread_id="thr", entry_skill_id="entry"),
        config={},
    ))
    identities = JournalIdentities("ses", "thr", "sub")
    factory = JournalRecordFactory(
        session_id="ses",
        actor=ActorRef(kind="operator", source="recovery", principal_id="op-1"),
        identities=identities,
    )
    operation_id = identities.tool(identities.turn(0), "call-1")
    record = factory.build(
        operation_id=operation_id,
        record_type=TOOL_RECOVERY_RECORD_TYPE,
        payload=ToolRecoveryCommittedV1.model_validate(_payload(
            basis="operator", verdict="provided", reconcile_status=None,
        )),
        thread_id="thr",
    )
    await core.append(record, lease=created.lease, expected_seq=created.ack.last_seq)
    await core.close()

    reader = JsonlSessionJournalCore(tmp_path)
    verification = await reader.verify("ses")
    envelopes = [envelope async for envelope in reader.load("ses")]

    assert verification.health is JournalHealth.HEALTHY
    assert envelopes[-1].record_id == f"{operation_id}:tool_recovery_committed:none:0"
    assert ToolRecoveryCommittedV1.model_validate(envelopes[-1].payload).verdict == "provided"
