"""Audited Shutdown 的唯一 admission、replay 与 finish handoff。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import anyio

from taifeng.conversation.journal.records import SubmissionAcceptedV1
from taifeng.loop.audit_lifecycle import (
    SessionFinishingError,
    SessionFinishResult,
    SessionLifecycle,
)
from taifeng.loop.audit_support import SessionAuditFrozenError

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import JournalRecord
    from taifeng.loop.audit_journal import JournalAppendReceipt
    from taifeng.loop.audit_lifecycle import _FinishFuture


class _CoordinatorProtocol(Protocol):
    """mixin 所需的 coordinator 私有能力边界。"""

    _append_lock: anyio.Lock
    _finish_future: _FinishFuture | None
    _frozen_error: SessionAuditFrozenError | None
    _lifecycle: SessionLifecycle
    _lifecycle_lock: anyio.Lock

    @property
    def session_id(self) -> str:
        """返回 Session id。"""

    async def _append_locked(
        self,
        records: tuple[JournalRecord, ...],
    ) -> JournalAppendReceipt:
        """在 append lock 内提交已验证记录。"""

    def _raise_if_append_sealed(self) -> None:
        """terminal seal 后拒绝普通 append。"""


@dataclass(frozen=True, slots=True)
class ShutdownAdmissionClaim:
    """Shutdown admission 的唯一 owner 与待延迟重抛 fatal。"""

    owner: bool
    fatal: KeyboardInterrupt | SystemExit | None = None


class ShutdownLifecycleMixin:
    """向 SessionAuditCoordinator 提供单一 Shutdown lifecycle admission。"""

    _shutdown_submission_id: str | None
    _shutdown_accepted_record_id: str | None
    _shutdown_thread_id: str | None
    _shutdown_finish_started: anyio.Event

    def _init_shutdown_lifecycle(self) -> None:
        """初始化唯一 Shutdown identity 与 finish handoff。"""
        self._shutdown_submission_id = None
        self._shutdown_accepted_record_id = None
        self._shutdown_thread_id = None
        self._shutdown_finish_started = anyio.Event()

    @property
    def shutdown_submission_id(self) -> str | None:
        """返回已登记的 durable Shutdown id，供 actor EventMsg 归因。"""
        return self._shutdown_submission_id

    async def admit_shutdown(self, record: JournalRecord) -> ShutdownAdmissionClaim:
        """登记首个 Shutdown；healthy 时 durable accept，frozen 时安全降级。"""
        from taifeng.loop.audit_journal import validate_records

        coordinator = cast("_CoordinatorProtocol", self)
        records = validate_records((record,), session_id=coordinator.session_id)
        accepted = SubmissionAcceptedV1.model_validate(record.payload)
        valid = (
            record.record_type == "submission_accepted"
            and accepted.op_kind == "shutdown"
            and record.submission_id is not None
            and record.thread_id is not None
        )
        if not valid:
            raise ValueError("invalid Shutdown acceptance record")
        async with coordinator._lifecycle_lock:
            if self._shutdown_submission_id == record.submission_id:
                return ShutdownAdmissionClaim(owner=False)
            if coordinator._lifecycle is not SessionLifecycle.OPEN:
                raise SessionFinishingError(
                    coordinator.session_id,
                    coordinator._lifecycle,
                )
            coordinator._lifecycle = SessionLifecycle.FINISHING
            self._shutdown_submission_id = record.submission_id
            self._shutdown_thread_id = record.thread_id
            if coordinator._frozen_error is not None:
                return ShutdownAdmissionClaim(owner=True)
            try:
                async with coordinator._append_lock:
                    coordinator._raise_if_append_sealed()
                    receipt = await coordinator._append_locked(records)
            except (KeyboardInterrupt, SystemExit) as fatal:
                return ShutdownAdmissionClaim(owner=True, fatal=fatal)
            except SessionAuditFrozenError:
                return ShutdownAdmissionClaim(owner=True)
            self._shutdown_accepted_record_id = receipt.ack.record_ids[0]
        return ShutdownAdmissionClaim(owner=True)

    async def wait_shutdown_finish(
        self,
        submission_id: str,
    ) -> SessionFinishResult:
        """same-id retry 等待已登记 owner 发布的唯一 finish future。"""
        coordinator = cast("_CoordinatorProtocol", self)
        async with coordinator._lifecycle_lock:
            if self._shutdown_submission_id != submission_id:
                raise SessionFinishingError(
                    coordinator.session_id,
                    coordinator._lifecycle,
                )
            future = coordinator._finish_future
        if future is None:
            await self._shutdown_finish_started.wait()
            async with coordinator._lifecycle_lock:
                future = coordinator._finish_future
        assert future is not None
        return await future.wait()

    def _publish_shutdown_finish_started(self) -> None:
        """在唯一 finish future 安装后唤醒 same-id replay。"""
        self._shutdown_finish_started.set()

    def _shutdown_admission(self) -> tuple[str, str, str] | None:
        """返回已 durable accept 的 Shutdown terminal batch 输入。"""
        if (
            self._shutdown_submission_id is None
            or self._shutdown_accepted_record_id is None
            or self._shutdown_thread_id is None
        ):
            return None
        return (
            self._shutdown_submission_id,
            self._shutdown_accepted_record_id,
            self._shutdown_thread_id,
        )


__all__ = ["ShutdownAdmissionClaim", "ShutdownLifecycleMixin"]
