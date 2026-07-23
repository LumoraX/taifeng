"""SessionJournal 单 Session 协调器的并发、冻结与投影状态测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    Durability,
    JournalAck,
    JournalConflictError,
    JournalIntegrityError,
    JournalRecord,
    NonCanonicalValueError,
    ProjectionResult,
    SessionLease,
)
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditCoordinator,
    SessionAuditFrozenError,
)
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import Sequence


_ZERO_HASH = "0" * 64


def _lease(session_id: str = "ses_1") -> SessionLease:
    """构造固定 live lease。"""
    return SessionLease(
        session_id=session_id,
        writer_id=f"writer_{session_id}",
        writer_epoch=1,
        lease_id=f"lease_{session_id}",
    )


def _record(record_id: str, *, session_id: str = "ses_1") -> JournalRecord:
    """构造最小 runtime-owned JournalRecord。"""
    return JournalRecord(
        session_id=session_id,
        record_id=record_id,
        record_type="test_record",
        actor=ActorRef(kind="system", source="test"),
        payload={"record_id": record_id},
    )


@dataclass(frozen=True, slots=True)
class _AppendCall:
    """一次 core append 调用的稳定快照。"""

    record_ids: tuple[str, ...]
    expected_seq: int


class _RecordingCore:
    """返回确定 durable ack 的可控 core。"""

    def __init__(
        self,
        *,
        failures: Sequence[BaseException] = (),
        pause_first: bool = False,
        ack_session_id: str | None = None,
    ) -> None:
        """配置逐次失败、首调用暂停与错误 ack Session。"""
        self.failures = list(failures)
        self.pause_first = pause_first
        self.ack_session_id = ack_session_id
        self.calls: list[_AppendCall] = []
        self.entered_first = anyio.Event()
        self.release_first = anyio.Event()
        self.active = 0
        self.max_active = 0

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """记录调用，并按配置返回 ack 或抛错。"""
        self.calls.append(
            _AppendCall(
                record_ids=tuple(record.record_id for record in records),
                expected_seq=expected_seq,
            )
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.pause_first and len(self.calls) == 1:
                self.entered_first.set()
                await self.release_first.wait()
            if self.failures:
                raise self.failures.pop(0)
            return JournalAck(
                session_id=self.ack_session_id or lease.session_id,
                first_seq=expected_seq + 1,
                last_seq=expected_seq + len(records),
                record_ids=tuple(record.record_id for record in records),
                tail_hash=_ZERO_HASH,
                writer_epoch=lease.writer_epoch,
                durability=Durability.COMMITTED,
            )
        finally:
            self.active -= 1


def _coordinator(
    core: _RecordingCore,
    *,
    session_id: str = "ses_1",
    root: CancellationToken | None = None,
) -> SessionAuditCoordinator:
    """从初始化 batch 尾 seq=3 构造协调器。"""
    return SessionAuditCoordinator(
        core=core,
        lease=_lease(session_id),
        expected_seq=3,
        session_root_cancel=root,
    )


@pytest.mark.anyio
async def test_concurrent_appends_are_serialized_with_latest_expected_seq() -> None:
    """并发 append 必须单飞，第二批只能看到第一批 durable ack 的尾序号。"""
    core = _RecordingCore(pause_first=True)
    coordinator = _coordinator(core)
    acknowledgements: list[JournalAck] = []

    async def append(record_id: str) -> None:
        acknowledgements.append(await coordinator.append(_record(record_id)))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(append, "rec_1")
        await core.entered_first.wait()
        tasks.start_soon(append, "rec_2")
        await anyio.lowlevel.checkpoint()
        assert core.calls == [_AppendCall(("rec_1",), 3)]
        core.release_first.set()

    assert [call.expected_seq for call in core.calls] == [3, 4]
    assert core.max_active == 1
    assert [ack.last_seq for ack in acknowledgements] == [4, 5]
    assert coordinator.expected_seq == 5


@pytest.mark.anyio
async def test_batch_advances_expected_seq_only_from_covering_durable_ack() -> None:
    """有效 durable ack 必须完整覆盖 batch，随后 expected seq 精确推进到 last_seq。"""
    core = _RecordingCore()
    coordinator = _coordinator(core)

    ack = await coordinator.append_batch((_record("rec_1"), _record("rec_2")))

    assert ack.record_ids == ("rec_1", "rec_2")
    assert ack.last_seq == 5
    assert coordinator.expected_seq == ack.last_seq


@pytest.mark.anyio
async def test_invalid_ack_freezes_without_advancing_expected_seq() -> None:
    """错误 Session 的 ack 不是当前 batch 的 durable 证明，必须 fail closed。"""
    core = _RecordingCore(ack_session_id="ses_other")
    coordinator = _coordinator(core)

    with pytest.raises(SessionAuditFrozenError) as raised:
        await coordinator.append(_record("rec_1"))

    assert coordinator.expected_seq == 3
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert raised.value.cause.code == "journal_ack_invalid"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        OSError("secret=io-token"),
        JournalIntegrityError("secret=corrupt"),
        NonCanonicalValueError("secret=runtime-owned"),
        JournalConflictError("secret=record-conflict", record_id="rec_1"),
    ],
)
async def test_runtime_journal_failures_freeze_and_keep_first_safe_error(
    failure: BaseException,
) -> None:
    """runtime-owned Journal 失败均冻结，后续调用复用同一脱敏错误且不再 dispatch。"""
    core = _RecordingCore(failures=(failure, OSError("secret=second")))
    coordinator = _coordinator(core)

    with pytest.raises(SessionAuditFrozenError) as first:
        await coordinator.append(_record("rec_1"))
    with pytest.raises(SessionAuditFrozenError) as repeated:
        await coordinator.append(_record("rec_2"))
    with pytest.raises(SessionAuditFrozenError) as effect:
        await coordinator.ensure_effect_allowed()

    assert first.value is repeated.value is effect.value
    assert len(core.calls) == 1
    assert coordinator.expected_seq == 3
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.effect_gate_open is False
    assert "secret" not in str(first.value)
    assert first.value.__cause__ is None
    assert first.value.__context__ is None


@pytest.mark.anyio
async def test_pre_dispatch_batch_shape_error_does_not_freeze_or_dispatch() -> None:
    """协调器可明确识别的 caller 形状错误必须在 core 前拒绝，不污染 health。"""
    core = _RecordingCore()
    coordinator = _coordinator(core)

    with pytest.raises(ValueError, match="same Session"):
        await coordinator.append_batch(
            (_record("rec_1"), _record("rec_other", session_id="ses_other"))
        )
    ack = await coordinator.append(_record("rec_2"))

    assert ack.last_seq == 4
    assert core.calls == [_AppendCall(("rec_2",), 3)]
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.effect_gate_open is True


@pytest.mark.anyio
async def test_cancelled_operation_token_prevents_append_dispatch() -> None:
    """长时 append 接受 target token，并在 core dispatch 前响应已发生的取消。"""
    core = _RecordingCore()
    coordinator = _coordinator(core)
    target = coordinator.register_target("turn_1")
    target.cancel()

    with pytest.raises(asyncio.CancelledError):
        await coordinator.append(_record("rec_1"), cancel=target)

    assert core.calls == []
    assert coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_effect_gate_freeze_prevents_effect_dispatch() -> None:
    """Journal 首因冻结后，effect gate 必须在调用 effect 之前稳定拒绝。"""
    core = _RecordingCore(failures=(OSError("secret=commit"),))
    coordinator = _coordinator(core)
    dispatched = 0

    with pytest.raises(SessionAuditFrozenError):
        await coordinator.append(_record("intent"))

    async def dispatch_effect() -> None:
        nonlocal dispatched
        await coordinator.ensure_effect_allowed()
        dispatched += 1

    with pytest.raises(SessionAuditFrozenError):
        await dispatch_effect()

    assert dispatched == 0


@pytest.mark.anyio
async def test_target_cancel_is_scoped_but_freeze_cancels_session_root() -> None:
    """target cancel 只级联自己的 subtree；freeze 才取消 root 与全部 active target。"""
    root = CancellationToken(name="session:ses_1")
    core = _RecordingCore(failures=(OSError("secret=journal"),))
    coordinator = _coordinator(core, root=root)
    first = coordinator.register_target("turn_1")
    first_child = first.child("tool_1")
    second = coordinator.register_target("turn_2")

    assert coordinator.cancel_target("turn_1") is True
    assert first.is_cancelled and first_child.is_cancelled
    assert not root.is_cancelled
    assert not second.is_cancelled
    assert coordinator.cancel_target("missing") is False
    assert coordinator.unregister_target("turn_1", first) is True

    with pytest.raises(SessionAuditFrozenError):
        await coordinator.append(_record("rec_1"))

    snapshot = coordinator.snapshot()
    assert root.is_cancelled and second.is_cancelled
    assert snapshot.root_cancelled is True
    assert snapshot.active_target_ids == ("turn_2",)


@pytest.mark.anyio
async def test_two_session_coordinators_are_failure_isolated() -> None:
    """一个 Session 的首因冻结不得污染另一 Session 的 seq、gate 或 root。"""
    failed_root = CancellationToken(name="session:failed")
    healthy_root = CancellationToken(name="session:healthy")
    failed_core = _RecordingCore(failures=(OSError("secret=failed"),))
    healthy_core = _RecordingCore()
    failed = _coordinator(failed_core, session_id="ses_failed", root=failed_root)
    healthy = _coordinator(healthy_core, session_id="ses_healthy", root=healthy_root)

    with pytest.raises(SessionAuditFrozenError):
        await failed.append(_record("rec_failed", session_id="ses_failed"))
    await healthy.ensure_effect_allowed()
    ack = await healthy.append(_record("rec_healthy", session_id="ses_healthy"))

    assert failed.health is AuditHealth.RECOVERY_REQUIRED
    assert healthy.health is AuditHealth.HEALTHY
    assert ack.last_seq == 4
    assert failed_root.is_cancelled
    assert not healthy_root.is_cancelled


@pytest.mark.anyio
async def test_projection_stale_is_per_thread_and_does_not_freeze_execution() -> None:
    """projection stale 只更新目标 thread 状态，不改变 Journal health/effect/root。"""
    root = CancellationToken(name="session:ses_1")
    coordinator = _coordinator(_RecordingCore(), root=root)

    first = coordinator.update_projection(
        ProjectionResult(
            thread_id="thr_1",
            projected_seq=4,
            stale=True,
            failure_class="OSError",
        ),
    )
    second = coordinator.update_projection(
        ProjectionResult(
            thread_id="thr_2",
            projected_seq=0,
            stale=True,
            failure_class="RuntimeError",
        )
    )

    await coordinator.ensure_effect_allowed()
    assert first.stale is True and first.projected_seq == 4
    assert first.failure is not None
    assert second.stale is True and second.projected_seq == 0
    assert second.failure is not None
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.effect_gate_open is True
    assert coordinator.expected_seq == 3
    assert not root.is_cancelled


def test_healthy_projection_replay_across_seq_gap_clears_without_regression() -> None:
    """Journal 可含 domain seq gap；可信 healthy 同水位可清 stale，旧水位不得回退。"""
    coordinator = _coordinator(_RecordingCore())
    coordinator.update_projection(
        ProjectionResult(
            thread_id="thr_1",
            projected_seq=4,
            stale=True,
            failure_class="OSError",
        ),
    )

    still_stale = coordinator.update_projection(
        ProjectionResult(thread_id="thr_1", projected_seq=3, stale=False)
    )
    recovered = coordinator.update_projection(
        ProjectionResult(thread_id="thr_1", projected_seq=4, stale=False)
    )
    old_replay = coordinator.update_projection(
        ProjectionResult(thread_id="thr_1", projected_seq=2, stale=False)
    )

    assert still_stale.stale is True and still_stale.projected_seq == 4
    assert recovered.stale is False and recovered.projected_seq == 4
    assert recovered.failure is None
    assert old_replay == recovered


def test_introspection_returns_frozen_snapshot_not_mutable_internal_state() -> None:
    """只读 introspection 必须返回冻结快照并稳定排序 thread/target。"""
    coordinator = _coordinator(_RecordingCore())
    coordinator.register_target("turn_b")
    coordinator.register_target("turn_a")
    coordinator.update_projection(
        ProjectionResult(
            thread_id="thr_b",
            projected_seq=0,
            stale=True,
            failure_class="OSError",
        )
    )
    coordinator.update_projection(
        ProjectionResult(
            thread_id="thr_a",
            projected_seq=0,
            stale=True,
            failure_class="OSError",
        )
    )

    snapshot = coordinator.snapshot()

    assert snapshot.health is AuditHealth.HEALTHY
    assert snapshot.active_target_ids == ("turn_a", "turn_b")
    assert tuple(item.thread_id for item in snapshot.projections) == ("thr_a", "thr_b")
    with pytest.raises((AttributeError, TypeError)):
        snapshot.expected_seq = 99  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thread_id", ""),
        ("projected_seq", -1),
        ("stale", False),
    ],
)
def test_coordinator_defensively_rejects_tampered_projection_result(
    field: str,
    value: object,
) -> None:
    """即使调用方绕过 frozen dataclass，协调器也必须在状态 mutation 前重验 DTO。"""
    coordinator = _coordinator(_RecordingCore())
    malformed = ProjectionResult(
        thread_id="thr_1",
        projected_seq=4,
        stale=True,
        failure_class="OSError",
    )
    object.__setattr__(malformed, field, value)

    with pytest.raises((TypeError, ValueError)):
        coordinator.update_projection(malformed)

    assert coordinator.snapshot().projections == ()


def test_coordinator_rejects_non_result_and_healthy_stale_api_without_mutation() -> None:
    """coordinator 只消费可信 ProjectionResult，stale 专用入口拒绝 healthy。"""
    coordinator = _coordinator(_RecordingCore())
    healthy = ProjectionResult(thread_id="thr_1", projected_seq=4, stale=False)

    with pytest.raises(TypeError):
        coordinator.update_projection(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="stale"):
        coordinator.mark_projection_stale(healthy)

    assert coordinator.snapshot().projections == ()
