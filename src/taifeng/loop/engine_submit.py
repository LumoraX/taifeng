"""AgentEngine 的提交与订阅入口（submit / subscribe / shutdown / instructions）。

方法体从 ``engine.py`` 原样搬出（W7.1 拆文件，零行为变更）；``AgentEngine`` 类里按原名赋值。
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from taifeng.instructions.types import InstructionContext
from taifeng.llm.errors import LLMError
from taifeng.loop.attachment_parts import admit_user_attachments
from taifeng.loop.audit_admission import (
    AcceptedUserMessage,
    AuditedUserMessageSubmission,
    InvalidAuditedSubmissionError,
    UnsupportedAuditedOperationError,
    admit_user_message,
    prepare_user_message,
    reject_invalid_user_message,
    reject_unsupported_audited_op,
    user_message_input_descriptor_hash,
)
from taifeng.loop.audit_cancel import AuditedCancelTurnSubmission, apply_cancel_turn
from taifeng.loop.audit_lifecycle import SessionLifecycle
from taifeng.loop.audit_mailbox import handoff_accepted_user_message
from taifeng.loop.audit_shutdown import shutdown_submission, submit_audited_shutdown
from taifeng.loop.audit_support import AuditHealth
from taifeng.loop.audit_suspension import submit_audited_resume
from taifeng.loop.engine_types import _TERMINAL_KINDS, _Subscriber
from taifeng.loop.engine_types import DeliveredEvent as DeliveredEvent
from taifeng.loop.event import EngineLog, EventMsg
from taifeng.loop.submission import CancelTurn, Op, Resume, Shutdown, Submission, UserMessage

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.loop.engine import AgentEngine


async def submit(self: AgentEngine, op: Op) -> str:
    """业务侧入队接口。返回 submission_id。"""
    sub = Submission(op=op)
    if self._audit_state is not None and isinstance(sub.op, UserMessage):
        state = self._audit_state
        state.coordinator.ensure_intake_open()
        descriptor_hash = user_message_input_descriptor_hash(sub)
        prepared = None
        with suppress(TypeError, ValueError, LLMError):
            from taifeng.llm.client import model_capabilities

            prepared = prepare_user_message(
                state,
                sub,
                image_input_policy=self._image_input_policy,
                model_input_capabilities=model_capabilities(self._model_client),
                file_input_policy=self._file_input_policy,
            )
        if prepared is None:
            async with self._audited_admission_lock:
                await reject_invalid_user_message(
                    state,
                    submission_id=sub.id,
                    descriptor_hash=descriptor_hash,
                )
            raise InvalidAuditedSubmissionError(
                sub.id,
                descriptor_hash,
            ) from None
        async with self._audited_admission_lock:
            accepted = prepared.accept(self._next_audited_turn_index)
            return await self._submit_audited_user_message_locked(accepted)
    if self._audit_state is not None and isinstance(sub.op, CancelTurn):
        return await self._submit_audited_cancel_turn(sub)
    if self._audit_state is not None and isinstance(sub.op, Resume):
        return await submit_audited_resume(self, sub)
    if self._audit_state is not None and isinstance(sub.op, Shutdown):
        return await submit_audited_shutdown(
            self._audit_state, sub, self._audited_admission_lock, self._audit_finish_owner)
    if self._audit_state is not None:
        # audit 动态门：仅 UserMessage/CancelTurn/Shutdown 允许；能力面外的 Op
        # 在执行前 durable 安全拒绝，不入队、不执行（spec 动态未支持操作）。
        async with self._audited_admission_lock:
            await reject_unsupported_audited_op(self._audit_state, sub)
        raise UnsupportedAuditedOperationError(sub.id, str(sub.op.kind))
    if isinstance(sub.op, UserMessage):
        # legacy path 在 enqueue 与 durable append 前完成图片 / 文件准入。
        admit_user_attachments(
            sub.op.attachments,
            image_input_policy=self._image_input_policy,
            file_input_policy=self._file_input_policy,
            model_client=self._model_client,
        )
    await self._submissions.put(sub)
    return sub.id


async def _submit_audited_cancel_turn(self: AgentEngine, sub: Submission) -> str:
    """healthy 时 durable 收敛 CancelTurn；frozen 时仅安全取消。"""
    assert self._audit_state is not None
    assert isinstance(sub.op, CancelTurn)
    state = self._audit_state
    if state.coordinator.health is AuditHealth.RECOVERY_REQUIRED:
        state.coordinator.cancel_target(sub.op.submission_id)
        await self._emit_cancel_turn_log(
            sub.id,
            sub.op.submission_id,
            result_status="safe_degraded",
        )
        return sub.id
    state.coordinator.ensure_intake_open()
    submission = AuditedCancelTurnSubmission(
        submission_id=sub.id,
        target_submission_id=sub.op.submission_id,
    )
    result = await apply_cancel_turn(
        state,
        submission,
        self._audited_admission_lock,
    )
    await self._emit_cancel_turn_log(
        sub.id,
        sub.op.submission_id,
        result_status=result.result_status,
    )
    return sub.id


async def _emit_cancel_turn_log(self: AgentEngine,
    cancel_submission_id: str,
    target_submission_id: str,
    *,
    result_status: str,
) -> None:
    """通过既有 EventMsg 通道投影 CancelTurn 结果。"""
    await self._emit(
        EventMsg(
            submission_id=cancel_submission_id,
            msg=EngineLog(
                data={
                    "level": "info",
                    "message": f"cancel turn result: {result_status}",
                    "extra": {"target_submission_id": target_submission_id},
                }
            ),
        )
    )


async def _submit_audited_user_message(self: AgentEngine,
    sub: AuditedUserMessageSubmission,
) -> str:
    """提交审计专用 frozen submission；historical receipt 不入队。"""
    async with self._audited_admission_lock:
        return await self._submit_audited_user_message_locked(sub)


async def _submit_audited_user_message_locked(self: AgentEngine,
    sub: AuditedUserMessageSubmission,
) -> str:
    """在单一 admission 顺序点 durable accept，并推进下一 index。"""
    assert self._audit_state is not None
    admission = await admit_user_message(self._audit_state, sub)
    if isinstance(admission, AcceptedUserMessage):
        self._next_audited_turn_index = max(
            self._next_audited_turn_index,
            sub.accepted_turn_index + 1,
        )
        await handoff_accepted_user_message(
            self._audit_state,
            self._audited_mailbox,
            self._submissions,
            admission,
        )
    return sub.id


def _new_subscriber(self: AgentEngine) -> _Subscriber:
    """按当前队列容量/水位配置新建一个订阅者。"""
    return _Subscriber(
        maxsize=self._event_queue_size,
        high_ratio=self._event_high_water_ratio,
        low_ratio=self._event_low_water_ratio,
    )


async def subscribe_all_envelopes(self: AgentEngine) -> AsyncIterator[DeliveredEvent]:
    """订阅本 engine 的全部事件（firehose），产出带 ``delivery_seq`` 的信封。

    审计可观测 层1：消费者凭 ``delivery_seq`` 从 0 起的连续性自检**自己**漏没漏
    （含「刚订阅就被丢弃」的窗口）；凭 ``event.seq`` 做全局连续性 + 组落库键。
    """
    sub = self._new_subscriber()
    self._all_subs.append(sub)
    try:
        while True:
            env = await sub.queue.get()
            yield env
            if env.event.msg.kind == "shutdown":
                return
    finally:
        with suppress(ValueError):
            self._all_subs.remove(sub)


async def subscribe_all(self: AgentEngine) -> AsyncIterator[EventMsg]:
    """订阅本 engine 的全部事件（向后兼容：产出裸 ``EventMsg``）。

    需 per-subscriber 投递序号自检时改用 ``subscribe_all_envelopes``。
    """
    async for env in self.subscribe_all_envelopes():
        yield env.event


async def subscribe_envelopes(self: AgentEngine, submission_id: str
) -> AsyncIterator[DeliveredEvent]:
    """订阅指定 submission 的事件，产出带 ``delivery_seq`` 的信封。

    ⚠️ 过滤订阅只收一个 submission 的事件，全局 ``event.seq`` 天然跳号（=过滤，
    非丢弃）；要自检自己的丢弃**必须**看 ``delivery_seq`` 跳号。
    """
    # 已终态 → 立即补投真实终结事件并收尾（ADR 0031）。必须早于订阅登记：
    # 补投路径不占用 per-submission 订阅位，也就不会挤掉在线订阅者。
    recorded = self._terminal_replay.get(submission_id)
    if recorded is not None:
        yield DeliveredEvent(event=recorded, delivery_seq=0)
        return
    sub = self._new_subscriber()
    self._event_subs[submission_id] = sub
    try:
        if self._closed:
            # engine 已收敛完毕、终结事件早已投完：晚到的订阅者直接拿合成终结
            await self._emit_operation_terminal(
                submission_id, None, kind="engine_shutdown",
            )
        while True:
            env = await sub.queue.get()
            if env.event.submission_id != submission_id:
                continue
            yield env
            # turn_suspended 是独立终结态(turn 已结束，等待 Resume)——必须纳入自动
            # 终止集合，否则 turn 挂起时消费者的 async for 永远拿不到终结信号、卡死，
            # 业务也无法释放实例并提交 Resume(Task 16 回归根因)。
            if env.event.msg.kind in _TERMINAL_KINDS:
                return
    finally:
        self._event_subs.pop(submission_id, None)


async def subscribe(self: AgentEngine, submission_id: str) -> AsyncIterator[EventMsg]:
    """订阅指定 submission 的事件（向后兼容：产出裸 ``EventMsg``）。完成后自动结束。

    需 per-subscriber 投递序号自检时改用 ``subscribe_envelopes``。
    """
    async for env in self.subscribe_envelopes(submission_id):
        yield env.event


async def shutdown(self: AgentEngine) -> None:
    """请求 actor 收敛；audit path 先与 admission 串行关闭 intake。"""
    if self._audit_state is None:
        await self.submit(Shutdown())
        return
    async with self._audited_admission_lock:
        lifecycle = await self._audit_state.coordinator.close_intake()
        if lifecycle is SessionLifecycle.CLOSED or self._audited_shutdown_enqueued:
            return
        await self._submissions.put(shutdown_submission(self._audit_state))
        self._audited_shutdown_enqueued = True


async def _instruction_emit_bridge(self: AgentEngine, kind: str, data: dict[str, Any],
) -> None:
    """resolver 用的 emit 回调：把 (kind, data) 包成 EventMsg 投递。"""
    msg_cls = self._INSTRUCTION_KIND_TO_MSG.get(kind)
    if msg_cls is None:
        logger.warning("unknown instruction event kind: %s", kind)
        return
    ev = EventMsg(
        submission_id=self._current_emit_submission_id,
        msg=msg_cls(data=data),
    )
    await self._emit(ev)


async def warmup_engine_scope(self: AgentEngine) -> None:
    """启动期解析 engine scope 的层（EnginePool.create 之后业务侧调）。

    无 resolver 时 no-op。失败时 fail-fast（raise InstructionFetchError）。
    """
    if self._instruction_resolver is None:
        return
    if not self._instruction_resolver.has_scope("engine"):
        return
    ctx = InstructionContext(
        session_id=self._session_id,
        thread_id=self._thread_id,
        entry_skill_id=self._entry_skill.id,
        turn_index=0,
        metadata=self._request_metadata,
        cancel=None,
    )
    self._engine_scope_resolved = await self._instruction_resolver.resolve(
        "engine", ctx,
    )
