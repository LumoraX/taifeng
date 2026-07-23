"""单 Session 的 Journal 追加、冻结、取消与投影状态协调。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import anyio

from taifeng.conversation.journal.errors import (
    JournalConflictError,
    JournalError,
    JournalIntegrityError,
    JournalLeaseError,
    JournalRecoveryRequiredError,
    NonCanonicalValueError,
)
from taifeng.conversation.journal.models import (
    Durability,
    JournalAck,
    JournalRecord,
    SessionLease,
)
from taifeng.conversation.journal.records import StableErrorV1, stable_error
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.journal.projector import ProjectionResult


class AuditHealth(StrEnum):
    """Session 审计协调器的可执行健康状态。"""

    HEALTHY = "healthy"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class ProjectionAuditSnapshot:
    """单 thread 的只读投影水位与稳定失败快照。"""

    thread_id: str
    projected_seq: int
    stale: bool
    failure: StableErrorV1 | None = None


@dataclass(frozen=True, slots=True)
class SessionAuditSnapshot:
    """协调器的只读、不可变 introspection 快照。"""

    session_id: str
    expected_seq: int
    health: AuditHealth
    effect_gate_open: bool
    root_cancelled: bool
    first_failure: StableErrorV1 | None
    active_target_ids: tuple[str, ...]
    projections: tuple[ProjectionAuditSnapshot, ...]


class SessionAuditFrozenError(RuntimeError):
    """Session 已因第一个 Journal 不确定性进入 recovery-required。"""

    def __init__(self, session_id: str, cause: StableErrorV1) -> None:
        """只保存稳定 DTO，不复制底层异常文本或 repr。"""
        super().__init__(
            "session audit frozen: "
            f"session={session_id}, code={cause.code}, class={cause.class_name}"
        )
        self.session_id = session_id
        self.cause = cause


class _JournalAppendCore(Protocol):
    """协调器依赖的最小 Journal append 边界。"""

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """以 caller expected seq 原子追加一个 batch。"""


class _InvalidJournalAckError(Exception):
    """core 返回的 ack 不能证明当前 batch durable。"""


def _journal_failure(error: BaseException) -> StableErrorV1:
    """把 Journal 边界异常映射为不读取原文的稳定失败 DTO。"""
    if isinstance(error, _InvalidJournalAckError):
        code, failure_class = "journal_ack_invalid", "journal_uncertain"
    elif isinstance(error, JournalRecoveryRequiredError):
        code, failure_class = "journal_recovery_required", "journal_uncertain"
    elif isinstance(error, JournalIntegrityError):
        code, failure_class = "journal_integrity_error", "journal_integrity"
    elif isinstance(error, JournalLeaseError):
        code, failure_class = "journal_lease_error", "journal_fencing"
    elif isinstance(error, JournalConflictError):
        code, failure_class = "journal_conflict", "journal_invariant"
    elif isinstance(error, NonCanonicalValueError):
        code, failure_class = "journal_noncanonical_runtime", "journal_invariant"
    elif isinstance(error, OSError):
        code, failure_class = "journal_io_error", "journal_io"
    elif isinstance(error, JournalError):
        code, failure_class = "journal_error", "journal_invariant"
    else:
        code, failure_class = "journal_core_error", "journal_uncertain"
    return StableErrorV1(
        code=code,
        class_name=type(error).__name__,
        failure_class=failure_class,
        retryable=False,
    )


def _projection_failure(
    result: ProjectionResult | None,
    failure: StableErrorV1 | BaseException | None,
) -> StableErrorV1:
    """从 projector 结果或异常构造不含原文的稳定 stale 原因。"""
    if isinstance(failure, StableErrorV1):
        return failure
    if failure is not None:
        return stable_error(failure)
    class_name = result.failure_class if result and result.failure_class else "ProjectionFailure"
    return StableErrorV1(
        code="projection_stale",
        class_name=class_name,
        failure_class="projection",
        retryable=True,
    )


class SessionAuditCoordinator:
    """串行化一个 Session 的 Journal append，并隔离其 fail-closed 状态。"""

    def __init__(
        self,
        *,
        core: _JournalAppendCore,
        lease: SessionLease,
        expected_seq: int,
        session_root_cancel: CancellationToken | None = None,
    ) -> None:
        """注入 core/lease/初始化 ack 尾序号与可选 Session root token。"""
        if expected_seq < 0:
            raise ValueError("expected_seq must be non-negative")
        self._core = core
        self._lease = lease
        self._expected_seq = expected_seq
        self._session_root_cancel = session_root_cancel or CancellationToken(
            name=f"session:{lease.session_id}"
        )
        self._append_lock = anyio.Lock()
        self._health = AuditHealth.HEALTHY
        self._effect_gate_open = True
        self._frozen_error: SessionAuditFrozenError | None = None
        self._targets: dict[str, CancellationToken] = {}
        self._projections: dict[str, ProjectionAuditSnapshot] = {}

    @property
    def session_id(self) -> str:
        """返回 coordinator 绑定的 Session id。"""
        return self._lease.session_id

    @property
    def expected_seq(self) -> int:
        """返回下一批 CAS 使用的 committed tail seq。"""
        return self._expected_seq

    @property
    def health(self) -> AuditHealth:
        """返回当前审计健康状态。"""
        return self._health

    @property
    def effect_gate_open(self) -> bool:
        """返回新 effect 是否仍可进入。"""
        return self._effect_gate_open

    @property
    def session_root_cancel(self) -> CancellationToken:
        """返回 Session root token，供 Engine/Turn 派生取消 subtree。"""
        return self._session_root_cancel

    async def append(
        self,
        record: JournalRecord,
        *,
        cancel: CancellationToken | None = None,
    ) -> JournalAck:
        """串行追加单条 runtime-owned record。"""
        return await self.append_batch((record,), cancel=cancel)

    async def append_batch(
        self,
        records: Sequence[JournalRecord],
        *,
        cancel: CancellationToken | None = None,
    ) -> JournalAck:
        """串行追加 batch，只以覆盖当前 batch 的 durable ack 推进 seq。"""
        self._raise_if_frozen()
        snapshot = self._validate_records(records)
        if cancel is not None:
            cancel.raise_if_cancelled()
        async with self._append_lock:
            self._raise_if_frozen()
            if cancel is not None:
                cancel.raise_if_cancelled()
            return await self._append_locked(snapshot)

    async def _append_locked(
        self,
        records: tuple[JournalRecord, ...],
    ) -> JournalAck:
        """在 append lock 内调用 core，并把任何 runtime 边界失败原子冻结。"""
        failure: BaseException | None = None
        try:
            ack = await self._core.append_batch(
                records,
                lease=self._lease,
                expected_seq=self._expected_seq,
            )
            self._validate_ack(ack, records)
        except (KeyboardInterrupt, SystemExit) as error:
            self.freeze(error)
            raise
        except Exception as error:
            failure = error
        if failure is not None:
            self.freeze(failure)
            failure = None
            self._raise_if_frozen()
        self._expected_seq = ack.last_seq
        return ack

    def _validate_records(
        self,
        records: Sequence[JournalRecord],
    ) -> tuple[JournalRecord, ...]:
        """在 dispatch 前拒绝空、非 DTO 或跨 Session batch，不冻结 Journal。"""
        snapshot = tuple(records)
        if not snapshot:
            raise ValueError("journal batch must contain at least one record")
        if any(not isinstance(record, JournalRecord) for record in snapshot):
            raise TypeError("journal batch accepts only JournalRecord values")
        if any(record.session_id != self.session_id for record in snapshot):
            raise ValueError("all records must belong to the same Session")
        return snapshot

    def _validate_ack(
        self,
        ack: JournalAck,
        records: tuple[JournalRecord, ...],
    ) -> None:
        """要求 ack 精确覆盖当前新 batch、lease epoch 与 expected seq。"""
        if not isinstance(ack, JournalAck):
            raise _InvalidJournalAckError
        expected_record_ids = tuple(record.record_id for record in records)
        expected_first_seq = self._expected_seq + 1
        expected_last_seq = self._expected_seq + len(records)
        valid = (
            ack.session_id == self.session_id
            and ack.writer_epoch == self._lease.writer_epoch
            and ack.durability is Durability.COMMITTED
            and ack.record_ids == expected_record_ids
            and ack.first_seq == expected_first_seq
            and ack.last_seq == expected_last_seq
        )
        if not valid:
            raise _InvalidJournalAckError

    async def ensure_effect_allowed(self) -> None:
        """effect 前 fail-closed 检查；冻结后永远返回同一稳定错误。"""
        self._raise_if_frozen()

    def freeze(
        self,
        cause: BaseException | StableErrorV1,
    ) -> SessionAuditFrozenError:
        """第一次调用同步、无 await 地固定首因、关 gate 并取消整个 Session。"""
        if self._frozen_error is not None:
            return self._frozen_error
        stable_cause = cause if isinstance(cause, StableErrorV1) else _journal_failure(cause)
        frozen = SessionAuditFrozenError(self.session_id, stable_cause)
        self._frozen_error = frozen
        self._health = AuditHealth.RECOVERY_REQUIRED
        self._effect_gate_open = False
        self._session_root_cancel.cancel()
        return frozen

    def _raise_if_frozen(self) -> None:
        """重抛唯一冻结错误，并清理旧 traceback/context 引用。"""
        frozen = self._frozen_error
        if frozen is None:
            return
        frozen.__traceback__ = None
        frozen.__cause__ = None
        frozen.__context__ = None
        frozen.__suppress_context__ = True
        raise frozen from None

    def register_target(self, target_id: str) -> CancellationToken:
        """登记 active turn token；其 child 自动形成目标取消 subtree。"""
        self._raise_if_frozen()
        if not target_id:
            raise ValueError("target_id must be non-empty")
        if target_id in self._targets:
            raise ValueError(f"target already registered: {target_id}")
        target = self._session_root_cancel.child(f"target:{target_id}")
        self._targets[target_id] = target
        return target

    def unregister_target(
        self,
        target_id: str,
        token: CancellationToken,
    ) -> bool:
        """仅当 id/token 仍对应同一 active turn 时注销，避免旧任务误删新任务。"""
        current = self._targets.get(target_id)
        if current is not token:
            return False
        self._targets.pop(target_id)
        return True

    def cancel_target(self, target_id: str) -> bool:
        """只取消目标 turn 及其 child subtree，不影响 Session root/peer target。"""
        target = self._targets.get(target_id)
        if target is None:
            return False
        target.cancel()
        return True

    def mark_projection_stale(
        self,
        thread_id: str,
        result: ProjectionResult | None = None,
        *,
        failure: StableErrorV1 | BaseException | None = None,
    ) -> ProjectionAuditSnapshot:
        """只标记一个 thread stale；不触碰 health、effect gate 或 root token。"""
        if not thread_id:
            raise ValueError("thread_id must be non-empty")
        if result is not None and result.thread_id != thread_id:
            raise ValueError("projection result belongs to another thread")
        if result is not None and not result.stale:
            raise ValueError("healthy projection result must use update_projection")
        current = self._projections.get(thread_id)
        incoming_seq = (
            result.projected_seq
            if result is not None
            else current.projected_seq if current is not None else 0
        )
        if current is not None and not current.stale and incoming_seq < current.projected_seq:
            return current
        projected_seq = max(
            current.projected_seq if current is not None else 0,
            incoming_seq,
        )
        state = ProjectionAuditSnapshot(
            thread_id=thread_id,
            projected_seq=projected_seq,
            stale=True,
            failure=current.failure
            if current is not None and current.stale
            else _projection_failure(result, failure),
        )
        self._projections[thread_id] = state
        return state

    def update_projection(self, result: ProjectionResult) -> ProjectionAuditSnapshot:
        """接收 projector 结果；健康 replay 单调推进并按失败水位清 stale。"""
        if result.stale:
            return self.mark_projection_stale(result.thread_id, result)
        current = self._projections.get(result.thread_id)
        if current is None:
            state = ProjectionAuditSnapshot(
                thread_id=result.thread_id,
                projected_seq=result.projected_seq,
                stale=False,
            )
        else:
            state = self._healthy_projection_state(current, result.projected_seq)
        self._projections[result.thread_id] = state
        return state

    @staticmethod
    def _healthy_projection_state(
        current: ProjectionAuditSnapshot,
        replayed_seq: int,
    ) -> ProjectionAuditSnapshot:
        """可信 healthy 结果不低于当前水位才清 stale，旧 replay 完全忽略。"""
        if replayed_seq < current.projected_seq:
            return current
        return ProjectionAuditSnapshot(
            thread_id=current.thread_id,
            projected_seq=replayed_seq,
            stale=False,
        )

    def projection_snapshot(self, thread_id: str) -> ProjectionAuditSnapshot | None:
        """读取一个 thread 的冻结投影快照。"""
        return self._projections.get(thread_id)

    def snapshot(self) -> SessionAuditSnapshot:
        """返回不暴露可变 mapping/token/core 的只读状态快照。"""
        return SessionAuditSnapshot(
            session_id=self.session_id,
            expected_seq=self._expected_seq,
            health=self._health,
            effect_gate_open=self._effect_gate_open,
            root_cancelled=self._session_root_cancel.is_cancelled,
            first_failure=(
                self._frozen_error.cause if self._frozen_error is not None else None
            ),
            active_target_ids=tuple(sorted(self._targets)),
            projections=tuple(
                self._projections[thread_id] for thread_id in sorted(self._projections)
            ),
        )


__all__ = [
    "AuditHealth",
    "ProjectionAuditSnapshot",
    "SessionAuditCoordinator",
    "SessionAuditFrozenError",
    "SessionAuditSnapshot",
]
