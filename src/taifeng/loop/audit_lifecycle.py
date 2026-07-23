"""Session audit lifecycle DTO 与内部同步 primitives。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import anyio

from taifeng.conversation.journal.models import ActorRef, JournalRecord
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    SessionEndedV1,
    ThreadTerminalV1,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from taifeng.conversation.journal.records import StableErrorV1


class SessionLifecycle(StrEnum):
    """受 admission lock 保护的单 Session 生命周期。"""

    OPEN = "open"
    FINISHING = "finishing"
    CLOSED = "closed"


class SessionFinishingError(RuntimeError):
    """Session intake 已关闭，不能再 durable accept 新 work。"""

    def __init__(self, session_id: str, lifecycle: SessionLifecycle) -> None:
        """只暴露稳定 Session/lifecycle 信息。"""
        super().__init__(f"session intake closed: session={session_id}, lifecycle={lifecycle}")
        self.session_id = session_id
        self.lifecycle = lifecycle


@dataclass(frozen=True, slots=True)
class AcceptedWork:
    """已 durable accept、必须在 Session terminal batch 前收敛的 work token。"""

    work_id: str
    _completed: anyio.Event

    def complete(self) -> None:
        """幂等标记 accepted work 已完成或已确定收敛。"""
        self._completed.set()

    @property
    def is_completed(self) -> bool:
        """返回 work 当前是否已收敛，不暴露内部 Event。"""
        return self._completed.is_set()

    async def wait_completed(self) -> None:
        """等待该 accepted work 收敛。"""
        await self._completed.wait()


class _AdmissionReservation:
    """shared lock 内登记、锁外 durable accept、再回锁结算的 reservation。"""

    __slots__ = ("_accepted_work", "_settled", "work_id")

    def __init__(self, work_id: str) -> None:
        """创建 pending acceptance reservation。"""
        self.work_id = work_id
        self._settled = anyio.Event()
        self._accepted_work: AcceptedWork | None = None

    @property
    def is_settled(self) -> bool:
        """返回 durable acceptance 是否已有确定结果。"""
        return self._settled.is_set()

    @property
    def accepted_work(self) -> AcceptedWork | None:
        """返回 durable accepted work；failed/pending 均为 None。"""
        return self._accepted_work

    @property
    def is_incomplete(self) -> bool:
        """pending acceptance 或尚未完成的 accepted work 都属于未收敛证据。"""
        return not self.is_settled or (
            self._accepted_work is not None and not self._accepted_work.is_completed
        )

    def settle_accepted(self, work: AcceptedWork) -> None:
        """在 coordinator lifecycle lock 内结算为 durable accepted。"""
        if self.is_settled:
            raise RuntimeError("admission reservation already settled")
        self._accepted_work = work
        self._settled.set()

    def settle_failed(self) -> None:
        """在 coordinator lifecycle lock 内结算为未 durable accept。"""
        if self.is_settled:
            raise RuntimeError("admission reservation already settled")
        self._settled.set()

    async def wait_settled(self) -> None:
        """等待锁外 durable acceptance 回到 shared lock 完成结算。"""
        await self._settled.wait()


@dataclass(frozen=True, slots=True)
class ThreadTerminalRequest:
    """finish 构造确定性 thread terminal record 所需的最小不可变输入。"""

    thread_id: str
    status: str
    end_reason: str
    stable_error: StableErrorV1 | None = None

    def __post_init__(self) -> None:
        """拒绝会生成含糊 terminal payload 的空字段。"""
        if not self.thread_id or not self.status or not self.end_reason:
            raise ValueError("thread terminal fields must be non-empty")


@dataclass(frozen=True, slots=True)
class SessionFinishResult:
    """单个 lifecycle caller 持有的防御性终结结果。"""

    session_id: str
    audit_complete: bool
    lease_released: bool
    terminal_record_ids: tuple[str, ...]
    _failure: StableErrorV1 | None = None

    @property
    def failure(self) -> StableErrorV1 | None:
        """返回失败 DTO 副本，避免调用方篡改共享结果。"""
        return self._failure.model_copy() if self._failure is not None else None


def _copy_finish_result(result: SessionFinishResult) -> SessionFinishResult:
    """重建 finish DTO 及嵌套 failure，隔离 object.__setattr__ 篡改。"""
    return SessionFinishResult(
        session_id=result.session_id,
        audit_complete=result.audit_complete,
        lease_released=result.lease_released,
        terminal_record_ids=tuple(result.terminal_record_ids),
        _failure=result.failure,
    )


class _FinishFuture:
    """anyio 后端无关的一次性共享 canonical finish value。"""

    def __init__(self) -> None:
        """创建未完成 future。"""
        self._completed = anyio.Event()
        self._result: SessionFinishResult | None = None

    def set_result(self, result: SessionFinishResult) -> None:
        """只允许 owner 发布一次防御性副本。"""
        if self._result is not None:
            raise RuntimeError("finish result already set")
        self._result = _copy_finish_result(result)
        self._completed.set()

    async def wait(self) -> SessionFinishResult:
        """等待并为每个 caller 重建独立 result/failure。"""
        await self._completed.wait()
        assert self._result is not None
        return _copy_finish_result(self._result)


def _build_terminal_records(
    *,
    session_id: str,
    requests: tuple[ThreadTerminalRequest, ...],
    committed_thread_ids: Collection[str],
    status: str,
    reason: str,
) -> tuple[JournalRecord, ...]:
    """复用 V1 factory 生成排序、稳定 ordinal 的唯一 terminal batch。"""
    operation_id = f"{session_id}:lifecycle:end"
    factory = JournalRecordFactory(
        session_id=session_id,
        actor=ActorRef(kind="system", source="session_lifecycle"),
        identities=JournalIdentities(session_id, "lifecycle", "finish"),
    )
    pending = sorted(
        (
            request
            for request in requests
            if request.thread_id not in committed_thread_ids
        ),
        key=lambda request: request.thread_id,
    )
    records = [
        factory.build(
            operation_id=operation_id,
            record_type="thread_terminal",
            payload=ThreadTerminalV1(
                status=request.status,
                end_reason=request.end_reason,
                stable_error=request.stable_error,
            ),
            ordinal=ordinal,
            thread_id=request.thread_id,
        )
        for ordinal, request in enumerate(pending)
    ]
    records.append(
        factory.build(
            operation_id=operation_id,
            record_type="session_ended",
            payload=SessionEndedV1(
                status=status,
                reason=reason,
                audit_complete=True,
            ),
        )
    )
    return tuple(records)


__all__ = [
    "AcceptedWork",
    "SessionFinishResult",
    "SessionFinishingError",
    "SessionLifecycle",
    "ThreadTerminalRequest",
]
