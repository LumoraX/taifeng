"""单 Session 的 Journal 追加、冻结、取消与投影状态协调。"""

from __future__ import annotations

import re
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
from taifeng.conversation.journal.projector import ProjectionResult
from taifeng.conversation.journal.records import StableErrorV1
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import Sequence

_HASH_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
        self._cause = _copy_stable_error(cause)

    @property
    def cause(self) -> StableErrorV1:
        """返回稳定首因副本，避免异常接收方篡改 coordinator 内部状态。"""
        return _copy_stable_error(self._cause)


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


def _projection_failure(result: ProjectionResult) -> StableErrorV1:
    """从已验证 projector stale 结果构造不含任意异常原文的稳定原因。"""
    assert result.failure_class is not None
    return StableErrorV1(
        code="projection_stale",
        class_name=result.failure_class,
        failure_class="projection",
        retryable=True,
    )


def _copy_stable_error(error: StableErrorV1) -> StableErrorV1:
    """重建稳定错误，阻断调用方用 object.__setattr__ 篡改内部状态。"""
    return StableErrorV1(
        payload_version=error.payload_version,
        code=error.code,
        class_name=error.class_name,
        failure_class=error.failure_class,
        safe_message=error.safe_message,
        descriptor_hash=error.descriptor_hash,
        retryable=error.retryable,
    )


def _copy_projection_snapshot(
    snapshot: ProjectionAuditSnapshot,
) -> ProjectionAuditSnapshot:
    """深复制 projection snapshot 及其可绕过 frozen 的 Pydantic failure。"""
    return ProjectionAuditSnapshot(
        thread_id=snapshot.thread_id,
        projected_seq=snapshot.projected_seq,
        stale=snapshot.stale,
        failure=(
            _copy_stable_error(snapshot.failure)
            if snapshot.failure is not None
            else None
        ),
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
        self._first_failure: StableErrorV1 | None = None
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
            ack = self._validate_ack(ack, records)
        except (KeyboardInterrupt, SystemExit) as error:
            self.freeze(error)
            raise
        except BaseException as error:
            if isinstance(error, anyio.get_cancelled_exc_class()):
                self.freeze(error)
                raise
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
        ack: object,
        records: tuple[JournalRecord, ...],
    ) -> JournalAck:
        """以 exact type 重验并重建 ack，拒绝 coercion、子类与 caller 别名。"""
        if type(ack) is not JournalAck:
            raise _InvalidJournalAckError
        assert isinstance(ack, JournalAck)
        if (
            type(ack.session_id) is not str
            or type(ack.first_seq) is not int
            or type(ack.last_seq) is not int
            or type(ack.record_ids) is not tuple
            or any(type(record_id) is not str or not record_id for record_id in ack.record_ids)
            or type(ack.tail_hash) is not str
            or _HASH_HEX_PATTERN.fullmatch(ack.tail_hash) is None
            or type(ack.writer_epoch) is not int
            or type(ack.durability) is not Durability
        ):
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
        return JournalAck(
            session_id=ack.session_id,
            first_seq=ack.first_seq,
            last_seq=ack.last_seq,
            record_ids=ack.record_ids,
            tail_hash=ack.tail_hash,
            writer_epoch=ack.writer_epoch,
            durability=ack.durability,
        )

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
        stable_cause = (
            _copy_stable_error(cause)
            if isinstance(cause, StableErrorV1)
            else _journal_failure(cause)
        )
        self._first_failure = _copy_stable_error(stable_cause)
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
        token.detach()
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
        result: ProjectionResult,
    ) -> ProjectionAuditSnapshot:
        """只镜像可信 stale 结果；不接收 raw exception 或猜测水位。"""
        validated = self._validated_projection_result(result)
        if not validated.stale:
            raise ValueError("mark_projection_stale requires a stale result")
        return self._mark_projection_stale(validated)

    def _mark_projection_stale(
        self,
        result: ProjectionResult,
    ) -> ProjectionAuditSnapshot:
        """把已重验 stale 结果单调写入目标 thread 状态。"""
        current = self._projections.get(result.thread_id)
        incoming_seq = result.projected_seq
        if current is not None and not current.stale and incoming_seq < current.projected_seq:
            return _copy_projection_snapshot(current)
        projected_seq = max(
            current.projected_seq if current is not None else 0,
            incoming_seq,
        )
        state = ProjectionAuditSnapshot(
            thread_id=result.thread_id,
            projected_seq=projected_seq,
            stale=True,
            failure=current.failure
            if current is not None and current.stale
            else _projection_failure(result),
        )
        return self._store_projection(state)

    def update_projection(self, result: ProjectionResult) -> ProjectionAuditSnapshot:
        """重验并镜像 projector 结果；健康 replay 单调推进或清 stale。"""
        validated = self._validated_projection_result(result)
        if validated.stale:
            return self._mark_projection_stale(validated)
        current = self._projections.get(validated.thread_id)
        if current is None:
            state = ProjectionAuditSnapshot(
                thread_id=validated.thread_id,
                projected_seq=validated.projected_seq,
                stale=False,
            )
        else:
            state = self._healthy_projection_state(current, validated.projected_seq)
        return self._store_projection(state)

    def _store_projection(
        self,
        state: ProjectionAuditSnapshot,
    ) -> ProjectionAuditSnapshot:
        """内部保存与 caller 返回各自深复制，永不共享 snapshot/failure。"""
        internal = _copy_projection_snapshot(state)
        self._projections[state.thread_id] = internal
        return _copy_projection_snapshot(internal)

    @staticmethod
    def _validated_projection_result(result: ProjectionResult) -> ProjectionResult:
        """拒绝错误类型，并重建 frozen DTO 以防 object.__setattr__ 绕过构造校验。"""
        if not isinstance(result, ProjectionResult):
            raise TypeError("coordinator accepts only ProjectionResult")
        return ProjectionResult(
            thread_id=result.thread_id,
            projected_seq=result.projected_seq,
            stale=result.stale,
            failure_class=result.failure_class,
            failure_record_id=result.failure_record_id,
        )

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
        state = self._projections.get(thread_id)
        return _copy_projection_snapshot(state) if state is not None else None

    def snapshot(self) -> SessionAuditSnapshot:
        """返回不暴露可变 mapping/token/core 的只读状态快照。"""
        return SessionAuditSnapshot(
            session_id=self.session_id,
            expected_seq=self._expected_seq,
            health=self._health,
            effect_gate_open=self._effect_gate_open,
            root_cancelled=self._session_root_cancel.is_cancelled,
            first_failure=(
                _copy_stable_error(self._first_failure)
                if self._first_failure is not None
                else None
            ),
            active_target_ids=tuple(sorted(self._targets)),
            projections=tuple(
                _copy_projection_snapshot(self._projections[thread_id])
                for thread_id in sorted(self._projections)
            ),
        )


__all__ = [
    "AuditHealth",
    "ProjectionAuditSnapshot",
    "SessionAuditCoordinator",
    "SessionAuditFrozenError",
    "SessionAuditSnapshot",
]
