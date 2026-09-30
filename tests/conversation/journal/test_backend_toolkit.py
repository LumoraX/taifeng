"""Journal 后端构件（ADR 0114）：存储无关的纯函数，与 JSONL core 的行为逐字一致。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from taifeng.experimental import (
    SESSION_ENDED_RECORD_TYPE,
    ZERO_HASH,
    ActorRef,
    CommittedRecord,
    JournalConflictError,
    JournalIntegrityError,
    JournalRecord,
    JsonlSessionJournalCore,
    RootThreadDescriptor,
    SessionDescriptor,
    build_initialization_records,
    descriptor_fingerprint,
    ended_record_id,
    index_envelopes,
    record_fingerprint,
    resolve_idempotent_ack,
    seal_batch,
    snapshot_records,
    verify_envelopes,
)

if TYPE_CHECKING:
    from pathlib import Path

_SESSION = "ses_1"


def _descriptor(**overrides: str) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=_SESSION, creation_operation_id=overrides.get("operation_id", "create_1"),
        writer_id=overrides.get("writer_id", "writer_a"),
        root_thread=RootThreadDescriptor(thread_id="thr_root", entry_skill_id="general"),
        config={"model": "sim"},
    )


def _record(record_id: str, *, value: str | None = None, record_type: str = "x",
            session_id: str = _SESSION) -> JournalRecord:
    return JournalRecord(
        session_id=session_id, record_id=record_id, record_type=record_type,
        actor=ActorRef(kind="system", source="test"), payload={"value": value or record_id},
    )


async def test_sealed_envelopes_match_what_the_jsonl_core_writes(tmp_path: Path) -> None:
    """同样的输入，构件算出的 envelope 与参考实现落盘的逐字段相同。"""
    core = JsonlSessionJournalCore(tmp_path)
    created = await core.create_session(_descriptor())
    await core.append_batch((_record("r1"), _record("r2")), lease=created.lease, expected_seq=3)
    written = [envelope async for envelope in core.load(_SESSION)]
    await core.close()

    init = seal_batch(
        build_initialization_records(_descriptor()), previous_seq=0, previous_hash=ZERO_HASH,
        writer_epoch=1, recorded_at=written[0].recorded_at,
    )
    batch = seal_batch(
        (_record("r1"), _record("r2")), previous_seq=3, previous_hash=init.ack.tail_hash,
        writer_epoch=1, recorded_at=written[3].recorded_at,
    )
    assert [*init.envelopes, *batch.envelopes] == written
    assert init.ack == created.ack
    assert (batch.ack.first_seq, batch.ack.last_seq, batch.ack.tail_hash) == (
        4, 5, written[-1].record_hash)
    assert verify_envelopes(written, session_id=_SESSION).committed_tail_hash == batch.ack.tail_hash


def test_seal_batch_rejects_malformed_batches() -> None:
    kwargs = {"previous_seq": 0, "previous_hash": ZERO_HASH, "writer_epoch": 1}
    with pytest.raises(ValueError, match="at least one record"):
        seal_batch((), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="one session"):
        seal_batch((_record("a"), _record("b", session_id="other")), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique"):
        seal_batch((_record("a"), _record("a")), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fingerprints"):
        seal_batch((_record("a"),), fingerprints=("x", "y"), **kwargs)  # type: ignore[arg-type]


def test_sealed_batch_shares_one_timestamp_and_indexes_by_record_id() -> None:
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    sealed = seal_batch(
        (_record("a"), _record("b")), previous_seq=7, previous_hash="a" * 64, writer_epoch=3,
        recorded_at=stamp,
    )
    assert [e.seq for e in sealed.envelopes] == [8, 9]
    assert {e.recorded_at for e in sealed.envelopes} == {stamp}
    assert {e.writer_epoch for e in sealed.envelopes} == {3}
    assert sealed.envelopes[0].previous_hash == "a" * 64
    assert sealed.envelopes[1].previous_hash == sealed.envelopes[0].record_hash
    committed = sealed.committed()
    assert set(committed) == {"a", "b"}
    assert committed["a"] == CommittedRecord(record_fingerprint(_record("a")), sealed.ack)


def test_snapshot_records_detaches_from_the_callers_objects() -> None:
    original = _record("a")
    snapshots, fingerprints = snapshot_records([original])
    assert snapshots[0] == original and snapshots[0] is not original
    assert fingerprints == (record_fingerprint(original),)
    with pytest.raises(ValueError, match="at least one record"):
        snapshot_records([])
    with pytest.raises(ValueError, match="one session"):
        snapshot_records([_record("a"), _record("b", session_id="other")])


def test_idempotency_rules() -> None:
    single = seal_batch((_record("r1"),), previous_seq=3, previous_hash=ZERO_HASH, writer_epoch=1)
    pair = seal_batch(
        (_record("b1"), _record("b2")), previous_seq=4, previous_hash=single.ack.tail_hash,
        writer_epoch=1,
    )
    index = {**single.committed(), **pair.committed()}

    def resolve(*records: JournalRecord) -> object:
        return resolve_idempotent_ack(
            records, tuple(record_fingerprint(r) for r in records), index.get)

    assert resolve(_record("new")) is None
    assert resolve(_record("r1")) == single.ack
    assert resolve(_record("b1"), _record("b2")) == pair.ack
    with pytest.raises(JournalConflictError, match="record content conflict"):
        resolve(_record("r1", value="changed"))
    for batch in (
        (_record("b1"), _record("new")),          # 已提交的混着新的
        (_record("b1"),),                         # 原 batch 的子集
        (_record("b2"), _record("b1")),           # 换了顺序
        (_record("r1"), _record("b1")),           # 跨 batch 重组
        (_record("b1", value="changed"), _record("b2")),
    ):
        with pytest.raises(JournalConflictError, match="batch idempotency conflict"):
            resolve(*batch)


def _chain() -> list:
    init = seal_batch(
        build_initialization_records(_descriptor()), previous_seq=0, previous_hash=ZERO_HASH,
        writer_epoch=1,
    )
    more = seal_batch(
        (_record("r1"), _record("r2")), previous_seq=3, previous_hash=init.ack.tail_hash,
        writer_epoch=1,
    )
    return [*init.envelopes, *more.envelopes]


def test_verify_accepts_a_clean_chain_and_an_empty_one() -> None:
    verification = verify_envelopes(_chain(), session_id=_SESSION)
    assert (verification.committed_tail_seq, verification.record_count) == (5, 5)
    empty = verify_envelopes([], session_id=_SESSION)
    assert (empty.committed_tail_seq, empty.committed_tail_hash) == (0, ZERO_HASH)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: c.pop(1), "seq mismatch"),
        (lambda c: c.reverse(), "seq mismatch"),
        (lambda c: c.__setitem__(3, c[3].model_copy(update={"payload": {"value": "forged"}})),
         "payload_hash mismatch"),
        (lambda c: c.__setitem__(3, c[3].model_copy(update={"record_type": "forged"})),
         "record_hash mismatch"),
        (lambda c: c.__setitem__(3, c[3].model_copy(update={"previous_hash": "f" * 64})),
         "previous_hash mismatch"),
        (lambda c: c.__setitem__(0, c[0].model_copy(update={"session_id": "other"})),
         "session_id mismatch"),
    ],
)
def test_verify_rejects_tampering(mutate: object, reason: str) -> None:
    chain = _chain()
    mutate(chain)  # type: ignore[operator]
    with pytest.raises(JournalIntegrityError, match=reason):
        verify_envelopes(chain, session_id=_SESSION)


def test_verify_enforces_the_epoch_lineage() -> None:
    chain = _chain()
    # Session 必须从 epoch 1 开始
    high = seal_batch(
        build_initialization_records(_descriptor()), previous_seq=0, previous_hash=ZERO_HASH,
        writer_epoch=2,
    )
    with pytest.raises(JournalIntegrityError, match="must start at writer_epoch 1"):
        verify_envelopes(high.envelopes, session_id=_SESSION)
    # epoch 上升必须由一条 writer_takeover 开启
    jumped = seal_batch(
        (_record("r3"),), previous_seq=5, previous_hash=chain[-1].record_hash, writer_epoch=2)
    with pytest.raises(JournalIntegrityError, match="without writer_takeover"):
        verify_envelopes([*chain, *jumped.envelopes], session_id=_SESSION)


def test_index_is_rebuilt_from_envelopes_and_batch_acks() -> None:
    init = seal_batch(
        build_initialization_records(_descriptor()), previous_seq=0, previous_hash=ZERO_HASH,
        writer_epoch=1,
    )
    more = seal_batch(
        (_record("r1"), _record("r2")), previous_seq=3, previous_hash=init.ack.tail_hash,
        writer_epoch=1,
    )
    envelopes = [*init.envelopes, *more.envelopes]
    assert index_envelopes(envelopes, [init.ack, more.ack]) == {**init.committed(), **more.committed()}
    with pytest.raises(JournalIntegrityError, match="outside any committed batch"):
        index_envelopes(envelopes, [init.ack])


def test_ended_record_and_descriptor_fingerprint() -> None:
    chain = _chain()
    assert ended_record_id(chain) is None
    ended = seal_batch(
        (_record("end_1", record_type=SESSION_ENDED_RECORD_TYPE),), previous_seq=5,
        previous_hash=chain[-1].record_hash, writer_epoch=1,
    )
    assert ended_record_id([*chain, *ended.envelopes]) == "end_1"
    assert descriptor_fingerprint(_descriptor()) == descriptor_fingerprint(_descriptor())
    assert descriptor_fingerprint(_descriptor()) != descriptor_fingerprint(
        _descriptor(writer_id="writer_b"))
