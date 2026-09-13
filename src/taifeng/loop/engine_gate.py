"""engine 根 gate 与 turn 派发：根闸获取释放 / 受闸 op / turn 执行 / 会话 token 天花板 / 挂起闸

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `engine-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 engine 唯一持有。

**兄弟调用一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，兄弟模块
与测试按原名调用/打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import user_message
from taifeng.instructions.source import InstructionFetchError
from taifeng.instructions.types import InstructionContext, ResolvedInstruction
from taifeng.loop.audit_llm import AuditedTurnInput, audited_turn_index
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.event import (
    EventMsg,
    PreTurnHookDenied,
    ResourceLimitExceeded,
    SubmissionQueued,
    TurnFailed,
    TurnSuspended,
)
from taifeng.loop.submission import Submission, UserMessage

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine


class EngineGate:
    """engine 根 gate 与 turn 派发协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供运行态与共享依赖。
        """
        self._engine = engine

    async def acquire_root_gate(
        self, submission_id: str, cancel: CancellationToken,
    ) -> bool:
        """排队获取 root gate；gate 被占时 emit submission_queued，排队中被取消返回 False。

        `asyncio.Lock` 是 FIFO：提交序即执行序。与 cancel token 竞速——CancelTurn 命中
        排队中的 submission（_pending 已登记）→ 放弃排队，调用方发 cancelled 终结。
        """
        if cancel.is_cancelled:
            return False
        if self._engine._root_gate.locked():
            await self._engine._emit(EventMsg(
                submission_id=submission_id,
                msg=SubmissionQueued(data={
                    "submission_id": submission_id,
                    "waiting_on": self._engine._root_gate_owner,
                }),
            ))
        # 先把协程标成返回 bool 再 ensure_future:asyncio.Lock.acquire() 被标注为
        # 返回 Literal[True],而 Future 对类型参数**不变**,Task[Literal[True]] 不能
        # 赋给 Future[bool]。在源头收成 bool 比把下游签名放宽成 Any 更不伤类型信息。
        acquire_coro: Coroutine[Any, Any, bool] = self._engine._root_gate.acquire()
        acquire = asyncio.ensure_future(acquire_coro)
        waiter = asyncio.ensure_future(cancel.wait_cancelled())
        try:
            await asyncio.wait({acquire, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # 任务级 raw cancel（engine 收敛）：撤回 acquire，恰好拿到的锁立刻归还
            await self._engine._abandon_acquire(acquire)
            raise
        finally:
            waiter.cancel()
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            # 锁与 raw cancel 同一轮到达：cancel 会在下一个 await 抛出，此时若已持锁
            # 会在 release 期间跑起 turn——一律视为未获取，把锁归还后按取消传播
            await self._engine._abandon_acquire(acquire)
            raise asyncio.CancelledError("engine converging")
        if acquire.done() and not acquire.cancelled():
            acquire.result()
            self._engine._root_gate_owner = submission_id
            return True
        # 取消 token 先到：撤回 acquire
        await self._engine._abandon_acquire(acquire)
        return False

    async def abandon_acquire(self, acquire: asyncio.Future[bool]) -> None:
        """撤回一次 gate acquire；若它已经（或在撤回瞬间）拿到锁，立刻归还。"""
        if not acquire.done():
            acquire.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await acquire
        if acquire.done() and not acquire.cancelled() and acquire.result():
            self._engine._root_gate.release()

    def resume_tool_cancel(self, call_id: str) -> CancellationToken:
        """resume 执行已批准工具的 token 必须派生自 engine 根 token（R4）。

        此前用全新根 token → engine shutdown / pool close 的级联取消无法中止该工具。
        """
        if self._engine._root_cancel is None:
            raise RuntimeError("engine not running: resume requires an active root cancel token")
        return self._engine._root_cancel.child(f"resume_tool:{call_id}")

    def release_root_gate(self) -> None:
        """释放 root gate（持有者退出真终态之后调用）。"""
        self._engine._root_gate_owner = None
        self._engine._root_gate.release()

    async def run_gated_op(
        self,
        submission_id: str,
        root_cancel: CancellationToken,
        run: Callable[[CancellationToken], Coroutine[Any, Any, None]],
    ) -> None:
        """非 UserMessage 的 gated Op（CompactNow / Rollback / 根 Rewind / 根 Resume）。

        登记 _pending（排队中可被 CancelTurn 取消）→ 排队取 gate → 以派生 token 跑
        op → 释放。op 内部若再登记 _pending 会覆盖这里的记录（其 token 派生自同一
        gate_cancel，取消任一都能传达）。
        """
        gate_cancel = root_cancel.child(f"sub:{submission_id}:gate")
        self._engine._pending[submission_id] = _PendingTurn(submission_id, gate_cancel)
        if not await self._engine._acquire_root_gate(submission_id, gate_cancel):
            self._engine._pending.pop(submission_id, None)
            await self._engine._emit_operation_terminal(submission_id, None, kind="cancelled")
            return
        try:
            await run(gate_cancel)
        finally:
            self._engine._pending.pop(submission_id, None)
            self._engine._release_root_gate()

    async def run_turn_for(
        self,
        sub: Submission | AuditedTurnInput,
        root_cancel: CancellationToken,
        *,
        gate_held: bool = False,
    ) -> None:
        """根 turn 入口：登记 _pending → 排队取 root gate → 跑 turn → 释放。

        _pending 在排队前登记，CancelTurn 才能取消排队中的 submission
        （→ turn_failed{kind=cancelled}）。``gate_held=True``（audited 路径）表示
        调用方已在 application 之前持有 gate，这里不再取 / 放。
        """
        turn_cancel = root_cancel.child(f"sub:{sub.id}")
        self._engine._pending[sub.id] = _PendingTurn(sub.id, turn_cancel, audited_turn_index(sub))
        if gate_held:
            await self._engine._run_turn_for_gated(sub, turn_cancel)
            return
        if not await self._engine._acquire_root_gate(sub.id, turn_cancel):
            self._engine._pending.pop(sub.id, None)
            await self._engine._emit_operation_terminal(sub.id, None, kind="cancelled")
            return
        try:
            await self._engine._run_turn_for_gated(sub, turn_cancel)
        finally:
            self._engine._release_root_gate()

    async def run_turn_for_gated(
        self,
        sub: Submission | AuditedTurnInput,
        turn_cancel: CancellationToken,
    ) -> None:
        """持有 root gate 后的根 turn 主体（挂起守卫 → 落 user → 指令 → hook → runner）。"""
        if isinstance(sub, Submission):
            assert isinstance(sub.op, UserMessage)
            user_text = sub.op.text
            attachments = sub.op.attachments
        else:
            user_text = sub.text
            attachments = None

        # 挂起态守卫(suspend-review-fixes):根 thread 有活跃挂起 → 在落史**之前**
        # 显式拒绝新 UserMessage。挂起 = turn 停在待裁决,裁决(Resume retry/abort)
        # 是继续会话的唯一出口;放行会让新 turn 的同名编排 call_id fco 污染
        # request 级核销凭据(假核销 → TTL 静默 no-op / 幽灵续跑重放错轮),并使
        # engine 级 K2 record 可叠加成僵尸。拒绝不落史:被拒消息若入史会污染
        # 编排重放锚与续跑 seed——业务凭 thread_suspended 事件引导用户先裁决,
        # 结清后重发(消息文本在事件归因的 submission 里,未静默丢弃)。
        active_suspension = self._engine._find_active_suspension()
        if active_suspension is not None:
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=TurnFailed(data={
                "error": "active_suspension",
                "kind": "thread_suspended",
                "record_id": active_suspension.record_id,
                "iterations": 0,
                "is_root": True,
            })))
            self._engine._pending.pop(sub.id, None)
            self._engine._turn_index += 1
            return

        # 把 user 消息落 buffer + 持久化
        if attachments is not None:
            item = user_message(
                user_text,
                thread_id=self._engine._thread_id,
                attachments=attachments,
            )
            # resume/内部路径仍在 durable append 前执行 defense-in-depth 校验。
            from taifeng.llm.client import model_capabilities
            from taifeng.loop.prompt import history_to_api_messages

            history_to_api_messages(
                [item],
                image_input_policy=self._engine._image_input_policy,
                model_capabilities=model_capabilities(self._engine._model_client),
            )
            async with self._engine._lock:
                self._engine._history.append(item)
            await self._engine._store.append(item)

        # === T4 instructions-injection ===
        # 在 turn 启动前 resolve 当前 turn 的 instructions（engine 已 warmup 过；
        # 这里 resolve engine+session+turn 三档并合并）。
        # 失败 fail-fast → 将 InstructionFetchError 转成 turn_failed 事件
        resolved_for_turn: list[ResolvedInstruction] = []
        if self._engine._instruction_resolver is not None:
            self._engine._current_emit_submission_id = sub.id
            ctx = InstructionContext(
                session_id=self._engine._session_id,
                thread_id=self._engine._thread_id,
                entry_skill_id=self._engine._entry_skill.id,
                turn_index=self._engine._turn_index,
                metadata=self._engine._request_metadata,
                cancel=turn_cancel,
            )
            try:
                resolved_for_turn = await self._engine._instruction_resolver.resolve(
                    ("engine", "session", "turn"), ctx,
                )
            except InstructionFetchError as e:
                # fail-fast: 发 turn_failed 后退出
                await self._engine._emit(EventMsg(
                    submission_id=sub.id,
                    msg=TurnFailed(data={
                        "error": str(e),
                        "kind": "InstructionFetchError",
                        "iterations": 0,
                        # 引擎直接派发的 fail-fast 失败必属于根 turn。
                        "is_root": True,
                    }),
                ))
                self._engine._pending.pop(sub.id, None)
                self._engine._turn_index += 1
                return
            finally:
                self._engine._current_emit_submission_id = "*"
            self._engine._last_resolved = list(resolved_for_turn)

        # === pre_turn hook ===
        # 业务侧最后一道介入点：可基于 user_text + turn_index 拒绝 turn 启动。
        # 顺序约束（与 spec hooks/Requirement "pre_turn hook 调用点" 对齐）：
        #   1) user_message 已持久化（resume 友好）
        #   2) instruction resolve 已完成
        #   3) 此处 hook deny → 不创建 TurnRunner、emit turn_failed
        if self._engine._hooks is not None:
            from taifeng.hooks.types import HookContext, PreTurnHook
            pre_decision = await self._engine._hooks.run(
                "pre_turn",
                PreTurnHook(
                    user_text=user_text,
                    iteration=self._engine._turn_index,
                ),
                HookContext(
                    thread_id=self._engine._thread_id,
                    submission_id=sub.id,
                    entry_skill_id=self._engine._entry_skill.id,
                ),
            )
            if not pre_decision.allow:
                # emit 两条事件：先 pre_turn_hook_denied（定位原因），
                # 再 turn_failed（消费 subscribe(sub_id) 的 break 条件）
                preview = user_text[:200]
                await self._engine._emit(EventMsg(
                    submission_id=sub.id,
                    msg=PreTurnHookDenied(data={
                        "reason": pre_decision.reason or "",
                        "user_text_preview": preview,
                        "iteration": self._engine._turn_index,
                    }),
                ))
                # 注：kind 字段约定为"真实抛出的异常类名"（如 InstructionFetchError）；
                # hook deny 不抛异常，故此处用与 event kind 一致的描述性 label
                # （详见 spec config-consistency-fixes A3）
                await self._engine._emit(EventMsg(
                    submission_id=sub.id,
                    msg=TurnFailed(data={
                        "error": "pre_turn_hook_denied",
                        "kind": "pre_turn_hook_denied",
                        "iterations": 0,
                        # pre_turn hook 拒绝发生在 Engine 派发阶段（无 TurnRunner），
                        # 必属于根 turn。
                        "is_root": True,
                    }),
                ))
                self._engine._pending.pop(sub.id, None)
                self._engine._turn_index += 1
                return

        # K2 跨 turn 守卫：会话累计 token 已触顶 → 经 failure policy 裁决
        # (resource-limit-retry-semantics):TERMINAL → 拒绝开新 turn(现状);
        # SUSPEND → engine 级 RESOURCE_LIMIT 挂起,retry+extend_tokens 抬顶后续跑。
        if await self._engine._gate_session_tokens(sub.id):
            self._engine._pending.pop(sub.id, None)
            self._engine._turn_index += 1
            return

        await self._engine._build_and_run_runner(sub.id, turn_cancel, resolved_for_turn)

    async def gate_session_tokens(self, submission_id: str) -> bool:
        """K2 引擎级闸门:触顶时按 policy 裁决终态 / 挂起。

        Returns:
            True = 本次 turn 被闸(已 emit 终态或挂起事件,调用方收尾返回);
            False = 未触顶,照常开跑。

        SUSPEND 路径:在 engine 级直接落 SuspensionRecord(user_message 已入史,
        Resume retry+extend_tokens 抬顶后经既有根续跑链跑该 turn)。
        """
        if (self._engine._max_session_tokens is None
                or self._engine._session_tokens < self._engine._max_session_tokens):
            return False
        from taifeng.loop.failure_policy import (
            DEFAULT_FAILURE_POLICY,
            FailureContext,
            FailureDisposition,
        )
        policy = self._engine._failure_policy or DEFAULT_FAILURE_POLICY
        disposition = policy.decide(FailureContext(
            origin="guard_trip",
            failure_class=None,
            end_reason="resource_limit_exceeded",
            error_kind=None,
            retryable=False,
            is_root=True,
            iteration=0,
        ))
        suspended = disposition is FailureDisposition.SUSPEND
        await self._engine._emit(EventMsg(
            submission_id=submission_id, msg=ResourceLimitExceeded(
                data={
                    "limit_kind": "session_tokens",
                    "used": self._engine._session_tokens,
                    "limit": self._engine._max_session_tokens,
                    # R3 scope 如实:挂起时 turn 并未被拒杀,报 turn_suspended
                    "scope": "turn_suspended" if suspended else "turn_refused",
                })))
        if not suspended:
            await self._engine._emit(EventMsg(submission_id=submission_id, msg=TurnFailed(
                data={
                    "error": "session_token_limit_exceeded",
                    "kind": "resource_limit_exceeded",
                    "iterations": 0,
                    "is_root": True,
                })))
            return True
        await self._engine._suspend_engine_gate(submission_id)
        return True

    async def suspend_engine_gate(self, submission_id: str) -> None:
        """落 engine 级 K2 挂起记录(turn 未开跑,record 直接挂根 history)。

        与 turn 内护栏挂起同形(RESOURCE_LIMIT / related_call_id=None /
        on_expire 恒 abort——自动 retry 无人携带增额必然无效);Resume
        retry+extend_tokens 经既有根续跑链(_handle_resume)直接跑该 turn。
        """
        import secrets as _secrets

        from taifeng.suspend.reason import PendingRequest, SuspendReason
        from taifeng.suspend.record import SuspensionRecord

        pending = PendingRequest(
            request_id=f"sr_{_secrets.token_hex(6)}",
            reason=SuspendReason.RESOURCE_LIMIT,
            ttl_seconds=self._engine._failure_suspend_ttl_seconds,
            on_expire="abort",
            payload_schema={
                "type": "object",
                "properties": {"action": {"enum": ["retry", "abort"]}},
            },
            related_call_id=None,
            detail={
                "end_reason": "resource_limit_exceeded",
                "guard_snapshot": {
                    "used": self._engine._session_tokens,
                    "limit": self._engine._max_session_tokens,
                },
                "gate": "turn_refused",
            },
        )
        record = SuspensionRecord(
            record_id=f"sr_{_secrets.token_hex(6)}",
            thread_id=self._engine._thread_id,
            submission_id=submission_id,
            turn_index=self._engine._turn_index,
            pending=(pending,),
            created_at=int(self._engine._now_factory()),
        )
        item = record.to_item()
        async with self._engine._lock:
            self._engine._history.append(item)
        await self._engine._store.append(item)
        await self._engine._emit(EventMsg(submission_id=submission_id, msg=TurnSuspended(
            data={
                "thread_id": self._engine._thread_id,
                "record_id": record.record_id,
                "pending": item.payload["pending"],
                "cache_invalidated": True,
                "expires_at": record.expires_at,
            })))
