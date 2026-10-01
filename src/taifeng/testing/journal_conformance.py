"""SessionJournal core 一致性检查（ADR 0114）。

``journal_core_cases()`` 检查 ``SessionJournalCore`` 的行为——创建、追加的 CAS 与幂等、lease、接管、
读取。换 core 的实现直接跑；换存储的实现把自己的适配器装进 ``JsonlSessionJournalCore`` 后跑同一套
（适配器各方法的直接检查见 ``journal_adapter_conformance``）。

用法（pytest）::

    from taifeng.testing import journal_core_cases

    class _Harness:
        def __init__(self, root): self._root = root
        async def new_core(self): return MyJournalCore(self._root)
        async def abandon(self, core): await core.close()

    @pytest.mark.parametrize("case", journal_core_cases(), ids=lambda case: case.name)
    async def test_journal_core(case, tmp_path):
        await case.run(_Harness(tmp_path))

每个 case 需要一份**全新的、互不相干的存储**（一个 case 一个 harness）。harness 的 ``new_core`` 每次
返回接在同一份存储上的新实例——检查用两个实例模拟两个进程。

检查只验证协议层的行为，不碰物理格式：通过这些检查的后端可以注入 ``AuditConfig.journal_core``。
物理损坏的处置（torn tail、结果未知的提交）取决于存储介质，不在通用检查范围内，由各后端自测。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.conversation.journal.backend import ZERO_HASH, verify_envelopes
from taifeng.conversation.journal.errors import (
    JournalAlreadyExistsError,
    JournalBusyError,
    JournalConflictError,
    JournalLeaseError,
    JournalSessionEndedError,
    JournalSessionNotFoundError,
)
from taifeng.conversation.journal.models import (
    SESSION_ENDED_RECORD_TYPE,
    WRITER_TAKEOVER_RECORD_TYPE,
    ActorRef,
    Durability,
    JournalRecord,
    RootThreadDescriptor,
    SessionDescriptor,
    WriterTakeoverV1,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from taifeng.conversation.journal.backend import SessionJournalCore
    from taifeng.conversation.journal.models import JournalEnvelope, SessionCreateResult


class ConformanceFailure(AssertionError):  # noqa: N818  # 断言语义：失败即一致性不满足
    """后端的行为与协议约定不符。"""


class JournalCoreHarness(Protocol):
    """被测后端的接入点。一个 harness 对应一份独立的存储。"""

    async def new_core(self) -> SessionJournalCore:
        """返回接在这份存储上的一个新 core 实例（相当于另一个进程）。"""
        ...

    async def abandon(self, core: SessionJournalCore) -> None:
        """模拟持有 ``core`` 的进程消失：它占着的 writer 资格全部释放，不写任何记录。"""
        ...


@dataclass(frozen=True, slots=True)
class ConformanceCase:
    """一项检查：``run(harness)`` 通过即返回，失败抛 ``ConformanceFailure``。"""

    name: str
    description: str
    run: Callable[[Any], Awaitable[None]]


# ---------------------------------------------------------------------------
# 公共构件
# ---------------------------------------------------------------------------

_SESSION = "ses_conformance"
_ROOT_THREAD = "thr_root"


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ConformanceFailure(message)


def _descriptor(
    session_id: str = _SESSION, *, operation_id: str = "create_1", writer_id: str = "writer_a",
) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        creation_operation_id=operation_id,
        writer_id=writer_id,
        root_thread=RootThreadDescriptor(thread_id=_ROOT_THREAD, entry_skill_id="general"),
        config={"model": "sim"},
    )


def _record(
    record_id: str, *, session_id: str = _SESSION, record_type: str = "conformance_record",
    value: str | None = None,
) -> JournalRecord:
    return JournalRecord(
        session_id=session_id,
        record_id=record_id,
        record_type=record_type,
        actor=ActorRef(kind="system", source="conformance"),
        payload={"value": record_id if value is None else value},
    )


async def _loaded(
    core: SessionJournalCore, session_id: str = _SESSION, *, after_seq: int = 0,
) -> list[JournalEnvelope]:
    return [envelope async for envelope in core.load(session_id, after_seq=after_seq)]


async def _expect(
    errors: type[BaseException] | tuple[type[BaseException], ...],
    action: Awaitable[Any],
    what: str,
) -> BaseException:
    """``action`` 必须抛出 ``errors`` 之一；不抛或抛别的都是不一致。"""
    expected = errors if isinstance(errors, tuple) else (errors,)
    names = " / ".join(error.__name__ for error in expected)
    try:
        await action
    except expected as raised:
        return raised
    except ConformanceFailure:
        raise
    except Exception as raised:  # noqa: BLE001  # 报告实际抛出的类型
        raise ConformanceFailure(
            f"{what}: expected {names}, got {type(raised).__name__}: {raised}"
        ) from raised
    raise ConformanceFailure(f"{what}: expected {names}, but the call succeeded")


async def _created(harness: JournalCoreHarness) -> tuple[SessionJournalCore, SessionCreateResult]:
    core = await harness.new_core()
    return core, await core.create_session(_descriptor())


def _verified(envelopes: Sequence[JournalEnvelope], session_id: str = _SESSION) -> None:
    """整条链 strict 校验；把完整性错误翻译成一致性失败。"""
    try:
        verify_envelopes(envelopes, session_id=session_id)
    except Exception as raised:  # noqa: BLE001
        raise ConformanceFailure(f"committed chain does not verify: {raised}") from raised


# ---------------------------------------------------------------------------
# core：创建
# ---------------------------------------------------------------------------


async def _create_writes_initialization(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    _check(created.lease.session_id == _SESSION, "lease.session_id must be the created session")
    _check(created.lease.writer_id == "writer_a", "lease.writer_id must be the descriptor's writer")
    _check(created.lease.writer_epoch == 1, "a new session starts at writer_epoch 1")
    _check(bool(created.lease.lease_id), "lease_id must be non-empty")
    ack = created.ack
    _check((ack.first_seq, ack.last_seq) == (1, 3), "initialization occupies seq 1..3")
    _check(ack.record_ids == (
        "create_1:session_started", "create_1:thread_created", "create_1:thread_bound",
    ), "initialization record ids derive from creation_operation_id")
    _check(ack.writer_epoch == 1 and ack.durability is Durability.COMMITTED,
           "initialization ack must be committed at epoch 1")
    envelopes = await _loaded(core)
    _check([e.record_type for e in envelopes] == [
        "session_started", "thread_created", "thread_bound",
    ], "load must return the three initialization records in order")
    _check(envelopes[0].previous_hash == ZERO_HASH, "the first record links to the zero hash")
    _check(envelopes[-1].record_hash == ack.tail_hash, "ack.tail_hash is the last record's hash")
    _check(all(e.thread_id == _ROOT_THREAD for e in envelopes),
           "initialization records carry the root thread id")
    _verified(envelopes)


async def _create_is_idempotent(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    again = await core.create_session(_descriptor())
    _check(again == created, "the same live writer retrying the same create gets the same result")
    _check(len(await _loaded(core)) == 3, "a retried create must not write again")
    await _expect(
        JournalBusyError, core.create_session(_descriptor(operation_id="create_2")),
        "create with another operation id while the writer is live",
    )


async def _create_existing_is_refused(harness: JournalCoreHarness) -> None:
    core, _ = await _created(harness)
    other = await harness.new_core()
    await _expect(
        (JournalBusyError, JournalAlreadyExistsError), other.create_session(_descriptor()),
        "create from another instance while the writer is live",
    )
    await harness.abandon(core)
    await _expect(
        JournalAlreadyExistsError,
        other.create_session(_descriptor(operation_id="create_2", writer_id="writer_b")),
        "create over an existing session",
    )
    _check(len(await _loaded(other)) == 3, "a refused create must not write")


# ---------------------------------------------------------------------------
# core：追加
# ---------------------------------------------------------------------------


async def _append_links_the_chain(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    record = JournalRecord(
        session_id=_SESSION, record_id="r1", record_type="conformance_record",
        actor=ActorRef(kind="user", source="conformance", principal_id="u1"),
        payload={"text": "你好", "nested": {"n": [1, 2, 3]}},
        operation_id="op_1", attempt_id="att_1", occurred_at=stamp, submission_id="sub_1",
        thread_id=_ROOT_THREAD, turn_id="turn_1", parent_record_id="create_1:thread_bound",
        causation_id="create_1:thread_bound", correlation_id="corr_1",
    )
    ack = await core.append_batch((record,), lease=created.lease, expected_seq=3)
    _check((ack.first_seq, ack.last_seq, ack.record_ids) == (4, 4, ("r1",)),
           "a single append takes the next seq")
    _check(ack.writer_epoch == 1, "ack carries the writer epoch")
    envelopes = await _loaded(core)
    last = envelopes[-1]
    _check(last.seq == 4 and last.previous_hash == envelopes[2].record_hash,
           "the appended record links to the previous tail")
    _check(last.record_hash == ack.tail_hash, "ack.tail_hash is the appended record's hash")
    for name in JournalRecord.model_fields:
        _check(getattr(last, name) == getattr(record, name),
               f"the envelope must preserve the caller's {name}")
    _verified(envelopes)


async def _batch_is_atomic_and_ordered(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    batch = tuple(_record(f"b{i}") for i in range(1, 4))
    ack = await core.append_batch(batch, lease=created.lease, expected_seq=3)
    _check((ack.first_seq, ack.last_seq) == (4, 6), "one ack covers the whole batch")
    _check(ack.record_ids == ("b1", "b2", "b3"), "ack lists record ids in submission order")
    envelopes = await _loaded(core)
    _check([e.record_id for e in envelopes[3:]] == ["b1", "b2", "b3"],
           "records are stored in submission order")
    _check(len({e.recorded_at for e in envelopes[3:]}) == 1,
           "records of one batch share recorded_at")
    _verified(envelopes)


async def _stale_expected_seq_conflicts(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    for stale in (0, 2, 4):
        raised = await _expect(
            JournalConflictError,
            core.append_batch((_record("r1"),), lease=created.lease, expected_seq=stale),
            f"append with expected_seq={stale} while the tail is 3",
        )
        _check(getattr(raised, "actual_seq", None) == 3,
               "the conflict must report the actual tail seq")
    _check(len(await _loaded(core)) == 3, "a rejected append must not write")


async def _complete_retry_is_idempotent(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    single = await core.append_batch((_record("r1"),), lease=created.lease, expected_seq=3)
    batch = (_record("b1"), _record("b2"))
    committed = await core.append_batch(batch, lease=created.lease, expected_seq=4)
    # 调用方没收到 ack 而重试：expected_seq 已过时，仍须拿回原 ack
    _check(await core.append_batch((_record("r1"),), lease=created.lease, expected_seq=3) == single,
           "retrying a committed record returns its original ack")
    _check(await core.append_batch(batch, lease=created.lease, expected_seq=4) == committed,
           "retrying a committed batch returns its original ack")
    _check(len(await _loaded(core)) == 6, "retries must not write again")


async def _changed_content_conflicts(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    await core.append_batch((_record("r1"),), lease=created.lease, expected_seq=3)
    await _expect(
        JournalConflictError,
        core.append_batch((_record("r1", value="changed"),), lease=created.lease, expected_seq=4),
        "the same record id with different content",
    )
    _check(len(await _loaded(core)) == 4, "a conflicting append must not write")


async def _partial_overlap_conflicts(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    await core.append_batch((_record("b1"), _record("b2")), lease=created.lease, expected_seq=3)
    for label, batch in (
        ("a committed record mixed with a new one", (_record("b1"), _record("new"))),
        ("a subset of a committed batch", (_record("b1"),)),
        ("a committed batch in another order", (_record("b2"), _record("b1"))),
    ):
        await _expect(
            JournalConflictError,
            core.append_batch(batch, lease=created.lease, expected_seq=5), label,
        )
    _check(len(await _loaded(core)) == 5, "conflicting batches must not write")


async def _append_requires_the_live_lease(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    lease = created.lease
    for label, forged in (
        ("another lease id", lease.model_copy(update={"lease_id": "forged"})),
        ("another writer epoch", lease.model_copy(update={"writer_epoch": lease.writer_epoch + 1})),
        ("another writer id", lease.model_copy(update={"writer_id": "writer_x"})),
    ):
        await _expect(
            JournalLeaseError,
            core.append_batch((_record("r1"),), lease=forged, expected_seq=3),
            f"append with {label}",
        )
    other = await harness.new_core()
    await _expect(
        JournalLeaseError,
        other.append_batch((_record("r1"),), lease=lease, expected_seq=3),
        "append through an instance that does not hold the writer",
    )
    _check(len(await _loaded(core)) == 3, "appends without the live lease must not write")


async def _invalid_batches_are_rejected(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    await _expect(
        ValueError, core.append_batch((), lease=created.lease, expected_seq=3), "an empty batch",
    )
    await _expect(
        ValueError,
        core.append_batch(
            (_record("r1"), _record("r2", session_id="ses_other")),
            lease=created.lease, expected_seq=3,
        ),
        "a batch spanning two sessions",
    )
    _check(len(await _loaded(core)) == 3, "rejected batches must not write")


async def _concurrent_appends_commit_once(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    outcomes = await asyncio.gather(
        *(core.append_batch((_record(f"c{i}"),), lease=created.lease, expected_seq=3)
          for i in range(5)),
        return_exceptions=True,
    )
    winners = [o for o in outcomes if not isinstance(o, BaseException)]
    losers = [o for o in outcomes if isinstance(o, BaseException)]
    _check(len(winners) == 1, f"exactly one of five racing appends may win, got {len(winners)}")
    _check(all(isinstance(o, JournalConflictError) for o in losers),
           "the losers of a race must see JournalConflictError")
    envelopes = await _loaded(core)
    _check(len(envelopes) == 4, "only the winner's record is stored")
    _verified(envelopes)


# ---------------------------------------------------------------------------
# core：关闭与接管
# ---------------------------------------------------------------------------


async def _closed_session_rejects_appends(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    await _expect(
        JournalLeaseError,
        core.close_session(created.lease.model_copy(update={"lease_id": "forged"})),
        "close_session with a forged lease",
    )
    await core.close_session(created.lease)
    await _expect(
        JournalLeaseError,
        core.append_batch((_record("r1"),), lease=created.lease, expected_seq=3),
        "append after close_session",
    )
    await _expect(
        JournalLeaseError, core.close_session(created.lease), "closing the same lease twice",
    )
    _check(len(await _loaded(core)) == 3, "close_session must not write any record")
    # 释放之后别的实例可以接管
    other = await harness.new_core()
    opened = await other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1")
    _check(opened.lease.writer_epoch == 2, "taking over a released session raises the epoch")


async def _second_writer_is_refused(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    other = await harness.new_core()
    await _expect(
        JournalBusyError,
        other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1"),
        "open_existing while another instance holds the writer",
    )
    await _expect(
        JournalBusyError,
        core.open_existing(_SESSION, writer_id="writer_a", operation_id="open_1"),
        "open_existing on the instance that already holds the writer",
    )
    # 被拒的接管不留痕迹，原 writer 照常工作
    ack = await core.append_batch((_record("r1"),), lease=created.lease, expected_seq=3)
    _check(ack.last_seq == 4, "the live writer keeps working after a refused takeover")
    _check(len(await _loaded(core)) == 4, "a refused takeover must not write")


async def _unknown_session_cannot_be_opened(harness: JournalCoreHarness) -> None:
    core = await harness.new_core()
    await _expect(
        JournalSessionNotFoundError,
        core.open_existing("ses_missing", writer_id="writer_a", operation_id="open_1"),
        "open_existing on a session that was never created",
    )
    _check(await _loaded(core, "ses_missing") == [], "loading an unknown session yields nothing")
    for bad in ({"writer_id": "", "operation_id": "o"}, {"writer_id": "w", "operation_id": ""}):
        await _expect(
            ValueError, core.open_existing(_SESSION, **bad),
            "open_existing with an empty writer / operation id",
        )


async def _takeover_records_its_lineage(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    await core.append_batch((_record("r1"),), lease=created.lease, expected_seq=3)
    before = await _loaded(core)
    await harness.abandon(core)

    other = await harness.new_core()
    opened = await other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1")
    _check(opened.previous_epoch == 1, "previous_epoch is the epoch before the takeover")
    lease = opened.lease
    _check((lease.session_id, lease.writer_id, lease.writer_epoch) == (_SESSION, "writer_b", 2),
           "the new lease belongs to the new writer at epoch + 1")
    _check(lease.lease_id != created.lease.lease_id, "a takeover must mint a new lease id")
    _check((opened.ack.first_seq, opened.ack.last_seq) == (5, 5),
           "the takeover record is appended right after the previous tail")
    _check(opened.ack.record_ids == (f"open_1:{WRITER_TAKEOVER_RECORD_TYPE}",),
           "the takeover record id derives from the operation id")
    _check(opened.ack.writer_epoch == 2, "the takeover ack is at the new epoch")

    envelopes = await _loaded(other)
    _check(envelopes[:4] == before, "a takeover must not rewrite earlier records")
    takeover = envelopes[-1]
    _check(takeover.record_type == WRITER_TAKEOVER_RECORD_TYPE and takeover.writer_epoch == 2,
           "the takeover record carries the new epoch")
    payload = WriterTakeoverV1.model_validate(takeover.payload)
    _check(
        (payload.writer_id, payload.operation_id, payload.previous_epoch,
         payload.previous_tail_seq, payload.previous_tail_hash)
        == ("writer_b", "open_1", 1, 4, before[-1].record_hash),
        "the takeover payload must point at the tail it took over",
    )
    _verified(envelopes)


async def _takeover_is_idempotent(harness: JournalCoreHarness) -> None:
    core, _ = await _created(harness)
    await harness.abandon(core)
    other = await harness.new_core()
    opened = await other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1")
    again = await other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1")
    _check(again == opened, "the same live writer retrying the same open gets the same result")
    await _expect(
        JournalBusyError,
        other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_2"),
        "open_existing with another operation id while the writer is live",
    )
    _check(len(await _loaded(other)) == 4, "a retried open must not write again")


async def _takeover_retry_after_ack_loss(harness: JournalCoreHarness) -> None:
    core, _ = await _created(harness)
    await harness.abandon(core)
    first = await harness.new_core()
    opened = await first.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1")
    # 接管记录已落库，但进程在拿到结果前消失；重启后用同一个 operation id 重试
    await harness.abandon(first)
    second = await harness.new_core()
    retried = await second.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1")
    _check(retried.ack == opened.ack, "retrying a durable takeover reuses its record")
    _check(retried.lease.writer_epoch == 2 and retried.previous_epoch == 1,
           "the retried takeover keeps the epoch it already claimed")
    _check(len(await _loaded(second)) == 4, "retrying a durable takeover must not append")

    await second.append_batch((_record("r1"),), lease=retried.lease, expected_seq=4)
    await harness.abandon(second)
    third = await harness.new_core()
    await _expect(
        JournalConflictError,
        third.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1"),
        "reusing a takeover operation id after its epoch has been written to",
    )


async def _new_writer_appends_after_takeover(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    first = await core.append_batch((_record("r1"),), lease=created.lease, expected_seq=3)
    await harness.abandon(core)
    await _expect(
        JournalLeaseError,
        core.append_batch((_record("r2"),), lease=created.lease, expected_seq=4),
        "append by a writer that lost its session",
    )
    other = await harness.new_core()
    opened = await other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1")
    await _expect(
        JournalLeaseError,
        other.append_batch((_record("r2"),), lease=created.lease, expected_seq=5),
        "append with the pre-takeover lease",
    )
    ack = await other.append_batch((_record("r2"),), lease=opened.lease, expected_seq=5)
    _check((ack.last_seq, ack.writer_epoch) == (6, 2), "the new writer appends at the new epoch")
    # 接管前已提交的内容，由新 writer 重试仍然幂等
    _check(await other.append_batch((_record("r1"),), lease=opened.lease, expected_seq=3) == first,
           "records committed before the takeover still resolve idempotently")
    envelopes = await _loaded(other)
    _check([e.writer_epoch for e in envelopes] == [1, 1, 1, 1, 2, 2],
           "epochs change only at the takeover record")
    _verified(envelopes)


async def _ended_session_cannot_be_reopened(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    await core.append_batch(
        (_record("end_1", record_type=SESSION_ENDED_RECORD_TYPE),),
        lease=created.lease, expected_seq=3,
    )
    await harness.abandon(core)
    other = await harness.new_core()
    raised = await _expect(
        JournalSessionEndedError,
        other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_1"),
        "open_existing on a session that has a committed session_ended",
    )
    _check(getattr(raised, "ended_record_id", None) == "end_1",
           "the error names the session_ended record")
    _check(len(await _loaded(other)) == 4, "a refused reopen must not write")
    # 拒绝之后不占着 writer 资格：再来一次仍是同样的结论，而不是 Busy
    await _expect(
        JournalSessionEndedError,
        other.open_existing(_SESSION, writer_id="writer_b", operation_id="open_2"),
        "a second open_existing on an ended session",
    )


# ---------------------------------------------------------------------------
# core：读取与隔离
# ---------------------------------------------------------------------------


async def _load_honours_after_seq(harness: JournalCoreHarness) -> None:
    core, created = await _created(harness)
    await core.append_batch((_record("r1"), _record("r2")), lease=created.lease, expected_seq=3)
    _check([e.seq for e in await _loaded(core)] == [1, 2, 3, 4, 5], "load returns seq order")
    _check([e.seq for e in await _loaded(core, after_seq=3)] == [4, 5],
           "after_seq excludes records up to and including it")
    _check(await _loaded(core, after_seq=5) == [], "after_seq at the tail yields nothing")
    # 另一个实例（只读）看到同样的已提交内容
    reader = await harness.new_core()
    _check(await _loaded(reader) == await _loaded(core),
           "another instance reads the same committed records")


async def _sessions_are_independent(harness: JournalCoreHarness) -> None:
    core = await harness.new_core()
    one = await core.create_session(_descriptor("ses_one"))
    two = await core.create_session(_descriptor("ses_two", writer_id="writer_b"))
    await core.append_batch(
        (_record("r1", session_id="ses_one"),), lease=one.lease, expected_seq=3)
    await _expect(
        JournalLeaseError,
        core.append_batch((_record("r1", session_id="ses_two"),), lease=one.lease, expected_seq=3),
        "appending to one session with another session's lease",
    )
    # 同一个 record id 在两个 Session 里互不相干
    await core.append_batch(
        (_record("r1", session_id="ses_two", value="other"),), lease=two.lease, expected_seq=3)
    _check(len(await _loaded(core, "ses_one")) == 4 and len(await _loaded(core, "ses_two")) == 4,
           "each session keeps its own records")
    await core.close_session(one.lease)
    ack = await core.append_batch(
        (_record("r2", session_id="ses_two"),), lease=two.lease, expected_seq=4)
    _check(ack.last_seq == 5, "closing one session leaves the other writable")
    _verified(await _loaded(core, "ses_one"), "ses_one")
    _verified(await _loaded(core, "ses_two"), "ses_two")


def journal_core_cases() -> tuple[ConformanceCase, ...]:
    """``SessionJournalCore`` 的全部一致性检查。"""
    return (
        ConformanceCase("create_writes_initialization", "创建写下初始化三记录，seq 1–3、epoch 1", _create_writes_initialization),
        ConformanceCase("create_is_idempotent", "同一 live writer 重试同一次创建得到同样的结果", _create_is_idempotent),
        ConformanceCase("create_existing_is_refused", "已存在的 Session 不能再创建", _create_existing_is_refused),
        ConformanceCase("append_links_the_chain", "追加分配连续 seq、接上 hash chain、原样保留调用方字段", _append_links_the_chain),
        ConformanceCase("batch_is_atomic_and_ordered", "一批 record 原子落库、保持提交顺序", _batch_is_atomic_and_ordered),
        ConformanceCase("stale_expected_seq_conflicts", "expected_seq 过时即冲突，且不写入", _stale_expected_seq_conflicts),
        ConformanceCase("complete_retry_is_idempotent", "完整重试返回原 ack，不重复写入", _complete_retry_is_idempotent),
        ConformanceCase("changed_content_conflicts", "同 record id 内容不同即冲突", _changed_content_conflicts),
        ConformanceCase("partial_overlap_conflicts", "与已提交 batch 部分重叠 / 重组即冲突", _partial_overlap_conflicts),
        ConformanceCase("append_requires_the_live_lease", "追加必须持有完全匹配的 live lease", _append_requires_the_live_lease),
        ConformanceCase("invalid_batches_are_rejected", "空批次与跨 Session 批次在写入前被拒", _invalid_batches_are_rejected),
        ConformanceCase("concurrent_appends_commit_once", "同一 expected_seq 的并发追加恰好成功一个", _concurrent_appends_commit_once),
        ConformanceCase("closed_session_rejects_appends", "close_session 之后不能再追加，别的实例可以接管", _closed_session_rejects_appends),
        ConformanceCase("second_writer_is_refused", "writer 存活时第二个 writer 被拒且不留痕迹", _second_writer_is_refused),
        ConformanceCase("unknown_session_cannot_be_opened", "不存在的 Session 不能接管，读取为空", _unknown_session_cannot_be_opened),
        ConformanceCase("takeover_records_its_lineage", "接管 epoch + 1，并写下指向接管前尾部的记录", _takeover_records_its_lineage),
        ConformanceCase("takeover_is_idempotent", "同一 live writer 重试同一次接管得到同样的结果", _takeover_is_idempotent),
        ConformanceCase("takeover_retry_after_ack_loss", "接管已落库而结果丢失时，重试复用该记录；已被使用则冲突", _takeover_retry_after_ack_loss),
        ConformanceCase("new_writer_appends_after_takeover", "接管后新 writer 可写、旧 lease 失效、幂等索引延续", _new_writer_appends_after_takeover),
        ConformanceCase("ended_session_cannot_be_reopened", "已终结的 Session 不能再接管", _ended_session_cannot_be_reopened),
        ConformanceCase("load_honours_after_seq", "读取按 seq 升序并遵守 after_seq", _load_honours_after_seq),
        ConformanceCase("sessions_are_independent", "各 Session 的 lease、record id、生命周期互不相干", _sessions_are_independent),
    )


__all__ = [
    "ConformanceCase",
    "ConformanceFailure",
    "JournalCoreHarness",
    "journal_core_cases",
]
