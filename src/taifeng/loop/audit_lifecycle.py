"""Session audit admission/lifecycle 的不可变公开 primitives。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
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

    async def wait_completed(self) -> None:
        """等待该 accepted work 收敛。"""
        await self._completed.wait()


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
    """所有 lifecycle caller 共享的稳定终结结果。"""

    session_id: str
    audit_complete: bool
    terminal_record_ids: tuple[str, ...]
    _failure: StableErrorV1 | None = None

    @property
    def failure(self) -> StableErrorV1 | None:
        """返回失败 DTO 副本，避免调用方篡改共享结果。"""
        return self._failure.model_copy() if self._failure is not None else None


class FinishFuture:
    """anyio 后端无关的一次性共享 finish result。"""

    def __init__(self) -> None:
        """创建未完成 future。"""
        self._completed = anyio.Event()
        self._result: SessionFinishResult | None = None

    def set_result(self, result: SessionFinishResult) -> None:
        """只允许 owner 发布一次结果。"""
        if self._result is not None:
            raise RuntimeError("finish result already set")
        self._result = result
        self._completed.set()

    async def wait(self) -> SessionFinishResult:
        """等待并返回 owner 发布的同一个不可变结果对象。"""
        await self._completed.wait()
        assert self._result is not None
        return self._result


__all__ = [
    "AcceptedWork",
    "SessionFinishResult",
    "SessionFinishingError",
    "SessionLifecycle",
    "ThreadTerminalRequest",
]
