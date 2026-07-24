"""单 Session 的 target cancellation registry。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import anyio

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken

TargetCancelStatus = Literal["cancelled", "already_terminal", "not_found"]


@dataclass(frozen=True, slots=True)
class TargetCancelResult:
    """一次 CancelTurn 的稳定、可重放结果。"""

    result_status: TargetCancelStatus
    terminal_record_ids: tuple[str, ...]


@dataclass(slots=True)
class _TargetState:
    """active target token 与终态唤醒点。"""

    token: CancellationToken
    terminal: anyio.Event
    terminal_record_ids: tuple[str, ...] = ()
    execution_end_reason: str | None = None
    closed_without_terminal: bool = False
    cancel_requested: bool = False


@dataclass(slots=True)
class _CancelResolution:
    """同一 CancelTurn submission 的共享收敛状态。"""

    target: _TargetState | None
    result: TargetCancelResult | None = None


class TargetCancellationRegistry:
    """登记 active/terminal target，并稳定解析 CancelTurn 重试。"""

    def __init__(self, root: CancellationToken) -> None:
        """绑定 Session root；target 只从该 root 派生子树。"""
        self._root = root
        self._active: dict[str, _TargetState] = {}
        self._terminal: dict[str, tuple[str, ...]] = {}
        self._resolutions: dict[str, _CancelResolution] = {}

    @property
    def active_target_ids(self) -> tuple[str, ...]:
        """返回排序后的 active target identities。"""
        return tuple(sorted(self._active))

    def register(self, target_id: str) -> CancellationToken:
        """登记 active target 及其独立 cancellation subtree。"""
        if not target_id:
            raise ValueError("target_id must be non-empty")
        if target_id in self._active or target_id in self._terminal:
            raise ValueError(f"target already registered: {target_id}")
        token = self._root.child(f"target:{target_id}")
        self._active[target_id] = _TargetState(token=token, terminal=anyio.Event())
        return token

    def unregister(self, target_id: str, token: CancellationToken) -> bool:
        """仅移除 identity/token 精确匹配的 active target。"""
        state = self._active.get(target_id)
        if state is None or state.token is not token:
            return False
        self._active.pop(target_id)
        token._detach_from_parent()  # noqa: SLF001
        state.closed_without_terminal = True
        state.terminal.set()
        return True

    def cancel(self, target_id: str) -> bool:
        """只取消一个 active target 及其 child subtree。"""
        state = self._active.get(target_id)
        if state is None:
            return False
        state.token.cancel()
        return True

    def cancel_all(self) -> None:
        """freeze 时显式取消全部 active target，防御 parent edge 损坏。"""
        for state in tuple(self._active.values()):
            state.token.cancel()
            state.closed_without_terminal = True
            state.terminal.set()

    def register_terminal(
        self,
        target_id: str,
        token: CancellationToken,
        *,
        terminal_record_ids: tuple[str, ...],
    ) -> bool:
        """登记 target durable 终态并唤醒等待中的 CancelTurn。"""
        state = self._active.get(target_id)
        if state is None or state.token is not token:
            return False
        if not terminal_record_ids or any(not item for item in terminal_record_ids):
            raise ValueError("terminal_record_ids must be non-empty")
        ids = tuple(terminal_record_ids)
        self._active.pop(target_id)
        token._detach_from_parent()  # noqa: SLF001
        state.terminal_record_ids = ids
        self._terminal[target_id] = ids
        state.terminal.set()
        return True

    def record_outcome(self, target_id: str, end_reason: str) -> bool:
        """在 runner 返回后、writeback 前冻结真实 execution outcome。"""
        state = self._active.get(target_id)
        if state is None:
            return False
        if not end_reason:
            raise ValueError("end_reason must be non-empty")
        if state.execution_end_reason is not None:
            return state.execution_end_reason == end_reason
        state.execution_end_reason = end_reason
        return True

    def outcome(
        self,
        target_id: str,
        token: CancellationToken,
    ) -> str | None:
        """仅对 identity/token 精确匹配的 active target 返回 execution outcome。"""
        state = self._active.get(target_id)
        if state is None or state.token is not token:
            return None
        return state.execution_end_reason

    def was_cancel_requested(
        self,
        target_id: str,
        token: CancellationToken,
    ) -> bool:
        """返回该 active target 是否确由 CancelTurn 请求取消。"""
        state = self._active.get(target_id)
        return (
            state is not None
            and state.token is token
            and state.cancel_requested
        )

    async def resolve(
        self,
        *,
        cancel_submission_id: str,
        target_submission_id: str,
    ) -> TargetCancelResult:
        """取消 active target 并等待终态；重试复用首次稳定裁定。"""
        if not cancel_submission_id or not target_submission_id:
            raise ValueError("cancel and target submission ids must be non-empty")
        resolution = self._resolutions.get(cancel_submission_id)
        if resolution is None:
            resolution = self._start_resolution(target_submission_id)
            self._resolutions[cancel_submission_id] = resolution
        if resolution.result is None:
            assert resolution.target is not None
            await resolution.target.terminal.wait()
            if resolution.result is None:
                target = resolution.target
                resolution.result = (
                    TargetCancelResult(
                        result_status="not_found",
                        terminal_record_ids=(),
                    )
                    if target.closed_without_terminal
                    else TargetCancelResult(
                        result_status="cancelled",
                        terminal_record_ids=target.terminal_record_ids,
                    )
                )
        return self._copy_result(resolution.result)

    def _start_resolution(self, target_id: str) -> _CancelResolution:
        """在首个 await 前原子决定 missing/terminal/active 分支。"""
        terminal_ids = self._terminal.get(target_id)
        if terminal_ids is not None:
            return _CancelResolution(
                target=None,
                result=TargetCancelResult(
                    result_status="already_terminal",
                    terminal_record_ids=terminal_ids,
                ),
            )
        target = self._active.get(target_id)
        if target is None:
            return _CancelResolution(
                target=None,
                result=TargetCancelResult(
                    result_status="not_found",
                    terminal_record_ids=(),
                ),
            )
        target.cancel_requested = True
        target.token.cancel()
        return _CancelResolution(target=target)

    @staticmethod
    def _copy_result(result: TargetCancelResult) -> TargetCancelResult:
        """为每个 caller 重建独立 frozen value。"""
        return TargetCancelResult(
            result_status=result.result_status,
            terminal_record_ids=tuple(result.terminal_record_ids),
        )


class TargetCancellationMixin:
    """向 SessionAuditCoordinator 提供薄 target registry API。"""

    _target_cancellations: TargetCancellationRegistry

    def register_target(self, target_id: str) -> CancellationToken:
        """登记 active turn token；其 child 自动形成目标取消 subtree。"""
        self._raise_if_frozen()  # type: ignore[attr-defined]
        return self._target_cancellations.register(target_id)

    def unregister_target(
        self,
        target_id: str,
        token: CancellationToken,
    ) -> bool:
        """仅当 id/token 对应同一 active turn 时注销。"""
        return self._target_cancellations.unregister(target_id, token)

    def cancel_target(self, target_id: str) -> bool:
        """只取消目标 turn 及其 child subtree。"""
        return self._target_cancellations.cancel(target_id)

    def register_target_terminal(
        self,
        target_id: str,
        token: CancellationToken,
        *,
        terminal_record_ids: tuple[str, ...],
    ) -> bool:
        """登记 target durable 终态与 record ids。"""
        return self._target_cancellations.register_terminal(
            target_id,
            token,
            terminal_record_ids=terminal_record_ids,
        )

    def record_target_outcome(self, target_id: str, end_reason: str) -> bool:
        """在 writeback 前冻结 runner 的真实 end_reason。"""
        return self._target_cancellations.record_outcome(target_id, end_reason)

    def target_outcome(
        self,
        target_id: str,
        token: CancellationToken,
    ) -> str | None:
        """读取仍由指定 token 拥有的 target execution outcome。"""
        return self._target_cancellations.outcome(target_id, token)

    def target_cancel_requested(
        self,
        target_id: str,
        token: CancellationToken,
    ) -> bool:
        """判断 target cancellation 是否来自 CancelTurn。"""
        return self._target_cancellations.was_cancel_requested(target_id, token)

    async def resolve_target_cancel(
        self,
        *,
        cancel_submission_id: str,
        target_submission_id: str,
    ) -> TargetCancelResult:
        """返回 scoped CancelTurn 的稳定 registry 结果。"""
        result = await self._target_cancellations.resolve(
            cancel_submission_id=cancel_submission_id,
            target_submission_id=target_submission_id,
        )
        self._raise_if_frozen()  # type: ignore[attr-defined]
        return result
