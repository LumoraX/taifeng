"""Audited accepted token 的 actor mailbox ownership 协调器。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol

import anyio

from taifeng.loop.audit_support import _accepted_work_ownership_failure

if TYPE_CHECKING:
    from taifeng.loop.audit_admission import (
        AcceptedUserMessage,
        AuditedAdmissionState,
    )

type _ReservationState = Literal[
    "registered",
    "claimed",
    "started",
    "retired",
    "closed",
    "aborted",
]


class AcceptedUserMessageSink(Protocol):
    """accepted token handoff 使用的最小有界 queue 协议。"""

    async def put(self, item: AcceptedUserMessage) -> None:
        """等待 queue 明确取得 token。"""


class AuditedApplicationCheckpoint:
    """actor 等待 audited application 成功或精确失败的一次性握手。"""

    def __init__(self) -> None:
        """创建 result-bearing future，并确保无人等待时也检索异常。"""
        self._future: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        self._future.add_done_callback(self._consume_terminal_exception)

    async def wait(self) -> None:
        """等待 application 结果；失败时传播原始 BaseException 对象。"""
        await self._future

    def succeed(self) -> bool:
        """只允许首个 terminal result 发布 application success。"""
        if self._future.done():
            return False
        self._future.set_result(None)
        return True

    def fail(self, error: BaseException) -> bool:
        """只允许首个 terminal result 发布原始 application failure。"""
        if self._future.done():
            return False
        self._future.set_exception(error)
        return True

    @staticmethod
    def _consume_terminal_exception(future: asyncio.Future[None]) -> None:
        """检索无人等待的异常；正常 await 仍会传播同一对象。"""
        if not future.cancelled():
            future.exception()


class _MailboxClosedError(RuntimeError):
    """actor mailbox 已关闭，不能再取得 accepted token。"""


class _MailboxReservation:
    """跨 register/put/claim/finalizer 的单 token ownership 状态。"""

    __slots__ = ("put_scope", "state", "token")

    def __init__(self, token: AcceptedUserMessage) -> None:
        """初始化 caller-owned registered reservation。"""
        self.token = token
        self.state: _ReservationState = "registered"
        self.put_scope: anyio.CancelScope | None = None


class AuditedSubmissionMailbox:
    """原子协调 accepted token 的 put、actor claim 与 actor finalizer。"""

    def __init__(self) -> None:
        """初始化 open mailbox 与空 reservation 表。"""
        self._lock = asyncio.Lock()
        self._closed = False
        self._reservations: dict[str, _MailboxReservation] = {}

    async def put_token(
        self,
        sink: AcceptedUserMessageSink,
        token: AcceptedUserMessage,
    ) -> tuple[BaseException | None, bool]:
        """register-before-put；返回失败与 caller 是否仍应退休 token。"""
        reservation: _MailboxReservation | None = None
        try:
            reservation = await self._register(token)
            if reservation is None:
                return _MailboxClosedError(), True
            scope = anyio.CancelScope()
            if not await self._attach_scope(reservation, scope):
                return _MailboxClosedError(), False
            error: BaseException | None = None
            with scope:
                try:
                    await sink.put(token)
                except BaseException as caught:  # noqa: BLE001
                    error = caught
            if scope.cancel_called:
                return _MailboxClosedError(), False
            if error is not None:
                return error, await self._abort(reservation)
            if await self._put_remains_valid(reservation):
                return None, False
            return _MailboxClosedError(), False
        except BaseException as error:  # noqa: BLE001
            if reservation is None:
                return error, True
            return error, await self._abort(reservation)

    async def claim(self, token: AcceptedUserMessage) -> bool:
        """actor dequeue 后标记 claimed，ownership 保留到 child start handshake。"""
        async with self._lock:
            reservation = self._reservations.get(token.submission_id)
            if (
                self._closed
                or reservation is None
                or reservation.token is not token
                or reservation.state != "registered"
            ):
                return False
            reservation.state = "claimed"
            return True

    async def start_claimed(self, token: AcceptedUserMessage) -> bool:
        """child outer finally 安装后原子取得唯一 retirement ownership。"""
        async with self._lock:
            reservation = self._reservations.get(token.submission_id)
            if (
                self._closed
                or reservation is None
                or reservation.token is not token
                or reservation.state != "claimed"
            ):
                return False
            reservation.state = "started"
            return True

    async def take_started_retirement(self, token: AcceptedUserMessage) -> bool:
        """operation finally 原子取得 started token 的实际 retirement 权。"""
        with anyio.CancelScope(shield=True):
            async with self._lock:
                reservation = self._reservations.get(token.submission_id)
                if (
                    reservation is None
                    or reservation.token is not token
                    or reservation.state != "started"
                ):
                    return False
                reservation.state = "retired"
                self._reservations.pop(token.submission_id, None)
                return True
        return False

    async def close(self) -> tuple[AcceptedUserMessage, ...]:
        """关闭 mailbox，接管 registered/claimed，保留 started 给 operation。"""
        async with self._lock:
            if self._closed:
                return ()
            self._closed = True
            owned: list[AcceptedUserMessage] = []
            for submission_id, reservation in tuple(self._reservations.items()):
                if reservation.state == "started":
                    continue
                self._reservations.pop(submission_id, None)
                reservation.state = "closed"
                if reservation.put_scope is not None:
                    reservation.put_scope.cancel()
                owned.append(reservation.token)
            return tuple(owned)

    async def _register(
        self,
        token: AcceptedUserMessage,
    ) -> _MailboxReservation | None:
        """put 前登记唯一 token；closed 时不取得 caller ownership。"""
        async with self._lock:
            if self._closed:
                return None
            if token.submission_id in self._reservations:
                raise ValueError(f"mailbox token already registered: {token.submission_id}")
            reservation = _MailboxReservation(token)
            self._reservations[token.submission_id] = reservation
            return reservation

    async def _attach_scope(
        self,
        reservation: _MailboxReservation,
        scope: anyio.CancelScope,
    ) -> bool:
        """登记可由 finalizer 取消的 put scope。"""
        async with self._lock:
            if self._closed or reservation.state != "registered":
                return False
            reservation.put_scope = scope
            return True

    async def _abort(self, reservation: _MailboxReservation) -> bool:
        """失败方仅在 ownership 未转交/未被 finalizer 接管时退休。"""
        with anyio.CancelScope(shield=True):
            async with self._lock:
                if reservation.state != "registered":
                    return False
                reservation.state = "aborted"
                current = self._reservations.get(reservation.token.submission_id)
                if current is reservation:
                    self._reservations.pop(reservation.token.submission_id, None)
                return True
        return False

    async def _put_remains_valid(self, reservation: _MailboxReservation) -> bool:
        """put 返回后接受 mailbox/operation 已确定取得过 ownership。"""
        async with self._lock:
            return reservation.state in (
                "registered",
                "claimed",
                "started",
                "retired",
            )


async def _retire_tokens(
    tokens: tuple[AcceptedUserMessage, ...],
) -> None:
    """cancellation-independent 退休一组未转交 accepted work。"""
    async def retire_all() -> None:
        """在独立 task 内完整退休 ownership，避免 caller 取消截断首个 await。"""
        with anyio.CancelScope(shield=True):
            for token in tokens:
                await token.accepted_work.complete()

    worker = asyncio.create_task(retire_all())
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    worker.result()
    if cancellation is not None:
        raise cancellation


async def retire_started_audited_token(
    mailbox: AuditedSubmissionMailbox,
    token: AcceptedUserMessage,
) -> None:
    """operation finally 仅在 handshake 胜出时 cancellation-independent 退休。"""
    async def retire_if_owned() -> None:
        """在独立 task 内仲裁并释放 started ownership。"""
        with anyio.CancelScope(shield=True):
            if await mailbox.take_started_retirement(token):
                await token.accepted_work.complete()

    worker = asyncio.create_task(retire_if_owned())
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    worker.result()
    if cancellation is not None:
        raise cancellation


async def handoff_accepted_user_message(
    state: AuditedAdmissionState,
    mailbox: AuditedSubmissionMailbox,
    sink: AcceptedUserMessageSink,
    token: AcceptedUserMessage,
) -> None:
    """把 definite accepted token 交给 mailbox；失败时精确冻结/退休。"""
    try:
        await state.coordinator.ensure_effect_allowed()
    except BaseException as cause:
        frozen = state.coordinator.freeze(_accepted_work_ownership_failure())
        await _retire_tokens((token,))
        if isinstance(
            cause,
            (KeyboardInterrupt, SystemExit, anyio.get_cancelled_exc_class()),
        ):
            raise
        raise frozen from None
    handoff_error, caller_owns = await mailbox.put_token(sink, token)
    if handoff_error is None:
        return
    frozen = state.coordinator.freeze(_accepted_work_ownership_failure())
    if caller_owns:
        await _retire_tokens((token,))
    if isinstance(
        handoff_error,
        (KeyboardInterrupt, SystemExit, anyio.get_cancelled_exc_class()),
    ):
        raise handoff_error
    raise frozen from None


async def finalize_audited_mailbox(
    state: AuditedAdmissionState,
    mailbox: AuditedSubmissionMailbox,
) -> None:
    """actor 终止时冻结并退休所有未 start token。"""
    with anyio.CancelScope(shield=True):
        tokens = await mailbox.close()
        if not tokens:
            return
        state.coordinator.freeze(_accepted_work_ownership_failure())
        await _retire_tokens(tokens)


__all__ = [
    "AcceptedUserMessageSink",
    "AuditedApplicationCheckpoint",
    "AuditedSubmissionMailbox",
    "finalize_audited_mailbox",
    "handoff_accepted_user_message",
    "retire_started_audited_token",
]
