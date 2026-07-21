"""SessionJournal Phase 1 核心 DTO 契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from taifeng.conversation.journal.models import (
    ActorRef,
    Durability,
    JournalAck,
    JournalEnvelope,
    JournalHealth,
    JournalRecord,
    JournalVerification,
    RootThreadDescriptor,
    SessionDescriptor,
    SessionLease,
    build_initialization_records,
)


def _descriptor() -> SessionDescriptor:
    """构造固定 Session 初始化描述符。"""
    return SessionDescriptor(
        session_id="ses_1",
        creation_operation_id="create_1",
        writer_id="worker_a",
        root_thread=RootThreadDescriptor(
            thread_id="thr_root",
            entry_skill_id="general",
            source="user",
            tags=("test",),
            extra={"cwd": "/work"},
        ),
        config={"model": "sim", "temperature": 0},
    )


def test_session_descriptor_requires_stable_creation_identity() -> None:
    """Session 描述符必须显式携带 creation operation 与 writer 身份。"""
    descriptor = _descriptor()

    assert descriptor.creation_operation_id == "create_1"
    assert descriptor.writer_id == "worker_a"
    assert descriptor.root_thread.thread_id == "thr_root"


def test_initialization_records_have_stable_order_and_links() -> None:
    """初始化三记录顺序、ID 与 causation 链必须稳定。"""
    records = build_initialization_records(_descriptor())

    assert [record.record_type for record in records] == [
        "session_started",
        "thread_created",
        "thread_bound",
    ]
    assert [record.record_id for record in records] == [
        "create_1:session_started",
        "create_1:thread_created",
        "create_1:thread_bound",
    ]
    assert records[0].causation_id is None
    assert records[1].causation_id == records[0].record_id
    assert records[2].causation_id == records[1].record_id
    assert all(record.session_id == "ses_1" for record in records)
    assert all(record.thread_id == "thr_root" for record in records)


def test_initialization_payloads_preserve_descriptor_values() -> None:
    """三个初始化 payload 必须覆盖执行所需的稳定 descriptor 值。"""
    session_started, thread_created, thread_bound = build_initialization_records(_descriptor())

    assert session_started.payload == {
        "config": {"model": "sim", "temperature": 0},
        "creation_operation_id": "create_1",
        "writer_id": "worker_a",
    }
    assert thread_created.payload == {
        "entry_skill_id": "general",
        "source": "user",
        "tags": ["test"],
        "extra": {"cwd": "/work"},
    }
    assert thread_bound.payload == {"session_id": "ses_1", "thread_id": "thr_root"}


def test_models_are_frozen_and_reject_extra_fields() -> None:
    """核心 DTO 不允许静默扩字段或原地改值。"""
    actor = ActorRef(kind="user", source="cli")

    with pytest.raises(ValidationError):
        ActorRef.model_validate({"kind": "user", "source": "cli", "unexpected": True})
    with pytest.raises(ValidationError):
        actor.kind = "system"  # type: ignore[misc]


def test_record_envelope_ack_and_verification_shapes() -> None:
    """存储层所需核心结果 DTO 必须能表达严格 tail 与 durability。"""
    actor = ActorRef(kind="system", source="taifeng")
    record = JournalRecord(
        session_id="ses_1",
        record_id="rec_1",
        operation_id="op_1",
        record_type="probe",
        thread_id="thr_root",
        actor=actor,
        payload={"ok": True},
    )
    envelope = JournalEnvelope(
        session_id="ses_1",
        seq=1,
        writer_epoch=1,
        record_id=record.record_id,
        operation_id=record.operation_id,
        recorded_at=datetime(2026, 7, 21, tzinfo=UTC),
        record_type=record.record_type,
        thread_id=record.thread_id,
        actor=actor,
        previous_hash="0" * 64,
        payload_hash="1" * 64,
        record_hash="2" * 64,
        payload=record.payload,
    )
    lease = SessionLease(
        session_id="ses_1", writer_id="worker_a", writer_epoch=1, lease_id="lease_1"
    )
    ack = JournalAck(
        session_id="ses_1",
        first_seq=1,
        last_seq=1,
        record_ids=("rec_1",),
        tail_hash=envelope.record_hash,
        writer_epoch=lease.writer_epoch,
        durability=Durability.COMMITTED,
    )
    verification = JournalVerification(
        session_id="ses_1",
        health=JournalHealth.HEALTHY,
        committed_tail_seq=1,
        committed_tail_hash=envelope.record_hash,
        record_count=1,
    )

    assert ack.first_seq == verification.committed_tail_seq
    assert ack.tail_hash == verification.committed_tail_hash
    assert envelope.previous_hash == "0" * 64
