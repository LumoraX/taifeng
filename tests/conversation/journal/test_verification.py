"""SessionJournal JSONL strict load / verify 失败语义测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.journal import (
    ActorRef,
    JournalHealth,
    JournalIntegrityError,
    JournalRecord,
    JournalRecoveryRequiredError,
    RootThreadDescriptor,
    SessionDescriptor,
)
from taifeng.conversation.journal.framing import encode_batch
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import SessionLease


def _descriptor() -> SessionDescriptor:
    """构造 strict verification 使用的 Session。"""
    return SessionDescriptor(
        session_id="ses_1",
        creation_operation_id="create_1",
        writer_id="worker_a",
        root_thread=RootThreadDescriptor(
            thread_id="thr_root",
            entry_skill_id="general",
        ),
        config={"model": "sim"},
    )


def _record(*, record_id: str = "rec_1", value: int = 1) -> JournalRecord:
    """构造可区分 fingerprint 的测试 record。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="test_record",
        actor=ActorRef(kind="user", source="test"),
        payload={"value": value},
    )


async def _created(
    tmp_path: Path,
) -> tuple[JsonlSessionJournalCore, SessionLease]:
    """创建初始化已 committed 的 live Journal。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor())
    return journal, created.lease


def _path(tmp_path: Path) -> Path:
    """返回固定测试 Session 的物理路径。"""
    return tmp_path / "ses_1.journal.jsonl"


def _append_bytes(path: Path, payload: bytes) -> None:
    """模拟进程中断或外部损坏产生的物理尾。"""
    with path.open("ab") as stream:
        stream.write(payload)


def _mutate_envelope(path: Path, field: str, value: object, *, index: int = 1) -> None:
    """修改一条已 committed envelope，同时保留前后物理内容。"""
    lines = path.read_bytes().splitlines(keepends=True)
    parsed = json.loads(lines[index])
    parsed[field] = value
    lines[index] = json.dumps(parsed, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(b"".join(lines))


@pytest.mark.anyio
async def test_clean_verification_reports_exact_committed_tail(tmp_path: Path) -> None:
    """健康文件返回精确 seq/hash/count，且无 torn/pending 状态。"""
    journal, _ = await _created(tmp_path)

    verification = await journal.verify("ses_1")

    assert verification.health is JournalHealth.HEALTHY
    assert verification.committed_tail_seq == 3
    assert verification.record_count == 3
    assert verification.committed_tail_hash != "0" * 64
    assert verification.physical_tail_torn is False
    assert verification.pending_batch_id is None


@pytest.mark.anyio
async def test_after_seq_loads_only_committed_suffix(tmp_path: Path) -> None:
    """load(after_seq) 只返回严格大于水位的 committed envelopes。"""
    journal, lease = await _created(tmp_path)
    await journal.append_batch(
        (_record(record_id="rec_1"), _record(record_id="rec_2")),
        lease=lease,
        expected_seq=3,
    )

    loaded = [item async for item in journal.load("ses_1", after_seq=3)]

    assert [item.seq for item in loaded] == [4, 5]


@pytest.mark.anyio
async def test_torn_final_line_requires_recovery_without_exposing_tail(
    tmp_path: Path,
) -> None:
    """无换行的最终物理行不可见，并保留最后 committed tail。"""
    journal, _ = await _created(tmp_path)
    _append_bytes(_path(tmp_path), b'{"__journal_frame__":"BEGIN"')

    verification = await journal.verify("ses_1")
    loaded = [item async for item in journal.load("ses_1")]

    assert verification.health is JournalHealth.RECOVERY_REQUIRED
    assert verification.physical_tail_torn is True
    assert verification.committed_tail_seq == 3
    assert [item.seq for item in loaded] == [1, 2, 3]


@pytest.mark.anyio
async def test_incomplete_final_batch_is_invisible(tmp_path: Path) -> None:
    """有完整行但缺 COMMIT 的 batch 整体不可见。"""
    journal, _ = await _created(tmp_path)
    healthy = await journal.verify("ses_1")
    encoded = encode_batch(
        (_record(),),
        batch_id="pending_1",
        expected_seq=3,
        writer_epoch=1,
        previous_hash=healthy.committed_tail_hash,
        recorded_at=datetime.now(UTC),
    )
    _append_bytes(_path(tmp_path), b"".join(encoded.lines[:-1]))

    verification = await journal.verify("ses_1")
    loaded = [item async for item in journal.load("ses_1")]

    assert verification.health is JournalHealth.RECOVERY_REQUIRED
    assert verification.pending_batch_id == "pending_1"
    assert verification.committed_tail_seq == 3
    assert [item.seq for item in loaded] == [1, 2, 3]


@pytest.mark.anyio
async def test_malformed_middle_line_is_integrity_error(tmp_path: Path) -> None:
    """坏行后仍有物理内容时不能伪装为可恢复 torn tail。"""
    journal, _ = await _created(tmp_path)
    path = _path(tmp_path)
    lines = path.read_bytes().splitlines(keepends=True)
    lines[1] = b"{not-json}\n"
    path.write_bytes(b"".join(lines))

    with pytest.raises(JournalIntegrityError):
        await journal.verify("ses_1")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "index"),
    [
        ("seq", 2, 1),
        ("payload_hash", "f" * 64, 1),
        ("previous_hash", "f" * 64, 2),
        ("record_hash", "f" * 64, 1),
    ],
)
async def test_committed_envelope_corruption_is_integrity_error(
    tmp_path: Path,
    field: str,
    value: object,
    index: int,
) -> None:
    """seq 与 hash chain 任一 committed 字段被改写都 fail closed。"""
    journal, _ = await _created(tmp_path)
    _mutate_envelope(_path(tmp_path), field, value, index=index)

    with pytest.raises(JournalIntegrityError):
        await journal.verify("ses_1")


@pytest.mark.anyio
async def test_conflicting_duplicate_record_id_is_integrity_error(tmp_path: Path) -> None:
    """物理文件中的相同 id/different fingerprint 不得成为第二条事实。"""
    journal, _ = await _created(tmp_path)
    healthy = await journal.verify("ses_1")
    duplicate = _record(record_id="create_1:session_started", value=99)
    encoded = encode_batch(
        (duplicate,),
        batch_id="duplicate_1",
        expected_seq=3,
        writer_epoch=1,
        previous_hash=healthy.committed_tail_hash,
        recorded_at=datetime.now(UTC),
    )
    _append_bytes(_path(tmp_path), b"".join(encoded.lines))

    with pytest.raises(JournalIntegrityError, match="conflicting duplicate record_id"):
        await journal.verify("ses_1")


@pytest.mark.anyio
async def test_append_refuses_recovery_required_physical_tail(tmp_path: Path) -> None:
    """发现 torn tail 后，live writer 也必须拒绝 ordinary append。"""
    journal, lease = await _created(tmp_path)
    _append_bytes(_path(tmp_path), b'{"__journal_frame__":"BEGIN"')

    with pytest.raises(JournalRecoveryRequiredError) as raised:
        await journal.append(_record(), lease=lease, expected_seq=3)

    assert raised.value.committed_tail_seq == 3

