"""Session audit 协调器的状态 DTO、稳定错误与防御性复制 helper。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from taifeng.conversation.journal.errors import (
    JournalConflictError,
    JournalError,
    JournalIntegrityError,
    JournalLeaseError,
    JournalRecoveryRequiredError,
    NonCanonicalValueError,
)
from taifeng.conversation.journal.records import StableErrorV1

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from taifeng.conversation.journal.projector import ProjectionResult
    from taifeng.loop.audit_lifecycle import SessionLifecycle


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
    lifecycle: SessionLifecycle
    audit_complete: bool | None
    lease_released: bool | None
    accepted_work_ids: tuple[str, ...]


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


class _InvalidJournalAckError(Exception):
    """core 返回的 ack 不能证明当前 batch durable。"""


async def _await_owned[T](
    operation: Coroutine[Any, Any, T],
    *,
    name: str,
) -> tuple[T, asyncio.CancelledError | None]:
    """等待 coordinator-owned operation；caller raw cancel 只能延迟重抛。"""
    worker = asyncio.create_task(operation, name=name)
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is None or current.cancelling() == 0:
                raise
            cancellation = cancellation or error
            current.uncancel()
    return worker.result(), cancellation


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


def _accepted_work_ownership_failure() -> StableErrorV1:
    """返回 durable accepted work 无法安全交付时的稳定恢复原因。"""
    return StableErrorV1(
        code="accepted_work_handoff_failed",
        class_name="AcceptedWorkHandoffFailure",
        failure_class="lifecycle",
        retryable=False,
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


__all__ = [
    "AuditHealth",
    "ProjectionAuditSnapshot",
    "SessionAuditFrozenError",
    "SessionAuditSnapshot",
]
