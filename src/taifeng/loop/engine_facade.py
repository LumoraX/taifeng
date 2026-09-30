"""AgentEngine 对兄弟模块的薄委托（events / operations / TTL / lifecycle / gate / runner /
spawn / resume / child resume chain / ops）。

方法体从 ``engine.py`` 原样搬出（W7.1 拆文件，零行为变更）；``AgentEngine`` 类里按原名赋值，
spawn_* / 测试按这些原名调用与打桩。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.loop import engine_ops
from taifeng.loop.child_resume_chain import ChildResumeChain
from taifeng.loop.suspension_access import SuspensionAccess
from taifeng.loop.turn import TurnOutcome, TurnRunner

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Coroutine

    from taifeng.conversation.models import ResponseItem
    from taifeng.conversation.origin import InputOrigin
    from taifeng.instructions.types import ResolvedInstruction
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.audit_llm import AuditedTurnInput
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.engine_types import _PendingTurn, _Subscriber
    from taifeng.loop.event import EventMsg
    from taifeng.loop.spawn_handle import SpawnHandle, SpawnHandleRegistry
    from taifeng.loop.submission import CompactNow, Resume, Submission
    from taifeng.skill.definition import SkillDefinition
    from taifeng.suspend.record import SuspensionRecord


async def _emit(self: AgentEngine, ev: EventMsg) -> None:
    # suspension-ttl:借唯一事件总线做定时器簿记——所有层级 turn(根/子/spawn)的
    # 挂起与核销事件都流经此处,单点覆盖,无需在各续跑路径埋点。
    """_emit"""
    await self._events.emit(ev)


def _record_terminal(self: AgentEngine, ev: EventMsg) -> None:
    """登记一个 submission 的终结事件，供晚到订阅者补投（有界 FIFO）。"""
    self._events.record_terminal(ev)


def _deliver(self: AgentEngine, sub: _Subscriber, ev: EventMsg) -> None:
    """把事件投递给单个订阅者：分配 per-subscriber delivery_seq（含丢弃烧号）→"""
    self._events.deliver(sub, ev)


def _maybe_warn_water(self: AgentEngine, sub: _Subscriber) -> None:
    """有界队列堆积告警：qsize 上穿高水位告一条 WARNING，回落到低水位以下才"""
    self._events.maybe_warn_water(sub)


def events_dropped(self: AgentEngine) -> int:
    """K4：累计因订阅队列满而丢弃的事件数（0 = 无丢弃）。

    业务侧观测：>0 说明某订阅消费过慢、漏了事件——应加大 ``event_queue_size``
    或更快 drain。lossy-but-accounted：内核绝不为慢 consumer 阻塞主 actor。
    """
    return self._events_dropped


def _start_operation(self: AgentEngine,
    coroutine: Coroutine[Any, Any, None],
    *,
    name: str,
    submission_id: str | None = None,
) -> asyncio.Task[None]:
    """创建并登记 Engine-owned operation，终态总会检索异常。"""
    return self._ops.start_operation(coroutine, name=name, submission_id=submission_id)


async def _guarded_operation(self: AgentEngine, submission_id: str, coroutine: Coroutine[Any, Any, None],
) -> None:
    """operation 崩溃 → 终结事件 + 清 _pending，再原样上抛（日志由 _forget_operation 记）。"""
    await self._ops.guarded_operation(submission_id, coroutine)


async def _emit_operation_terminal(self: AgentEngine, submission_id: str, exc: BaseException | None, *, kind: str | None = None,
) -> None:
    """给一个 submission 发 engine 层面的 ``turn_failed`` 终结事件。"""
    await self._ops.emit_operation_terminal(submission_id, exc, kind=kind)


def _forget_operation(self: AgentEngine, task: asyncio.Task[None]) -> None:
    """检索 operation 终态并释放 Engine 显式 ownership。"""
    self._ops.forget_operation(task)


async def _converge_operations(self: AgentEngine) -> asyncio.CancelledError | None:
    """取消并等待所有 operation；actor 自身取消也不得截断收敛。"""
    return await self._ops.converge_operations()


def _arm_ttl_timer(self: AgentEngine, data: dict[str, Any]) -> None:
    """按 turn_suspended 事件武装到期定时器（实现见 SuspensionTtlScheduler.arm）。"""
    self._ttl.arm(data)


async def _ttl_expire_after(self: AgentEngine, delay: float, thread_id: str, record_id: str
) -> None:
    """到期触发裁决（实现见 SuspensionTtlScheduler.expire_after）。"""
    await self._ttl.expire_after(delay, thread_id, record_id)


async def _rearm_ttl_timers_cold(self: AgentEngine) -> None:
    """冷恢复后重武装根 thread 的到期定时器。"""
    await self._ttl.rearm_cold()


async def _rearm_spawn_ttl_timers_cold(self: AgentEngine) -> None:
    """冷恢复后重武装 spawn 子 thread 的到期定时器（句柄表重建之后）。"""
    await self._ttl.rearm_spawn_cold()


async def _ttl_record_active(self: AgentEngine, thread_id: str, record_id: str
) -> SuspensionRecord | None:
    """按 (thread_id, record_id) 取仍活跃的挂起记录。"""
    return await self._ttl.record_active(thread_id, record_id)


async def _resolve_expiry_route(self: AgentEngine, thread_id: str, record_id: str
) -> str | None:
    """解析到期裁决应投递到哪个 thread（不可解析返回 None）。"""
    return await self._ttl.resolve_expiry_route(thread_id, record_id)


async def _chain_contains_thread(self: AgentEngine, root_tid: str, target_tid: str, depth: int,
) -> bool:
    """自 root_tid 沿活跃挂起的 CHILD_SKILL pending DFS，判定子链是否含 target。"""
    return await self._ttl.chain_contains_thread(root_tid, target_tid, depth)


def _cancel_ttl_timers(self: AgentEngine) -> None:
    """shutdown：取消全部到期定时器（R4，不阻塞主 actor）。"""
    self._ttl.cancel_all()


async def _memory_session_end(self: AgentEngine) -> None:
    """K3 teardown：shutdown 时调 memory_store.on_session_end。best-effort。"""
    await self._lifecycle.memory_session_end()


async def _finalize_run_lifecycle(self: AgentEngine,
    cancel: CancellationToken,
    *,
    shutdown_requested: bool,
) -> None:
    """按原顺序收敛 actor、operation、持久化 flush 与订阅者终态。"""
    await self._lifecycle.finalize_run_lifecycle(cancel, shutdown_requested=shutdown_requested)


async def _terminate_orphan_submissions(self: AgentEngine) -> None:
    """Shutdown 收尾：对仍在订阅的与队列残留的 submission 投 engine_shutdown 终结。"""
    await self._lifecycle.terminate_orphan_submissions()


async def _acquire_root_gate(self: AgentEngine, submission_id: str, cancel: CancellationToken,
) -> bool:
    """排队获取 root gate；gate 被占时 emit submission_queued，排队中被取消返回 False。"""
    return await self._gate.acquire_root_gate(submission_id, cancel)


async def _abandon_acquire(self: AgentEngine, acquire: asyncio.Future[bool]) -> None:
    """撤回一次 gate acquire；若它已经（或在撤回瞬间）拿到锁，立刻归还。"""
    await self._gate.abandon_acquire(acquire)


def _resume_tool_cancel(self: AgentEngine, call_id: str) -> CancellationToken:
    """resume 执行已批准工具的 token 必须派生自 engine 根 token（R4）。"""
    return self._gate.resume_tool_cancel(call_id)


def _release_root_gate(self: AgentEngine) -> None:
    """释放 root gate（持有者退出真终态之后调用）。"""
    self._gate.release_root_gate()


async def _run_gated_op(self: AgentEngine,
    submission_id: str,
    root_cancel: CancellationToken,
    run: Callable[[CancellationToken], Coroutine[Any, Any, None]],
) -> None:
    """非 UserMessage 的 gated Op（CompactNow / Rollback / 根 Rewind / 根 Resume）。"""
    await self._gate.run_gated_op(submission_id, root_cancel, run)


async def _run_turn_for(self: AgentEngine,
    sub: Submission | AuditedTurnInput,
    root_cancel: CancellationToken,
    *,
    gate_held: bool = False,
) -> None:
    """根 turn 入口：登记 _pending → 排队取 root gate → 跑 turn → 释放。"""
    await self._gate.run_turn_for(sub, root_cancel, gate_held=gate_held)


async def _run_turn_for_gated(self: AgentEngine,
    sub: Submission | AuditedTurnInput,
    turn_cancel: CancellationToken,
) -> None:
    """持有 root gate 后的根 turn 主体（挂起守卫 → 落 user → 指令 → hook → runner）。"""
    await self._gate.run_turn_for_gated(sub, turn_cancel)


async def _gate_session_tokens(self: AgentEngine, submission_id: str) -> bool:
    """K2 引擎级闸门:触顶时按 policy 裁决终态 / 挂起。"""
    return await self._gate.gate_session_tokens(submission_id)


async def _suspend_engine_gate(self: AgentEngine, submission_id: str) -> None:
    """落 engine 级 K2 挂起记录(turn 未开跑,record 直接挂根 history)。"""
    await self._gate.suspend_engine_gate(submission_id)


def _active_root_pending(self: AgentEngine) -> _PendingTurn | None:
    """返回当前根 thread 在飞 turn 的 pending 记录（无则 None）。"""
    return self._runner.active_root_pending()


async def _drain_residual_injections(self: AgentEngine, runner: TurnRunner, submission_id: str,
) -> None:
    """turn 退出后把 runner 未消费的 pending 注入并入 buffer + store（R5）。"""
    await self._runner.drain_residual_injections(runner, submission_id)


async def _writeback_turn_runner(self: AgentEngine, runner: TurnRunner) -> None:
    """完整验证 audited history 后原子回写 runner 派生状态。"""
    await self._runner.writeback_turn_runner(runner)


async def _build_and_run_runner(self: AgentEngine,
    submission_id: str,
    turn_cancel: CancellationToken,
    resolved_for_turn: list[ResolvedInstruction],
    *,
    seed_pending_call_id: str | None = None,
    cache_break_expected_reason: str | None = None,
    auto_retry_count: int = 0,
    extra_seed_call_ids: tuple[str, ...] = (),
) -> None:
    """构造并运行一轮，最后一次性回写 Engine 状态。"""
    await self._runner.build_and_run_runner(
        submission_id, turn_cancel, resolved_for_turn,
        seed_pending_call_id=seed_pending_call_id,
        cache_break_expected_reason=cache_break_expected_reason,
        auto_retry_count=auto_retry_count, extra_seed_call_ids=extra_seed_call_ids,
    )


async def _fire_post_turn_hook(self: AgentEngine,
    submission_id: str,
    outcome: TurnOutcome,
    turn_cancel: CancellationToken,
    iteration: int,
) -> None:
    """root turn 真终态时同步触发 post_turn 钩子(审计型,不可否决)。"""
    await self._runner.fire_post_turn_hook(submission_id, outcome, turn_cancel, iteration)


def _spawn_handles(self: AgentEngine) -> SpawnHandleRegistry:
    """detached spawn 句柄表（白盒访问转发到 SpawnDriver）。

    逻辑已抽到 SpawnDriver；保留本 property 是为白盒断言 / 旧调用点提供等价访问，
    语义与抽取前一致（同一个 SpawnHandleRegistry 实例）。
    """
    return self._spawn._spawn_handles  # noqa: SLF001


def _fired_barriers(self: AgentEngine) -> set[str]:
    """join-barrier 进程内幂等守卫集（白盒访问转发到 SpawnDriver）。"""
    return self._spawn._fired_barriers  # noqa: SLF001


async def spawn_skill(self: AgentEngine, *, skill_id: str, args: dict[str, Any], reason: str,
    deadline_seconds: float | None = None, handle_id: str | None = None,
) -> dict[str, str]:
    """转发到 SpawnDriver.spawn_skill —— 公共 API + tools 的 spawn_coordinator 入口。

    分离式发起子 skill：立即返回句柄，子 skill 在后台分离 task 跑完。门控 / K1
    配额 / detached task 启动均由 SpawnDriver 负责。详见 spawn_driver.py。

    Args:
        skill_id: 要分离发起的子 skill id（须在 entry skill 的 child_skills 白名单内）。
        args: 子 skill 的种子输入（序列化为子 thread 首条 user_message）。
        reason: LLM / 业务自陈的发起理由（透传到事件 / 审计，taifeng 不解析语义）。
        deadline_seconds: 可选墙钟上限（秒），到点以 ``DEADLINE_EXCEEDED`` 取消整棵
            spawn 子树（cancel-reason-deadline）；None = 不限。

    Returns:
        ``{"handle_id": ..., "child_thread_id": ...}`` —— 立即可用于 ``spawn_status``。
    """
    return await self._spawn.spawn_skill(
        skill_id=skill_id, args=args, reason=reason, deadline_seconds=deadline_seconds,
        handle_id=handle_id,
    )


def _build_child_runner(self: AgentEngine,
    target: SkillDefinition,
    child_thread_id: str,
    seed: ResponseItem,
    cancel: CancellationToken,
    *,
    history: list[ResponseItem] | None = None,
    auto_retry_count: int = 0,
    sample_scope_id: str | None = None,
    audit_state: AuditedSessionState | None = None,
) -> TurnRunner:
    """构造 detached spawn 的子 TurnRunner（镜像 turn.py::_spawn_sub_runner 的 kwargs）。

    ``auto_retry_count``:TTL 到期自动 retry 的谱系计数(suspend-review-fixes:
    spawn 重跑透传 → failure_suspend_max_auto_retries 对 spawn 拓扑生效)。

    与阻塞式 call_skill 子 runner 的差异：``cancel`` 由 engine 根取消派生（而非
    父 turn 的 ctx.cancel），其余依赖（snapshot / model / runtime / store /
    compressors / dispatch_policy / budget / hooks / permission / 资源配额）一致。
    ``call_stack`` 留空 → 子 runner 自判为独立根 turn（detached 即独立上下文）。

    Args:
        history: 续跑场景传入【已补齐 gap 的子 thread 完整历史】（从 store load_thread
            读回）；首发场景为 None → 用 ``[seed]`` 起跑。两种场景都保持 call_stack 空，
            即 detached 子 turn 永远是独立根 turn（resume 后仍是独立根，不依附父）。
        sample_scope_id: 本次 Responses 逻辑采样作用域；事件仍按 child thread 分轨。
    """
    buffer = list(history) if history is not None else [seed]
    return TurnRunner(
        entry_skill=target,
        snapshot=self._snapshot,
        model_client=self._model_client,
        tool_runtime=self._tool_runtime,
        store=self._store,
        compressors=self._compressors,
        dispatch_policy=self._dispatch_policy,
        outcome_judge=self._outcome_judge,
        budget=self._budget,
        thread_id=child_thread_id,
        submission_id=child_thread_id,
        emit=self._emit,
        cancel=cancel,
        image_input_policy=self._image_input_policy,
        input_cost_estimator=self._input_cost_estimator,
        file_input_policy=self._file_input_policy,
        hooks=self._hooks,
        permission_policy=self._permission_policy,
        request_metadata=self._request_metadata,
        # 审计：子 thread 自己的 turn 从 0 编号，效果记在子 thread 名下（ADR 0098）
        turn_index=self._turn_index if audit_state is None else 0,
        audit_state=audit_state,
        script_executors=self._script_executors,
        max_iterations=self._max_iterations,
        denial_breaker_config=self._denial_breaker_config,
        doom_loop_config=self._doom_loop_config,
        failure_policy=self._failure_policy,
        failure_suspend_ttl_seconds=self._failure_suspend_ttl_seconds,
        failure_suspend_on_expire=self._failure_suspend_on_expire,
        auto_retry_count=auto_retry_count,
        max_parallel_tool_calls=self._max_parallel_tool_calls,
        sample_scope_id=sample_scope_id,
        reasoning_passback=self._reasoning_passback,
        enable_request_capture=self._enable_request_capture,
        capabilities=self._capabilities,
        # T6: deferred 暴露阈值（驱动 child 列表 inline/deferred + 工具裁剪）
        recall_threshold=self._recall_threshold,
        # 召回后端存在性：无后端恒 inline（与阈值同口径透传）
        has_recall_backend=self._has_recall_backend,
        spawn_registry=self._spawn_registry,
        session_tokens_used=self._session_tokens,
        max_session_tokens=self._max_session_tokens,
        usage_meter=self._usage_meter,
        memory_store=self._memory_store,
        memory_query_builder=self._memory_query_builder,
        pinned_states=self._pinned_states,
        history_buffer=buffer,
        # detached-spawn：spawned 子 runner 也注入协调器 → 子 skill 可继续 spawn
        spawn_coordinator=self,
    )


async def _resume_spawn(self: AgentEngine, sub: Submission, handle: SpawnHandle) -> None:
    """转发到 SpawnDriver.resume_spawn —— 续跑挂起的 detached spawn 子 thread。

    调用点：主 run 循环的 Resume 分支（命中挂起 spawn 句柄时）。
    """
    await self._spawn.resume_spawn(sub, handle)


def _match_suspended_spawn(self: AgentEngine, thread_id: str) -> SpawnHandle | None:
    """转发到 SpawnDriver.match_suspended_spawn —— Resume 路由判定。

    调用点：主 run 循环的 Resume 分支（判 thread_id 是否命中挂起 spawn）。
    """
    return self._spawn.match_suspended_spawn(thread_id)


def spawn_status(self: AgentEngine, handle_ids: list[str]) -> dict[str, dict[str, Any]]:
    """转发到 SpawnDriver.spawn_status —— 公共 API（业务侧轮询 / join 检查）。"""
    return self._spawn.spawn_status(handle_ids)


def is_spawn_thread(self: AgentEngine, thread_id: str) -> bool:
    """``thread_id`` 是否本 engine 登记过的 detached spawn 子 thread（可被 peer 寻址）。"""
    return any(h.child_thread_id == thread_id
               for h in self._spawn_handles.handles.values())


async def deliver_peer_message(self: AgentEngine,
    *,
    target: str,
    text: str,
    mode: str = "queue_only",
    from_thread_id: str | None = None,
    submission_id: str | None = None,
    origin: InputOrigin | None = None,
) -> dict[str, Any]:
    """转发到 SpawnDriver.deliver_peer_message —— peer-mailbox 唯一投递路径。

    ``send_message`` 工具（经 spawn_coordinator 协议）与 ``SendToPeer`` Op
    都收敛到此。详见 spawn_driver.py 同名方法。
    """
    return await self._spawn.deliver_peer_message(
        target=target, text=text, mode=mode, origin=origin,
        from_thread_id=from_thread_id, submission_id=submission_id)


async def wait_spawn_terminal(self: AgentEngine,
    *,
    handle_id: str,
    timeout_seconds: float,
    cancel: CancellationToken,
) -> dict[str, Any]:
    """转发到 SpawnDriver.wait_spawn_terminal —— ``wait_peer`` 工具实现体。"""
    return await self._spawn.wait_spawn_terminal(
        handle_id=handle_id, timeout_seconds=timeout_seconds, cancel=cancel)


async def wait_spawn_any(self: AgentEngine,
    *,
    handle_ids: list[str],
    timeout_seconds: float,
    cancel: CancellationToken,
) -> dict[str, Any]:
    """转发到 SpawnDriver.wait_spawn_any —— ``wait_any`` 工具实现体（any-of-N）。"""
    return await self._spawn.wait_spawn_any(
        handle_ids=handle_ids, timeout_seconds=timeout_seconds, cancel=cancel)


async def kill_spawn(self: AgentEngine, handle_id: str) -> None:
    """转发到 SpawnDriver.kill_spawn —— 公共 API（主动终止单个 spawn 子树）。"""
    await self._spawn.kill_spawn(handle_id)


def has_live_spawns(self: AgentEngine) -> bool:
    """转发到 SpawnDriver.has_live_spawns —— 公共 API（pool 释放前的引用计数保活）。"""
    return self._spawn.has_live_spawns()


async def set_join_barrier(self: AgentEngine,
    handle_ids: list[str],
    then_skill_id: str,
    then_args_template: dict[str, Any] | None = None,
) -> dict[str, str]:
    """转发到 SpawnDriver.set_join_barrier —— 公共 API（登记 join-barrier）。"""
    return await self._spawn.set_join_barrier(
        handle_ids, then_skill_id, then_args_template
    )


async def _rebuild_spawn_state_from_history(self: AgentEngine) -> None:
    """转发到 SpawnDriver.rebuild_from_history —— 冷恢复重建句柄表 / barrier / 守卫集。

    调用点：pool 重载 engine 时（engine 持有 prior history 的 resume 场景）。
    """
    await self._spawn.rebuild_from_history()
    # suspension-ttl 冷重武装(spawn 段):句柄表就绪后才枚举得到挂起态 spawn
    await self._rearm_spawn_ttl_timers_cold()


async def _handle_resume(self: AgentEngine, sub: Submission, root_cancel: CancellationToken) -> None:
    """续跑一个挂起的 thread：配对 resolutions → 补齐 history gap → 续采样。"""
    await self._resume.handle_resume(sub, root_cancel)


async def _handle_resume_resolved(self: AgentEngine, sub: Submission, op: Resume,
    record: SuspensionRecord, root_cancel: CancellationToken,
) -> None:
    """_handle_resume 的主体(在飞守卫占位后):配对 → 应用 → 结算 → 续跑。"""
    await self._resume.handle_resume_resolved(sub, op, record, root_cancel)


async def _handle_child_resume(self: AgentEngine, sub: Submission, op: Resume, root_cancel: CancellationToken
) -> None:
    """续跑一个【子 thread】的挂起，并把结果逐层回传父 call_skill 直到根完成。"""
    await self._child_chain.handle_child_resume(sub, op, root_cancel)


async def _build_resume_chain(self: AgentEngine, leaf_thread_id: str
) -> list[tuple[str, str, str | None]] | None:
    """自根 self._thread_id 沿 CHILD_SKILL pending 向下串出到 leaf 的续跑链。"""
    return await self._child_chain.build_resume_chain(leaf_thread_id)


def _max_total_spawns_guard(self: AgentEngine) -> int:
    """续跑链 DFS 下探的最大层数守卫(防坏数据成环)。"""
    return self._child_chain.max_total_spawns_guard()


def _next_child_link(
    record: SuspensionRecord,
) -> tuple[str, str, str | None] | None:
    """从一个挂起 record 里取首个 CHILD_SKILL pending → (子tid, 子skill_id, 父callid)。"""
    return ChildResumeChain.next_child_link(record)


async def _resume_leaf_thread(self: AgentEngine, sub: Submission, leaf_tid: str, leaf_skill_id: str,
    resolutions: dict[str, Any], root_cancel: CancellationToken,
    *, submission_id: str | None = None,
) -> str | None:
    """核销 leaf 子 thread 的用户挂起 + 续跑该子 turn，返回回传父的结果字符串。"""
    return await self._child_chain.resume_leaf_thread(
        sub,
        leaf_tid,
        leaf_skill_id,
        resolutions,
        root_cancel,
        submission_id=submission_id,
    )


async def _resume_leaf_settled(self: AgentEngine, sub: Submission, leaf_tid: str, leaf_skill_id: str,
    resolutions: dict[str, Any], record: SuspensionRecord,
    root_cancel: CancellationToken, *, submission_id: str | None = None,
) -> str | None:
    """_resume_leaf_thread 的主体(在飞守卫占位后):配对 → 应用 → 结算 → 续跑。"""
    return await self._child_chain.resume_leaf_settled(
        sub,
        leaf_tid,
        leaf_skill_id,
        resolutions,
        record,
        root_cancel,
        submission_id=submission_id,
    )


async def _resume_parent_level(self: AgentEngine, sub: Submission, parent_tid: str, parent_skill_id: str,
    call_id: str | None, child_result: str, root_cancel: CancellationToken,
    *, submission_id: str | None = None,
) -> str | None:
    """回填父 thread 中 call_id 对应 call_skill 的 output，续跑父 turn。"""
    return await self._child_chain.resume_parent_level(
        sub,
        parent_tid,
        parent_skill_id,
        call_id,
        child_result,
        root_cancel,
        submission_id=submission_id,
    )


async def _build_spawn_resume_chain(self: AgentEngine, root_tid: str, root_skill_id: str,
    resolutions: dict[str, Any] | None = None,
) -> list[tuple[str, str, str | None]] | None:
    """自 spawn 子 thread 沿 CHILD_SKILL pending 向下串到最深 leaf 的续跑链。"""
    return await self._child_chain.build_spawn_resume_chain(
        root_tid,
        root_skill_id,
        resolutions,
    )


async def _settle_call_skill_output(self: AgentEngine, sub: Submission, thread_id: str, call_id: str, child_result: str
) -> str:
    """在 thread 上回填 call_id 对应 call_skill 的 function_call_output + 落 resolved-marker"""
    return await self._child_chain.settle_call_skill_output(
        sub,
        thread_id,
        call_id,
        child_result,
    )


async def _load_thread_items(self: AgentEngine, thread_id: str) -> list[ResponseItem]:
    """非根 thread 的**逻辑 history 单一入口**:load_thread → reconstruct。"""
    return await self._suspend_access.load_thread_items(thread_id)


async def _apply_plan_on_thread(self: AgentEngine, thread_id: str, entry_skill_id: str,
    record: SuspensionRecord, plan: Any
) -> None:
    """在指定 thread 上应用 ResolvePlan 的 gap 补齐（form/data/deny/allow-execute）。"""
    await self._child_chain.apply_plan_on_thread(thread_id, entry_skill_id, record, plan)


async def _append_resolved_marker(self: AgentEngine, thread_id: str, record_id: str) -> None:
    """落 record 级 resolved-marker(request 级核销全量达成时的唯一非根签发点)。"""
    await self._child_chain.append_resolved_marker(thread_id, record_id)


async def _run_thread_turn(self: AgentEngine, sub: Submission, thread_id: str, entry_skill_id: str,
    root_cancel: CancellationToken, *, submission_id: str | None = None,
    auto_retry_count: int = 0,
) -> Any:
    """为指定（非根）thread 构造 TurnRunner 并续跑一轮，返回 TurnOutcome。"""
    return await self._child_chain.run_thread_turn(
        sub,
        thread_id,
        entry_skill_id,
        root_cancel,
        submission_id=submission_id,
        auto_retry_count=auto_retry_count,
    )


async def _execute_resumed_tool_on_thread(self: AgentEngine, thread_id: str, entry_skill_id: str, call_id: str
) -> None:
    """在指定 thread 上执行一个被批准的挂起 tool call，回填 function_call_output。"""
    await self._child_chain.execute_resumed_tool_on_thread(thread_id, entry_skill_id, call_id)


async def _cancel_active_suspension(self: AgentEngine, cancel_sub_id: str, target_sub_id: str
) -> None:
    """R4：若存在 submission_id 匹配的活跃挂起，追加 resolved-marker 丢弃之。"""
    await self._suspend_access.cancel_active_suspension(cancel_sub_id, target_sub_id)


def _find_active_suspension(self: AgentEngine) -> SuspensionRecord | None:
    """扫 self._history，返回最后一条尚未被 resolved-marker 消费的 suspension record。"""
    return self._suspend_access.find_active_suspension()


def _find_active_suspension_in(
    items: list[ResponseItem],
) -> SuspensionRecord | None:
    """在任意 items 序列中找最后一条未被 resolved-marker 消费的 suspension record。"""
    return SuspensionAccess.find_active_suspension_in(items)


def _deny_output_text(
    record: SuspensionRecord, call_id: str, reason_text: str,
) -> str:
    """按 pending reason 渲染 deny 回填文案(suspension-ttl-hardening)。"""
    return SuspensionAccess.deny_output_text(record, call_id, reason_text)


def _apply_plan_session_effects(self: AgentEngine, plan: Any, record: SuspensionRecord) -> int:
    """应用 ResolvePlan 的会话级副作用,返回续跑 runner 的 auto_retry_count。"""
    return self._suspend_access.apply_plan_session_effects(plan, record)


def _effective_resolutions(self: AgentEngine, record: SuspensionRecord, items: list[ResponseItem],
    resolutions: dict[str, Any],
) -> dict[str, Any]:
    """到期哨兵 resolutions 与未核销 pending 求交;人工 payload 原样返回。"""
    return self._suspend_access.effective_resolutions(record, items, resolutions)


def _settle_lock(self: AgentEngine, record_id: str) -> asyncio.Lock:
    """取 record 级结算锁(惰性创建;record 终结后残留的空锁可忽略不计)。"""
    return self._suspend_access.settle_lock(record_id)


def _unsettled_pendings(
    record: SuspensionRecord, items: list[ResponseItem],
) -> list[Any]:
    """返回 record 中尚未核销的 pending(request 级核销的推导真相,R5)。"""
    return SuspensionAccess.unsettled_pendings(record, items)


async def _execute_resumed_tool(self: AgentEngine, call_id: str) -> None:
    """resume 时对一个被批准的挂起 tool call 真正执行，回填 function_call_output。"""
    await self._suspend_access.execute_resumed_tool(call_id)


async def _run_compact_now(self: AgentEngine,
    submission_id: str,
    op: CompactNow,
    root_cancel: CancellationToken,
) -> None:
    """_run_compact_now"""
    await self._suspend_access.run_compact_now(submission_id, op, root_cancel)


async def _emit_rewind_table_rebuilt(self: AgentEngine) -> None:
    """冷恢复后补发 rewind_table_rebuilt（R3 可观测）。

    薄委托：pool_session 按 ``engine._emit_rewind_table_rebuilt()`` 白盒寻址，
    故保留本方法名，实现见 ``engine_ops.emit_rewind_table_rebuilt``。
    """
    await engine_ops.emit_rewind_table_rebuilt(self)
