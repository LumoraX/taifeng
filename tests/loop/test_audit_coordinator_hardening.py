"""SessionAuditCoordinator 取消、ack 与只读快照的防御性边界测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    CommitNotStartedError,
    Durability,
    JournalAck,
    JournalRecord,
    JournalRecoveryRequiredError,
    ProjectionResult,
    RootThreadDescriptor,
    SessionDescriptor,
    SessionLease,
)
from taifeng.conversation.journal.jsonl import (
    DefaultSyncFileAdapter,
    JsonlSessionJournalCore,
)
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditCoordinator,
    SessionAuditFrozenError,
)
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from pathlib import Path


_ZERO_HASH = "0" * 64


def _lease() -> SessionLease:
    """构造固定 coordinator lease。"""
    return SessionLease(
        session_id="ses_1",
        writer_id="writer_1",
        writer_epoch=1,
        lease_id="lease_1",
    )


def _record(record_id: str = "rec_1") -> JournalRecord:
    """构造固定 runtime-owned record。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="test_record",
        actor=ActorRef(kind="system", source="test"),
        payload={"record_id": record_id},
    )


def _ack(*, expected_seq: int = 3) -> JournalAck:
    """构造精确覆盖单条 record 的正常 ack。"""
    return JournalAck(
        session_id="ses_1",
        first_seq=expected_seq + 1,
        last_seq=expected_seq + 1,
        record_ids=("rec_1",),
        tail_hash=_ZERO_HASH,
        writer_epoch=1,
        durability=Durability.COMMITTED,
    )


def _coordinator(core: Any, *, expected_seq: int = 3) -> SessionAuditCoordinator:
    """构造绑定固定 Session 的 coordinator。"""
    return SessionAuditCoordinator(
        core=core,
        lease=_lease(),
        expected_seq=expected_seq,
    )


class _StaticCore:
    """返回或抛出一个预设 core outcome。"""

    def __init__(self, outcome: object) -> None:
        """保存 outcome 并初始化调用计数。"""
        self.outcome = outcome
        self.calls = 0
        self.mutated = False

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """标记已进入 core 后返回或抛出预设值。"""
        del records, lease, expected_seq
        self.calls += 1
        self.mutated = True
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return cast("JournalAck", self.outcome)


class _BlockingCore:
    """第一批暂停，用于验证第二批等待 append lock 时的取消边界。"""

    def __init__(self) -> None:
        """初始化同步事件与调用计数。"""
        self.entered = anyio.Event()
        self.release = anyio.Event()
        self.calls = 0

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """只允许首批进入 core，并等待测试释放。"""
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return JournalAck(
            session_id=lease.session_id,
            first_seq=expected_seq + 1,
            last_seq=expected_seq + len(records),
            record_ids=tuple(record.record_id for record in records),
            tail_hash=_ZERO_HASH,
            writer_epoch=lease.writer_epoch,
            durability=Durability.COMMITTED,
        )


class _PrewriteCancelThenAckCore:
    """遵循协议：raw cancel 仅表示 commit 未开始，后续调用可安全重试。"""

    def __init__(self) -> None:
        """初始化稳定 raw cancel 与调用计数。"""
        self.cancellation = asyncio.CancelledError("secret=prewrite")
        self.calls = 0

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """第一次在 mutation 前抛 raw cancel，第二次返回确定 ack。"""
        self.calls += 1
        if self.calls == 1:
            raise self.cancellation
        return JournalAck(
            session_id=lease.session_id,
            first_seq=expected_seq + 1,
            last_seq=expected_seq + len(records),
            record_ids=tuple(record.record_id for record in records),
            tail_hash=_ZERO_HASH,
            writer_epoch=lease.writer_epoch,
            durability=Durability.COMMITTED,
        )


@pytest.mark.anyio
async def test_cancel_while_waiting_append_lock_does_not_freeze_or_dispatch() -> None:
    """尚未调用 core 的 target cancel 只取消该操作，不冻结 Session。"""
    core = _BlockingCore()
    coordinator = _coordinator(core)
    target = coordinator.register_target("turn_2")
    second_cancelled = anyio.Event()

    async def append_second() -> None:
        try:
            await coordinator.append(_record("rec_2"), cancel=target)
        except asyncio.CancelledError:
            second_cancelled.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(coordinator.append, _record("rec_1"))
        await core.entered.wait()
        tasks.start_soon(append_second)
        await anyio.lowlevel.checkpoint()
        target.cancel()
        core.release.set()

    assert second_cancelled.is_set()
    assert core.calls == 1
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.expected_seq == 4


@pytest.mark.anyio
async def test_raw_cancel_from_conformant_core_is_prewrite_and_retryable() -> None:
    """core raw cancel 按协议证明无 mutation；coordinator 原样抛出但保持健康。"""
    core = _PrewriteCancelThenAckCore()
    coordinator = _coordinator(core)

    with pytest.raises(asyncio.CancelledError) as first:
        await coordinator.append(_record())
    await coordinator.ensure_effect_allowed()
    ack = await coordinator.append(_record())

    assert first.value is core.cancellation
    assert core.calls == 2
    assert ack.last_seq == coordinator.expected_seq == 4
    assert coordinator.health is AuditHealth.HEALTHY
    assert not coordinator.session_root_cancel.is_cancelled


@pytest.mark.anyio
@pytest.mark.parametrize("fatal", [KeyboardInterrupt("secret=k"), SystemExit("secret=s")])
async def test_process_fatal_after_core_invocation_freezes_then_reraises(
    fatal: KeyboardInterrupt | SystemExit,
) -> None:
    """进程 fatal 不得被替换或吞掉，但 Session 必须先 fail closed。"""
    core = _StaticCore(fatal)
    coordinator = _coordinator(core)

    with pytest.raises(type(fatal)) as first:
        await coordinator.append(_record())
    with pytest.raises(SessionAuditFrozenError) as repeated:
        await coordinator.append(_record("rec_2"))

    assert first.value is fatal
    assert core.calls == 1
    assert coordinator.session_root_cancel.is_cancelled
    assert repeated.value.cause.class_name == type(fatal).__name__


class _CoreAbort(BaseException):
    """模拟非 cancel/fatal 的 core BaseException。"""


@pytest.mark.anyio
async def test_other_base_exception_after_core_invocation_returns_frozen_error() -> None:
    """未知 BaseException 同样不能绕过 fail-closed gate。"""
    core = _StaticCore(_CoreAbort("secret=abort"))
    coordinator = _coordinator(core)

    with pytest.raises(SessionAuditFrozenError) as raised:
        await coordinator.append(_record())

    assert raised.value.cause.class_name == "_CoreAbort"
    assert coordinator.session_root_cancel.is_cancelled


class _PrewriteCancelledAdapter(DefaultSyncFileAdapter):
    """让真实 Jsonl core 第一次 append 明确报告 prewrite cancel。"""

    def __init__(self) -> None:
        """初始化单次 prewrite cancel 开关。"""
        self.cancelled = False

    def append_durable(self, path: Path, payload: bytes) -> None:
        """第一次证明未写并抛 cancel，第二次真实 durable append。"""
        if not self.cancelled:
            self.cancelled = True
            raise CommitNotStartedError(asyncio.CancelledError("secret=core-prewrite"))
        super().append_durable(path, payload)


@pytest.mark.anyio
async def test_real_jsonl_core_prewrite_cancel_stays_healthy_and_retries(
    tmp_path: Path,
) -> None:
    """真实 core raw cancel 只来自明确 prewrite，coordinator 不冻结且可重试。"""
    core = JsonlSessionJournalCore(
        tmp_path,
        sync_file_adapter=_PrewriteCancelledAdapter(),
    )
    created = await core.create_session(
        SessionDescriptor(
            session_id="ses_1",
            creation_operation_id="create_1",
            writer_id="writer_1",
            root_thread=RootThreadDescriptor(
                thread_id="thr_root",
                entry_skill_id="general",
            ),
            config={"model": "sim"},
        )
    )
    coordinator = SessionAuditCoordinator(
        core=core,
        lease=created.lease,
        expected_seq=created.ack.last_seq,
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.append(_record())
    await coordinator.ensure_effect_allowed()
    ack = await coordinator.append(_record())

    assert coordinator.health is AuditHealth.HEALTHY
    assert ack.last_seq == coordinator.expected_seq == 4
    assert not coordinator.session_root_cancel.is_cancelled


class _PostDispatchCancelledAdapter(DefaultSyncFileAdapter):
    """真实文件 mutation 后抛 cancel，验证 core 必须转换 recovery-required。"""

    def append_durable(self, path: Path, payload: bytes) -> None:
        """写入 BEGIN 后抛 cancel，制造 commit outcome unknown。"""
        with path.open("ab") as stream:
            stream.write(payload.splitlines(keepends=True)[0])
            stream.flush()
        raise asyncio.CancelledError("secret=postdispatch")


@pytest.mark.anyio
async def test_real_jsonl_postdispatch_cancel_becomes_recovery_and_freezes(
    tmp_path: Path,
) -> None:
    """真实 core 不得泄漏 postdispatch raw cancel；RecoveryRequired 冻结 coordinator。"""
    core = JsonlSessionJournalCore(
        tmp_path,
        sync_file_adapter=_PostDispatchCancelledAdapter(),
    )
    created = await core.create_session(
        SessionDescriptor(
            session_id="ses_1",
            creation_operation_id="create_1",
            writer_id="writer_1",
            root_thread=RootThreadDescriptor(
                thread_id="thr_root",
                entry_skill_id="general",
            ),
            config={"model": "sim"},
        )
    )
    coordinator = SessionAuditCoordinator(
        core=core,
        lease=created.lease,
        expected_seq=created.ack.last_seq,
    )

    with pytest.raises(SessionAuditFrozenError) as raised:
        await coordinator.append(_record())

    assert raised.value.cause.code == "journal_recovery_required"
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.expected_seq == 3


@pytest.mark.anyio
async def test_substitute_core_postdispatch_cancel_must_use_recovery_error() -> None:
    """替代 core 的 postdispatch cancel 只能通过 RecoveryRequired 表达。"""
    recovery = JournalRecoveryRequiredError(
        "ses_1",
        3,
        cause="commit_outcome_unknown",
    )
    coordinator = _coordinator(_StaticCore(recovery))

    with pytest.raises(SessionAuditFrozenError) as raised:
        await coordinator.append(_record())

    assert raised.value.cause.code == "journal_recovery_required"


class _StringSubclass(str):
    """构造与正常字符串等值但类型不精确的 ack 字段。"""


class _TupleSubclass(tuple[str, ...]):
    """构造与正常 tuple 等值但类型不精确的 ack 容器。"""


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "expected_seq"),
    [
        ("first_seq", True, 0),
        ("last_seq", True, 0),
        ("first_seq", 4.0, 3),
        ("last_seq", 4.0, 3),
        ("writer_epoch", True, 3),
        ("writer_epoch", 1.0, 3),
        ("record_ids", ["rec_1"], 3),
        ("record_ids", ("rec_1", 1), 3),
        ("record_ids", _TupleSubclass(("rec_1",)), 3),
        ("record_ids", (_StringSubclass("rec_1"),), 3),
        ("durability", "committed", 3),
        ("session_id", _StringSubclass("ses_1"), 3),
        ("tail_hash", "G" * 64, 3),
    ],
)
async def test_non_strict_or_tampered_ack_freezes_before_seq_advance(
    field: str,
    value: object,
    expected_seq: int,
) -> None:
    """model_construct/coercible/tampered ack 字段不得穿过 durable proof 边界。"""
    values: dict[str, object] = {
        "session_id": "ses_1",
        "first_seq": expected_seq + 1,
        "last_seq": expected_seq + 1,
        "record_ids": ("rec_1",),
        "tail_hash": _ZERO_HASH,
        "writer_epoch": 1,
        "durability": Durability.COMMITTED,
    }
    values[field] = value
    source = JournalAck.model_construct(None, **values)
    coordinator = _coordinator(_StaticCore(source), expected_seq=expected_seq)

    with pytest.raises(SessionAuditFrozenError) as raised:
        await coordinator.append(_record())

    assert raised.value.cause.code == "journal_ack_invalid"
    assert coordinator.expected_seq == expected_seq


@pytest.mark.anyio
async def test_valid_ack_is_rebuilt_before_return_and_seq_advance() -> None:
    """caller/core ack 与返回 ack 不得共享实例，内部 seq 也不能受返回对象篡改。"""
    source = _ack()
    coordinator = _coordinator(_StaticCore(source))

    returned = await coordinator.append(_record())

    assert returned == source
    assert returned is not source
    object.__setattr__(source, "last_seq", 900)
    object.__setattr__(returned, "last_seq", 901)
    assert coordinator.expected_seq == 4
    assert coordinator.snapshot().expected_seq == 4


def test_projection_return_and_introspection_snapshots_have_no_internal_aliases() -> None:
    """所有 projection 返回面都必须深复制 snapshot 与 StableErrorV1。"""
    coordinator = _coordinator(_StaticCore(_ack()))
    result = ProjectionResult(
        thread_id="thr_1",
        projected_seq=4,
        stale=True,
        failure_class="OSError",
    )

    marked = coordinator.mark_projection_stale(result)
    updated = coordinator.update_projection(result)
    projected = coordinator.projection_snapshot("thr_1")
    session_snapshot = coordinator.snapshot()
    introspected = session_snapshot.projections[0]

    assert projected is not None
    assert marked is not updated and updated is not projected
    assert projected is not introspected
    assert marked.failure is not updated.failure
    assert updated.failure is not projected.failure
    assert projected.failure is not introspected.failure
    object.__setattr__(marked, "projected_seq", 899)
    object.__setattr__(updated, "projected_seq", 900)
    assert updated.failure is not None
    object.__setattr__(updated.failure, "class_name", "Mutated")
    object.__setattr__(projected, "projected_seq", 901)
    assert projected.failure is not None
    object.__setattr__(projected.failure, "class_name", "AlsoMutated")
    object.__setattr__(introspected, "projected_seq", 902)
    object.__setattr__(session_snapshot, "expected_seq", 903)

    fresh = coordinator.projection_snapshot("thr_1")
    fresh_snapshot = coordinator.snapshot()
    fresh_session = fresh_snapshot.projections[0]
    assert fresh is not None and fresh.failure is not None
    assert fresh_session.failure is not None
    assert fresh.projected_seq == fresh_session.projected_seq == 4
    assert fresh.failure.class_name == fresh_session.failure.class_name == "OSError"
    assert fresh_snapshot.expected_seq == 3


@pytest.mark.anyio
async def test_first_failure_introspection_does_not_alias_frozen_error_cause() -> None:
    """snapshot.first_failure 必须复制，不能反向篡改唯一 FrozenError 的稳定 cause。"""
    coordinator = _coordinator(_StaticCore(OSError("secret=io")))

    with pytest.raises(SessionAuditFrozenError) as frozen:
        await coordinator.append(_record())
    first_snapshot = coordinator.snapshot()
    assert first_snapshot.first_failure is not None
    assert first_snapshot.first_failure is not frozen.value.cause
    object.__setattr__(first_snapshot.first_failure, "code", "mutated")
    object.__setattr__(frozen.value.cause, "code", "frozen-mutated")

    second_snapshot = coordinator.snapshot()
    assert second_snapshot.first_failure is not None
    assert second_snapshot.first_failure.code == "journal_io_error"
    assert frozen.value.cause.code == "journal_io_error"


@pytest.mark.anyio
async def test_failed_unregister_keeps_target_attached_for_root_freeze() -> None:
    """identity 不匹配时不得 detach，active target 仍须被 root freeze 级联取消。"""
    coordinator = _coordinator(_StaticCore(OSError("secret=io")))
    target = coordinator.register_target("turn_1")
    unrelated = CancellationToken(name="unrelated")

    assert coordinator.unregister_target("turn_1", unrelated) is False
    with pytest.raises(SessionAuditFrozenError):
        await coordinator.append(_record())

    assert target.is_cancelled


@pytest.mark.anyio
async def test_private_parent_edge_break_still_cancels_active_target_on_freeze() -> None:
    """即使内部 parent edge 被破坏，freeze 仍显式取消 active target 及 subtree。"""
    coordinator = _coordinator(_StaticCore(OSError("secret=io")))
    target = coordinator.register_target("turn_1")
    child = target.child("tool_1")
    assert target._detach_from_parent() is True  # noqa: SLF001

    with pytest.raises(SessionAuditFrozenError):
        await coordinator.append(_record())

    assert target.is_cancelled and child.is_cancelled


@pytest.mark.anyio
async def test_active_target_normally_remains_root_descendant_until_unregister() -> None:
    """未 unregister 的正常 active target 保持 root lineage，并由 freeze 取消。"""
    coordinator = _coordinator(_StaticCore(OSError("secret=io")))
    target = coordinator.register_target("turn_1")

    assert target in tuple(coordinator.session_root_cancel.descendants())
    with pytest.raises(SessionAuditFrozenError):
        await coordinator.append(_record())

    assert target.is_cancelled


def test_repeated_register_unregister_does_not_accumulate_root_descendants() -> None:
    """成功 unregister 必须 detach；大量 turn 结束后 root 不保留历史 token。"""
    coordinator = _coordinator(_StaticCore(_ack()))

    for _ in range(1000):
        target = coordinator.register_target("turn_1")
        assert coordinator.unregister_target("turn_1", target) is True

    assert tuple(coordinator.session_root_cancel.descendants()) == ()
