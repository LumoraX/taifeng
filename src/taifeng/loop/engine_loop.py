"""AgentEngine 的 actor 主循环与审计 turn 的应用 / 运行。

方法体从 ``engine.py`` 原样搬出（W7.1 拆文件，零行为变更）；``AgentEngine`` 类里按原名赋值。
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from taifeng.conversation.models import ResponseItem, system_injection, user_message
from taifeng.conversation.origin import tag_origin
from taifeng.loop import engine_ops, engine_prewarm
from taifeng.loop.audit_admission import AcceptedUserMessage, commit_accepted_application
from taifeng.loop.audit_cancel import finalize_cancelled_target
from taifeng.loop.audit_llm import AuditedTurnInput
from taifeng.loop.audit_mailbox import AuditedApplicationCheckpoint, retire_started_audited_token
from taifeng.loop.audit_support import _await_owned as audit_await_owned
from taifeng.loop.cancellation import CancelReason
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.event import EngineLog, EventMsg, UserInputInjected
from taifeng.loop.event import Shutdown as ShutdownMsg
from taifeng.loop.submission import (
    CancelTurn,
    CompactNow,
    InjectSystemMessage,
    InjectUserInput,
    Prewarm,
    RefreshSnapshot,
    Resume,
    Rewind,
    SendToPeer,
    Shutdown,
    Submission,
    ThreadRollback,
    UpdateBudget,
    UpdateInstructions,
    UserMessage,
)

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine


async def run(self: AgentEngine, cancel: CancellationToken) -> None:
    self._running = True
    shutdown_requested = False
    # detached-spawn：记下根取消 token，供 spawn 的分离 task 派生子 token（R4 可取消）。
    self._root_cancel = cancel
    # suspension-ttl 冷重武装(R5,根段):装载的历史里有带 ttl 的活跃挂起 →
    # 已过期立即裁决、未过期按剩余时长武装。此刻 spawn 句柄表尚未重建
    # (rebuild 要等本方法赋值 _root_cancel 后才跑),挂起态 spawn 子 thread
    # 的重武装由 _rebuild_spawn_state_from_history 收尾时完成。
    try:
        await self._rearm_ttl_timers_cold()
        while self._running:
            if cancel.is_cancelled:
                break
            try:
                sub = await asyncio.wait_for(self._submissions.get(), timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(sub.op, Shutdown):
                self._running = False
                shutdown_requested = True
                # suspension-ttl:取消全部到期定时器(R4,定时器挂 engine 生命周期)
                self._cancel_ttl_timers()
                await self._emit(
                    EventMsg(submission_id=sub.id, msg=ShutdownMsg())
                )
                break
            if isinstance(sub.op, CancelTurn):
                target = self._pending.get(sub.op.submission_id)
                if target is not None:
                    target.cancel.cancel(CancelReason.REQUESTED, "cancel_turn")
                    await self._emit(
                        EventMsg(
                            submission_id=sub.id,
                            msg=EngineLog(
                                data={
                                    "level": "info",
                                    "message": f"cancelled turn {sub.op.submission_id}",
                                    "extra": {},
                                }
                            ),
                        )
                    )
                else:
                    # R4：挂起态没有 live pending（turn 已退栈），CancelTurn 需
                    # 按 submission_id 匹配并清除活跃挂起 record（闭环可取消）。
                    await self._cancel_active_suspension(
                        sub.id, sub.op.submission_id
                    )
                continue
            if isinstance(sub.op, InjectSystemMessage):
                item = tag_origin(system_injection(
                    sub.op.text, thread_id=self._thread_id, source=sub.op.source
                ), sub.op.origin)
                active = self._active_root_pending()
                if active is not None:
                    # 在飞期间 root history 只有 runner 一个写者（ADR 0029）：
                    # 走与 InjectUserInput 同一 pending 队列，runner 迭代边界落
                    # buffer + store；否则 engine 直写会被 turn 结束的回写覆盖。
                    active.pending_input.append(item)
                else:
                    self._history.append(item)
                    await self._store.append(item)
                continue
            if isinstance(sub.op, SendToPeer):
                # peer-mailbox：与 send_message 工具收敛到同一投递路径。
                # 寻址失败 / TriggerTurn 打 root → EngineLog 告警（显式，不静默）。
                try:
                    await self.deliver_peer_message(
                        target=sub.op.target_thread_id,
                        text=sub.op.text,
                        mode=sub.op.mode,
                        from_thread_id=sub.op.from_thread_id, origin=sub.op.origin,
                        submission_id=sub.id,
                    )
                except ValueError as e:
                    await self._emit(
                        EventMsg(
                            submission_id=sub.id,
                            msg=EngineLog(data={
                                "level": "warning",
                                "message": f"send_to_peer 投递失败: {e}",
                                "extra": {
                                    "target": sub.op.target_thread_id,
                                    "mode": sub.op.mode,
                                },
                            }),
                        )
                    )
                continue
            if isinstance(sub.op, InjectUserInput):
                # B1 midturn-input-steering：投进活跃 turn 的 pending 队列（下一
                # 迭代边界 drain 并入）；无活跃 turn → 落历史不起新 turn。
                target = self._pending.get(sub.op.submission_id)
                item = tag_origin(
                    user_message(sub.op.text, thread_id=self._thread_id), sub.op.origin)
                if target is not None:
                    # 活跃 turn：入共享队列，由 runner drain 时落 store + emit
                    target.pending_input.append(item)
                    delivered = True
                else:
                    # 无活跃 turn：engine 落历史 + 持久化，不创建 TurnRunner
                    self._history.append(item)
                    await self._store.append(item)
                    delivered = False
                await self._emit(
                    EventMsg(
                        submission_id=sub.id,
                        msg=UserInputInjected(
                            data={
                                "submission_id": sub.op.submission_id,
                                "delivered": delivered,
                                "text_preview": sub.op.text[:80],
                            }
                        ),
                    )
                )
                continue
            if isinstance(sub.op, CompactNow):
                # manual 压缩 = 一次 LLM 调用：不能内联在 actor 循环里（会饿死
                # CancelTurn / Shutdown），且改写 root history 须持 root gate
                op_compact = sub.op
                self._start_operation(
                    self._run_gated_op(
                        sub.id, cancel,
                        partial(self._run_compact_now, sub.id, op_compact),
                    ),
                    name=f"compact:{sub.id}",
                    submission_id=sub.id,
                )
                continue
            if isinstance(sub.op, ThreadRollback):
                num_turns = sub.op.num_turns
                self._start_operation(
                    self._run_gated_op(
                        sub.id, cancel,
                        # 这条不能用 partial:handle_rollback 不接收 token,
                        # 而 run 的签名必带一个 —— lambda 在此是"丢弃末位参数"的
                        # 适配器。默认参数绑定会让 mypy 推不出 lambda 类型,故 ignore。
                        lambda _tok, sid=sub.id, n=num_turns: (  # type: ignore[misc]
                            engine_ops.handle_rollback(self, sid, n)
                        ),
                    ),
                    name=f"rollback:{sub.id}",
                    submission_id=sub.id,
                )
                continue
            if isinstance(sub.op, Prewarm):
                self._start_operation(
                    engine_prewarm.run_prewarm(self, sub.id, sub.op, cancel),
                    name=f"prewarm:{sub.id}", submission_id=sub.id,
                )
                continue
            if isinstance(sub.op, UpdateBudget):
                engine_ops.handle_update_budget(self, sub.id, sub.op)
                continue
            if isinstance(sub.op, RefreshSnapshot):
                engine_ops.handle_refresh_snapshot(self, sub.id)
                continue
            if isinstance(sub.op, UpdateInstructions):
                await engine_ops.handle_update_instructions(self, sub.id, sub.op)
                continue
            if isinstance(sub.op, Rewind):
                # 与 Resume 同理用 create_task：重推会跑完整 turn(采样 + 派发),
                # 不阻塞主 run 循环,且给 subscribe(submission_id) 留注册窗口。
                # 根 thread 的 rewind 改写 root history → 持 root gate；子 thread
                # rewind 作用于 child thread，不排队（其一致性归 wave2b）。
                is_root_rewind = (
                    sub.op.thread_id is None or sub.op.thread_id == self._thread_id
                )
                rewind_sub = sub
                body = (
                    self._run_gated_op(
                        sub.id, cancel,
                        partial(engine_ops.handle_rewind, self, rewind_sub),
                    )
                    if is_root_rewind
                    else engine_ops.handle_rewind(self, sub, cancel)
                )
                self._start_operation(
                    body, name=f"rewind:{sub.id}", submission_id=sub.id,
                )
                continue
            if isinstance(sub.op, Resume):
                # detached spawn 续跑优先判定：Resume.thread_id 命中某个【挂起】的
                # spawn 句柄 child_thread_id → 走 _resume_spawn（在该 child thread
                # 自己的线上独立续跑，与父 turn 完全解耦；父 turn 早已结束）。
                # 不命中（根 thread / call_skill 子链）→ 维持既有 _handle_resume。
                spawn_handle = self._match_suspended_spawn(sub.op.thread_id)
                if spawn_handle is not None:
                    self._start_operation(
                        self._resume_spawn(sub, spawn_handle),
                        name=f"resume-spawn:{sub.id}",
                        submission_id=sub.id,
                    )
                    continue
                # 与 UserMessage 一致用 create_task 异步派发：让续跑链（可能跨子/根
                # 多个 turn）不阻塞主 run 循环，且给 subscribe(submission_id) 留出在
                # 事件流出前注册队列的窗口（子 thread resume 续跑链 emit 多个事件，
                # 内联执行会与"submit 后再 subscribe"的消费者抢跑导致丢首批事件→挂死）。
                # 根 / call_skill 子链续跑最终都回写 root history → 持 root gate
                resume_sub = sub
                self._start_operation(
                    self._run_gated_op(
                        sub.id, cancel,
                        partial(self._handle_resume, resume_sub),
                    ),
                    name=f"resume:{sub.id}",
                    submission_id=sub.id,
                )
                continue
            if self._is_queued_user_message(sub):
                await self._start_queued_user_message(sub, cancel)
                continue
    finally:
        await self._finalize_run_lifecycle(
            cancel,
            shutdown_requested=shutdown_requested,
        )


def _is_queued_user_message(
    sub: Submission | AcceptedUserMessage,
) -> bool:
    """统一识别 legacy UserMessage 与 durable accepted token。"""
    return isinstance(sub, AcceptedUserMessage) or isinstance(sub.op, UserMessage)


async def _start_queued_user_message(self: AgentEngine,
    sub: Submission | AcceptedUserMessage,
    root_cancel: CancellationToken,
) -> None:
    """按 queue item 类型选择 durable 或 legacy turn 入口。"""
    if not isinstance(sub, AcceptedUserMessage):
        self._start_operation(
            self._run_turn_for(sub, root_cancel),
            name=f"turn:{sub.id}",
            submission_id=sub.id,
        )
        return
    if not await self._audited_mailbox.claim(sub):
        return
    application_checkpoint = AuditedApplicationCheckpoint()
    self._start_operation(
        self._run_claimed_audited_turn(
            sub,
            root_cancel,
            application_checkpoint=application_checkpoint,
        ),
        name=f"turn:{sub.id}",
        submission_id=sub.id,
    )
    await application_checkpoint.wait()


async def _run_claimed_audited_turn(self: AgentEngine,
    token: AcceptedUserMessage,
    root_cancel: CancellationToken,
    *,
    application_checkpoint: AuditedApplicationCheckpoint | None = None,
) -> None:
    """handshake 后收敛 application；失败由 actor checkpoint 单点传播。"""
    failure: BaseException | None = None
    try:
        if not await self._audited_mailbox.start_claimed(token):
            return
        try:
            await self._run_audited_turn_for(
                token,
                root_cancel,
                application_checkpoint=application_checkpoint,
            )
        except BaseException as error:  # noqa: BLE001
            failure = error
    finally:
        await retire_started_audited_token(
            self._audited_mailbox,
            token,
        )
    if failure is None:
        return
    if (
        application_checkpoint is not None
        and application_checkpoint.fail(failure)
    ):
        return
    raise failure


async def _run_audited_turn_for(self: AgentEngine,
    token: AcceptedUserMessage,
    root_cancel: CancellationToken,
    *,
    application_checkpoint: AuditedApplicationCheckpoint | None = None,
) -> None:
    """应用 ack conversation envelope；ownership 由外层 handshake/finally 管理。"""
    assert self._audit_state is not None
    await self._audit_state.coordinator.ensure_effect_allowed()
    try:
        item = token.validated_application()
    except BaseException as error:
        raise self._audit_state.coordinator.freeze(error) from None
    # ADR 0029 / 0101：accepted item 的 application（对话项落账 + 进 history + 投影）在本
    # token 拿到 root gate 时进行——Journal 顺序 = transcript 顺序 = 执行顺序，在飞 turn 的
    # prompt 确定不含排队消息。accept 本身（durable 准入记录）已在 submit 时落盘。
    # 排队前登记 _pending（gate token），CancelTurn 可取消排队；engine 收敛（raw
    # cancel）时对仍排队的 token「只应用不跑 turn」，满足 release 等 application 收敛。
    gate_cancel = root_cancel.child(f"sub:{token.submission_id}:gate")
    self._pending[token.submission_id] = _PendingTurn(
        token.submission_id, gate_cancel, token.accepted_turn_index,
    )
    # actor 握手语义 = 「交接完成」：gate 空闲时等 application 收敛（原语义）；
    # gate 被占时登记排队即交接完成，actor 可出队下一个 token——否则 actor 会
    # 永远等在排队 token 的 application 上（它要等 gate）。排队 token 之后的
    # application 失败走 operation 自己的 freeze / 终结路径。
    if self._root_gate.locked() and application_checkpoint is not None:
        application_checkpoint.succeed()
    raw_cancel: asyncio.CancelledError | None = None
    try:
        acquired = await self._acquire_root_gate(token.submission_id, gate_cancel)
    except asyncio.CancelledError as error:
        acquired = False
        raw_cancel = error
    if not acquired:
        # 取消（CancelTurn 或 engine 收敛）：accepted 是 durable 承诺，仍要应用
        self._pending.pop(token.submission_id, None)
        await self._apply_accepted_item_owned(token, item, application_checkpoint)
        await self._emit_operation_terminal(
            token.submission_id, None,
            kind="engine_shutdown" if raw_cancel is not None else "cancelled",
        )
        if raw_cancel is not None:
            raise raw_cancel
        return
    try:
        await self._apply_accepted_item(token, item, application_checkpoint)
        target_cancel = self._audit_state.coordinator.register_target(
            token.submission_id
        )
        await self._run_audited_target(token, item, target_cancel)
    finally:
        self._release_root_gate()


async def _apply_accepted_item_owned(self: AgentEngine,
    token: AcceptedUserMessage,
    item: ResponseItem,
    application_checkpoint: AuditedApplicationCheckpoint | None,
) -> None:
    """application 作为 coordinator-owned 步骤执行：caller 的 raw cancel 只能延迟重抛。"""
    _, cancellation = await audit_await_owned(
        self._apply_accepted_item(token, item, application_checkpoint),
        name=f"apply-accepted:{token.submission_id}",
    )
    if cancellation is not None:
        raise cancellation


async def _apply_accepted_item(self: AgentEngine,
    token: AcceptedUserMessage,
    item: ResponseItem,
    application_checkpoint: AuditedApplicationCheckpoint | None,
) -> None:
    """accepted user item 落账 → 进 hot history → 投影（ADR 0025 / 0101 application）。

    落账与取消无关（raw cancel 延迟到写完再抛）；投影可以被取消。
    """
    state = self._audit_state
    assert state is not None
    (ack, envelope), cancellation = await audit_await_owned(
        commit_accepted_application(state, token, item),
        name=f"commit-accepted:{token.submission_id}",
    )
    if cancellation is not None:
        raise cancellation
    async with self._lock:
        self._history.append(item)
    try:
        result = await state.projector.project((envelope,), ack)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001  # 普通未分类异常必须 fail closed
        raise state.coordinator.freeze(error) from None
    state.coordinator.update_projection(result)
    if application_checkpoint is not None:
        application_checkpoint.succeed()


async def _run_audited_target(self: AgentEngine,
    token: AcceptedUserMessage,
    item: ResponseItem,
    target_cancel: CancellationToken,
) -> None:
    """已持 root gate：跑 audited 根 turn 并收敛 target 终态。"""
    assert self._audit_state is not None
    try:
        await self._run_turn_for(
            AuditedTurnInput(
                id=token.submission_id,
                text=str(item.payload["text"]),
                accepted_turn_index=token.accepted_turn_index,
            ),
            target_cancel,
            gate_held=True,
        )
        end_reason = self._audit_state.coordinator.target_outcome(
            token.submission_id,
            target_cancel,
        )
        if (
            end_reason == "cancelled"
            and self._audit_state.coordinator.target_cancel_requested(
                token.submission_id,
                target_cancel,
            )
        ):
            await finalize_cancelled_target(
                self._audit_state,
                submission_id=token.submission_id,
                turn_index=token.accepted_turn_index,
                target_token=target_cancel,
            )
    finally:
        self._audit_state.coordinator.unregister_target(
            token.submission_id,
            target_cancel,
        )
