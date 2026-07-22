"""SessionJournal 原子 BEGIN/envelope/COMMIT frame codec 测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from taifeng.conversation.journal.errors import JournalIntegrityError
from taifeng.conversation.journal.framing import (
    EncodedBatch,
    decode_committed_lines,
    encode_batch,
)
from taifeng.conversation.journal.models import ActorRef, JournalHealth, JournalRecord

_ZERO_HASH = "0" * 64
_NOW = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)


def _records(count: int) -> tuple[JournalRecord, ...]:
    """构造固定、连续的测试 records。"""
    return tuple(
        JournalRecord(
            session_id="ses_1",
            record_id=f"rec_{index}",
            operation_id="op_1",
            record_type="probe",
            thread_id="thr_root",
            actor=ActorRef(kind="system", source="test"),
            payload={"index": index},
        )
        for index in range(count)
    )


def _encoded(count: int = 3) -> EncodedBatch:
    """编码固定测试 batch。"""
    return encode_batch(
        _records(count),
        batch_id="batch_1",
        expected_seq=7,
        writer_epoch=2,
        previous_hash="a" * 64,
        recorded_at=_NOW,
    )


def test_batch_frames_do_not_consume_record_sequence() -> None:
    """BEGIN/COMMIT 只声明原子可见性，不能进入 record seq。"""
    encoded = _encoded()

    assert [envelope.seq for envelope in encoded.envelopes] == [8, 9, 10]
    assert encoded.begin.expected_seq == 7
    assert encoded.commit.first_seq == 8
    assert encoded.commit.last_seq == 10
    assert len(encoded.lines) == 5


def test_envelopes_form_hash_chain_from_supplied_tail() -> None:
    """第一个 envelope 接调用方 tail，后续逐条接前一 record hash。"""
    encoded = _encoded()

    assert encoded.envelopes[0].previous_hash == "a" * 64
    assert encoded.envelopes[1].previous_hash == encoded.envelopes[0].record_hash
    assert encoded.envelopes[2].previous_hash == encoded.envelopes[1].record_hash
    assert encoded.commit.tail_hash == encoded.envelopes[-1].record_hash


def test_valid_batch_decodes_atomically() -> None:
    """完整匹配 COMMIT 后才发布整个 batch。"""
    encoded = _encoded()
    decoded = decode_committed_lines(
        encoded.lines,
        session_id="ses_1",
        initial_seq=7,
        initial_hash="a" * 64,
    )

    assert decoded.envelopes == encoded.envelopes
    assert decoded.verification.health is JournalHealth.HEALTHY
    assert decoded.verification.committed_tail_seq == 10
    assert decoded.verification.committed_tail_hash == encoded.commit.tail_hash
    assert decoded.batches[0].ack == encoded.ack


@pytest.mark.parametrize("cut", [1, 2, 4])
def test_partial_batch_is_invisible(cut: int) -> None:
    """BEGIN 后任意位置 EOF 都不得暴露部分 envelope。"""
    encoded = _encoded()
    decoded = decode_committed_lines(
        encoded.lines[:cut],
        session_id="ses_1",
        initial_seq=7,
        initial_hash="a" * 64,
    )

    assert decoded.envelopes == ()
    assert decoded.batches == ()
    assert decoded.verification.health is JournalHealth.RECOVERY_REQUIRED
    assert decoded.verification.committed_tail_seq == 7
    assert decoded.verification.committed_tail_hash == "a" * 64
    assert decoded.verification.pending_batch_id == "batch_1"


def test_commit_tail_hash_mismatch_is_rejected() -> None:
    """COMMIT 汇总与 envelopes 不一致属于 committed-region 完整性错误。"""
    encoded = _encoded()
    lines = list(encoded.lines)
    commit = json.loads(lines[-1])
    commit["tail_hash"] = _ZERO_HASH
    lines[-1] = json.dumps(commit, separators=(",", ":")).encode() + b"\n"

    with pytest.raises(JournalIntegrityError, match="tail_hash"):
        decode_committed_lines(
            lines,
            session_id="ses_1",
            initial_seq=7,
            initial_hash="a" * 64,
        )


def test_envelope_payload_tampering_is_rejected() -> None:
    """envelope payload 被修改但 hash 未重算时必须 fail closed。"""
    encoded = _encoded()
    lines = list(encoded.lines)
    envelope = json.loads(lines[1])
    envelope["payload"] = {"index": 999}
    lines[1] = json.dumps(envelope, separators=(",", ":")).encode() + b"\n"

    with pytest.raises(JournalIntegrityError, match="payload_hash"):
        decode_committed_lines(
            lines,
            session_id="ses_1",
            initial_seq=7,
            initial_hash="a" * 64,
        )


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [
        (b'"seq":"8"', "invalid journal schema"),
        (b'"seq":8,"seq":8', "non-canonical JSON"),
    ],
)
def test_noncanonical_or_duplicate_physical_json_is_rejected(
    replacement: bytes,
    reason: str,
) -> None:
    """物理行不得借助类型强转或重复键伪装成原 canonical envelope。"""
    encoded = _encoded(count=1)
    lines = list(encoded.lines)
    lines[1] = lines[1].replace(b'"seq":8', replacement, 1)

    with pytest.raises(JournalIntegrityError, match=reason):
        decode_committed_lines(
            lines,
            session_id="ses_1",
            initial_seq=7,
            initial_hash="a" * 64,
        )
