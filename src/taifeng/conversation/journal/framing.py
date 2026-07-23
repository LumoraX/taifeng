"""SessionJournal BEGIN/envelope/COMMIT 原子 batch codec。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, ValidationError

from taifeng.conversation.journal.canonical import (
    canonical_bytes,
    canonical_hash,
    model_canonical_data,
    payload_hash,
    record_fingerprint,
)
from taifeng.conversation.journal.errors import (
    JournalIntegrityError,
    NonCanonicalValueError,
)
from taifeng.conversation.journal.models import (
    Durability,
    JournalAck,
    JournalEnvelope,
    JournalHealth,
    JournalModel,
    JournalRecord,
    JournalVerification,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

_ZERO_HASH = "0" * 64
HashHex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BatchBegin(JournalModel):
    """声明一组 envelopes 的原子可见性起点。"""

    frame_kind: Literal["BEGIN"] = Field(default="BEGIN", alias="__journal_frame__")
    frame_version: int = 1
    batch_id: Annotated[str, Field(min_length=1)]
    record_count: Annotated[int, Field(ge=1)]
    expected_seq: Annotated[int, Field(ge=0)]
    batch_payload_hash: HashHex


class BatchCommit(JournalModel):
    """证明 batch 中 envelopes 已完整写入的尾 frame。"""

    frame_kind: Literal["COMMIT"] = Field(default="COMMIT", alias="__journal_frame__")
    frame_version: int = 1
    batch_id: Annotated[str, Field(min_length=1)]
    record_count: Annotated[int, Field(ge=1)]
    first_seq: Annotated[int, Field(ge=1)]
    last_seq: Annotated[int, Field(ge=1)]
    records_hash: HashHex
    tail_hash: HashHex


@dataclass(frozen=True)
class EncodedBatch:
    """已在内存完成 hash 的 batch 与可直接写文件的 canonical lines。"""

    begin: BatchBegin
    envelopes: tuple[JournalEnvelope, ...]
    commit: BatchCommit
    ack: JournalAck
    lines: tuple[bytes, ...]
    fingerprints: tuple[str, ...]


@dataclass(frozen=True)
class CommittedBatch:
    """decoder 验证通过的一组 committed envelopes。"""

    batch_id: str
    envelopes: tuple[JournalEnvelope, ...]
    fingerprints: tuple[str, ...]
    ack: JournalAck


@dataclass(frozen=True)
class DecodedJournal:
    """一段物理 lines 的 committed 视图与 strict verification。"""

    envelopes: tuple[JournalEnvelope, ...]
    batches: tuple[CommittedBatch, ...]
    verification: JournalVerification


def _envelope_for_record(
    record: JournalRecord,
    *,
    seq: int,
    writer_epoch: int,
    previous_hash: str,
    recorded_at: datetime,
) -> JournalEnvelope:
    """为调用方 record 分配 writer 字段并计算 envelope hash。"""
    values = record.model_dump(mode="python")
    provisional = JournalEnvelope(
        **values,
        seq=seq,
        writer_epoch=writer_epoch,
        recorded_at=recorded_at,
        previous_hash=previous_hash,
        payload_hash=payload_hash(record.payload),
        record_hash=_ZERO_HASH,
    )
    hash_values = model_canonical_data(provisional)
    del hash_values["record_hash"]
    return provisional.model_copy(update={"record_hash": canonical_hash(hash_values)})


def _frame_line(frame: BatchBegin | BatchCommit) -> bytes:
    """用 alias key 写出 frame canonical line。"""
    values = frame.model_dump(mode="python", by_alias=True)
    return canonical_bytes(values) + b"\n"


def _envelope_line(envelope: JournalEnvelope) -> bytes:
    """写出 envelope canonical line。"""
    return canonical_bytes(model_canonical_data(envelope)) + b"\n"


def encode_batch(
    records: tuple[JournalRecord, ...],
    *,
    batch_id: str,
    expected_seq: int,
    writer_epoch: int,
    previous_hash: str,
    recorded_at: datetime,
    record_fingerprints: tuple[str, ...] | None = None,
) -> EncodedBatch:
    """把非空 records 编码成一个原子 batch；frame 不占 record seq。"""
    if not records:
        raise ValueError("journal batch must contain at least one record")
    session_id = records[0].session_id
    if any(record.session_id != session_id for record in records):
        raise ValueError("all records in a batch must belong to one session")
    if len({record.record_id for record in records}) != len(records):
        raise ValueError("record ids must be unique within a new batch")

    fingerprints = (
        tuple(record_fingerprint(record) for record in records)
        if record_fingerprints is None
        else record_fingerprints
    )
    if len(fingerprints) != len(records):
        raise ValueError("record fingerprints must match record count")
    begin = BatchBegin(
        batch_id=batch_id,
        record_count=len(records),
        expected_seq=expected_seq,
        batch_payload_hash=canonical_hash(list(fingerprints)),
    )
    envelopes: list[JournalEnvelope] = []
    tail_hash = previous_hash
    for offset, record in enumerate(records, start=1):
        envelope = _envelope_for_record(
            record,
            seq=expected_seq + offset,
            writer_epoch=writer_epoch,
            previous_hash=tail_hash,
            recorded_at=recorded_at,
        )
        envelopes.append(envelope)
        tail_hash = envelope.record_hash
    envelope_tuple = tuple(envelopes)
    commit = BatchCommit(
        batch_id=batch_id,
        record_count=len(records),
        first_seq=envelope_tuple[0].seq,
        last_seq=envelope_tuple[-1].seq,
        records_hash=canonical_hash([item.record_hash for item in envelope_tuple]),
        tail_hash=tail_hash,
    )
    ack = JournalAck(
        session_id=session_id,
        first_seq=commit.first_seq,
        last_seq=commit.last_seq,
        record_ids=tuple(record.record_id for record in records),
        tail_hash=commit.tail_hash,
        writer_epoch=writer_epoch,
        durability=Durability.COMMITTED,
    )
    lines = (_frame_line(begin), *map(_envelope_line, envelope_tuple), _frame_line(commit))
    return EncodedBatch(begin, envelope_tuple, commit, ack, tuple(lines), fingerprints)


def _parse_line(raw: bytes | str, *, line_no: int) -> dict[str, object]:
    """解析一行 JSON object，并把底层异常收敛为 integrity error。"""
    try:
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        value = json.loads(raw)
    except (UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise JournalIntegrityError("invalid JSON", line_no=line_no) from exc
    if not isinstance(value, dict):
        raise JournalIntegrityError("line must contain a JSON object", line_no=line_no)
    try:
        canonical = canonical_bytes(value)
    except NonCanonicalValueError as exc:
        raise JournalIntegrityError("non-canonical JSON", line_no=line_no) from exc
    if raw_bytes.removesuffix(b"\n") != canonical:
        raise JournalIntegrityError("non-canonical JSON", line_no=line_no)
    return value


def _parse_model[ModelT: JournalModel](
    model_type: type[ModelT], value: dict[str, object], *, line_no: int
) -> ModelT:
    """校验 frame/envelope model，并保留物理行号。"""
    try:
        return model_type.model_validate_json(canonical_bytes(value), strict=True)
    except ValidationError as exc:
        raise JournalIntegrityError("invalid journal schema", line_no=line_no) from exc


def _record_from_envelope(envelope: JournalEnvelope) -> JournalRecord:
    """重建调用方 record，用于验证 batch payload fingerprint。"""
    values = envelope.model_dump(mode="python")
    for key in (
        "seq",
        "writer_epoch",
        "recorded_at",
        "previous_hash",
        "payload_hash",
        "record_hash",
    ):
        del values[key]
    return JournalRecord.model_validate(values)


def _validate_envelope(
    envelope: JournalEnvelope,
    *,
    session_id: str,
    expected_seq: int,
    expected_previous_hash: str,
    line_no: int,
) -> str:
    """验证单 envelope 的 session、seq、payload hash 与 record hash。"""
    if envelope.session_id != session_id:
        raise JournalIntegrityError("session_id mismatch", line_no=line_no)
    if envelope.seq != expected_seq:
        raise JournalIntegrityError("seq mismatch", line_no=line_no)
    if envelope.previous_hash != expected_previous_hash:
        raise JournalIntegrityError("previous_hash mismatch", line_no=line_no)
    if envelope.payload_hash != payload_hash(envelope.payload):
        raise JournalIntegrityError("payload_hash mismatch", line_no=line_no)
    values = model_canonical_data(envelope)
    del values["record_hash"]
    if envelope.record_hash != canonical_hash(values):
        raise JournalIntegrityError("record_hash mismatch", line_no=line_no)
    return record_fingerprint(_record_from_envelope(envelope))


def _validate_commit(
    begin: BatchBegin,
    envelopes: tuple[JournalEnvelope, ...],
    fingerprints: tuple[str, ...],
    commit: BatchCommit,
    *,
    line_no: int,
) -> None:
    """验证 BEGIN 声明、envelopes 和 COMMIT 汇总完全一致。"""
    if commit.batch_id != begin.batch_id or commit.record_count != begin.record_count:
        raise JournalIntegrityError("batch identity mismatch", line_no=line_no)
    if len(envelopes) != begin.record_count:
        raise JournalIntegrityError("record_count mismatch", line_no=line_no)
    if begin.batch_payload_hash != canonical_hash(list(fingerprints)):
        raise JournalIntegrityError("batch_payload_hash mismatch", line_no=line_no)
    if commit.first_seq != envelopes[0].seq or commit.last_seq != envelopes[-1].seq:
        raise JournalIntegrityError("commit seq range mismatch", line_no=line_no)
    if commit.records_hash != canonical_hash([item.record_hash for item in envelopes]):
        raise JournalIntegrityError("records_hash mismatch", line_no=line_no)
    if commit.tail_hash != envelopes[-1].record_hash:
        raise JournalIntegrityError("tail_hash mismatch", line_no=line_no)


def _committed_batch(
    begin: BatchBegin,
    envelopes: tuple[JournalEnvelope, ...],
    fingerprints: tuple[str, ...],
    commit: BatchCommit,
) -> CommittedBatch:
    """从已验证内容构造 batch ack/index。"""
    ack = JournalAck(
        session_id=envelopes[0].session_id,
        first_seq=commit.first_seq,
        last_seq=commit.last_seq,
        record_ids=tuple(item.record_id for item in envelopes),
        tail_hash=commit.tail_hash,
        writer_epoch=envelopes[0].writer_epoch,
        durability=Durability.COMMITTED,
    )
    return CommittedBatch(begin.batch_id, envelopes, fingerprints, ack)


def decode_committed_lines(
    lines: Sequence[bytes | str],
    *,
    session_id: str,
    initial_seq: int = 0,
    initial_hash: str = _ZERO_HASH,
) -> DecodedJournal:
    """strict decode 多个 batch；未闭合的最终 batch 保持完全不可见。"""
    committed: list[JournalEnvelope] = []
    batches: list[CommittedBatch] = []
    pending_begin: BatchBegin | None = None
    pending_envelopes: list[JournalEnvelope] = []
    pending_fingerprints: list[str] = []
    tail_seq = initial_seq
    tail_hash = initial_hash

    for line_no, raw in enumerate(lines, start=1):
        value = _parse_line(raw, line_no=line_no)
        frame_kind = value.get("__journal_frame__")
        if frame_kind == "BEGIN":
            if pending_begin is not None:
                raise JournalIntegrityError("nested BEGIN", line_no=line_no)
            pending_begin = _parse_model(BatchBegin, value, line_no=line_no)
            if pending_begin.expected_seq != tail_seq:
                raise JournalIntegrityError("BEGIN expected_seq mismatch", line_no=line_no)
            continue
        if frame_kind == "COMMIT":
            if pending_begin is None:
                raise JournalIntegrityError("COMMIT without BEGIN", line_no=line_no)
            commit = _parse_model(BatchCommit, value, line_no=line_no)
            envelope_tuple = tuple(pending_envelopes)
            fingerprint_tuple = tuple(pending_fingerprints)
            _validate_commit(
                pending_begin,
                envelope_tuple,
                fingerprint_tuple,
                commit,
                line_no=line_no,
            )
            batch = _committed_batch(pending_begin, envelope_tuple, fingerprint_tuple, commit)
            batches.append(batch)
            committed.extend(envelope_tuple)
            tail_seq = commit.last_seq
            tail_hash = commit.tail_hash
            pending_begin = None
            pending_envelopes = []
            pending_fingerprints = []
            continue
        if pending_begin is None:
            raise JournalIntegrityError("envelope outside batch", line_no=line_no)
        envelope = _parse_model(JournalEnvelope, value, line_no=line_no)
        expected_seq = tail_seq + len(pending_envelopes) + 1
        expected_hash = pending_envelopes[-1].record_hash if pending_envelopes else tail_hash
        fingerprint = _validate_envelope(
            envelope,
            session_id=session_id,
            expected_seq=expected_seq,
            expected_previous_hash=expected_hash,
            line_no=line_no,
        )
        pending_envelopes.append(envelope)
        pending_fingerprints.append(fingerprint)

    health = JournalHealth.RECOVERY_REQUIRED if pending_begin else JournalHealth.HEALTHY
    verification = JournalVerification(
        session_id=session_id,
        health=health,
        committed_tail_seq=tail_seq,
        committed_tail_hash=tail_hash,
        record_count=tail_seq,
        pending_batch_id=pending_begin.batch_id if pending_begin else None,
    )
    return DecodedJournal(tuple(committed), tuple(batches), verification)


__all__ = [
    "BatchBegin",
    "BatchCommit",
    "CommittedBatch",
    "DecodedJournal",
    "EncodedBatch",
    "decode_committed_lines",
    "encode_batch",
]
