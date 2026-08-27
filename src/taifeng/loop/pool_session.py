"""EnginePool session 准备、resume 收尾与 watcher 启动 helpers。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.loop.audit_bootstrap import (
    AuditedSessionState,
    bootstrap_audited_session,
    fail_audited_bootstrap,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from taifeng.conversation.models import ResponseItem
    from taifeng.conversation.store import MessageStore
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.loop.audit_config import AuditConfig
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.pool import EnginePool
    from taifeng.skill.registry import FilesystemSkillRegistry, SkillSnapshot


@dataclass(frozen=True, slots=True)
class PreparedPoolSession:
    """Journal/legacy thread 准备结果。"""

    thread_id: str
    initial_history: tuple[ResponseItem, ...]
    audit_state: AuditedSessionState | None


def _bind_audited_finish_owner(
    pool: EnginePool,
    engine: AgentEngine,
    state: AuditedSessionState | None,
    session_id: str,
) -> None:
    """把 audited state 与唯一 EnginePool release owner 注入 Engine。"""
    if state is None:
        return

    async def finish_owner() -> None:
        """把 audited Shutdown 收敛委托给唯一 EnginePool owner。"""
        await pool.release(session_id, force=True)

    engine._audit_state = state  # noqa: SLF001
    engine._audit_finish_owner = finish_owner  # noqa: SLF001


async def prepare_pool_session(
    *,
    audit: AuditConfig | None,
    store: MessageStore,
    projection_store: JsonlMessageStore | None,
    session_id: str,
    entry_skill_id: str,
    cwd: str | None,
    resume_thread_id: str | None,
) -> PreparedPoolSession:
    """按 pool 模式准备 audited bootstrap、legacy resume 或新 thread。"""
    if audit is not None:
        state = await bootstrap_audited_session(
            config=audit,
            projection_store=projection_store,
            session_id=session_id,
            entry_skill_id=entry_skill_id,
            cwd=cwd,
        )
        return PreparedPoolSession(state.thread_id, (), state)
    if resume_thread_id is None:
        thread_id = await store.create_thread(
            cwd=cwd,
            entry_skill_id=entry_skill_id,
            source=f"session:{session_id}",
        )
        return PreparedPoolSession(thread_id, (), None)
    iterator = await store.load_thread(resume_thread_id)
    history = tuple([item async for item in iterator])
    if not history:
        raise ValueError(
            f"resume_thread_id {resume_thread_id!r} not found or empty thread"
        )
    return PreparedPoolSession(resume_thread_id, history, None)


async def create_started_pool_engine(
    pool: EnginePool,
    *,
    engine_factory: Callable[..., AgentEngine],
    entry: object,
    snapshot: object,
    prepared: PreparedPoolSession,
    session_id: str,
    resume_thread_id: str | None,
) -> tuple[AgentEngine, asyncio.Task[None]]:
    """构造、注入 audit ownership、warmup 并启动 actor task。"""
    state = prepared.audit_state
    try:
        engine = engine_factory(
            entry_skill=entry,
            skill_snapshot=snapshot,
            tool_runtime=pool._tool_runtime,  # noqa: SLF001
            model_client=pool._model_client,  # noqa: SLF001
            image_input_policy=pool._image_input_policy,  # noqa: SLF001
            input_cost_estimator=pool._input_cost_estimator,  # noqa: SLF001
            store=pool._store,  # noqa: SLF001
            thread_id=prepared.thread_id,
            session_id=session_id,
            compressors=pool._compressors,  # noqa: SLF001
            dispatch_policy=pool._dispatch_policy,  # noqa: SLF001
            outcome_judge=pool._outcome_judge,  # noqa: SLF001
            budget=pool._budget,  # noqa: SLF001
            hooks=pool._hooks,  # noqa: SLF001
            max_iterations=pool._max_iterations,  # noqa: SLF001
            denial_breaker_config=pool._denial_breaker_config,  # noqa: SLF001
            doom_loop_config=pool._doom_loop_config,  # noqa: SLF001
            failure_policy=pool._failure_policy,  # noqa: SLF001
            failure_suspend_ttl_seconds=pool._failure_suspend_ttl_seconds,  # noqa: SLF001
            failure_suspend_max_auto_retries=pool._failure_suspend_max_auto_retries,  # noqa: SLF001
            failure_suspend_on_expire=pool._failure_suspend_on_expire,  # noqa: SLF001
            now_factory=pool._now_factory,  # noqa: SLF001
            max_parallel_tool_calls=pool._max_parallel_tool_calls,  # noqa: SLF001
            reasoning_passback=pool._reasoning_passback,  # noqa: SLF001
            enable_request_capture=pool._enable_request_capture,  # noqa: SLF001
            instruction_layers=pool._instruction_layers,  # noqa: SLF001
            script_executors=pool._script_executors,  # noqa: SLF001
            event_queue_size=pool._event_queue_size,  # noqa: SLF001
            event_high_water_ratio=pool._event_high_water_ratio,  # noqa: SLF001
            event_low_water_ratio=pool._event_low_water_ratio,  # noqa: SLF001
            event_warn_cooldown_sec=pool._event_warn_cooldown_sec,  # noqa: SLF001
            submission_queue_size=pool._submission_queue_size,  # noqa: SLF001
            initial_history=(
                list(prepared.initial_history) if resume_thread_id else None
            ),
            permission_policy=pool._permission_policy,  # noqa: SLF001
            request_metadata=pool._request_metadata,  # noqa: SLF001
            max_concurrent_spawns=pool._max_concurrent_spawns,  # noqa: SLF001
            max_total_spawns=pool._max_total_spawns,  # noqa: SLF001
            max_session_tokens=pool._max_session_tokens,  # noqa: SLF001
            memory_store=pool._memory_store,  # noqa: SLF001
            memory_query_builder=pool._memory_query_builder,  # noqa: SLF001
            pinned_state_sources=pool._pinned_state_sources,  # noqa: SLF001
            pinned_total_max_chars=pool._pinned_total_max_chars,  # noqa: SLF001
            recall_threshold=pool._recall_threshold,  # noqa: SLF001
            has_recall_backend=(
                pool._skill_recall is not None  # noqa: SLF001
                or pool._enable_auto_discovery  # noqa: SLF001
            ),
        )
        _bind_audited_finish_owner(pool, engine, state, session_id)
        # 让 engine 能在收到 RefreshSnapshot 时拉最新快照。
        engine._registry_ref = pool._registry  # type: ignore[attr-defined]  # noqa: SLF001
        # 启动期一次性 resolve engine scope，失败时由 audited finish 收敛。
        await engine.warmup_engine_scope()
        engine_cancel = pool._root_cancel.child(f"session:{session_id}")  # noqa: SLF001
        task = asyncio.create_task(engine.run(engine_cancel))
    except BaseException as exc:
        if state is not None:
            await fail_audited_bootstrap(
                state,
                exc,
                reason="engine_bootstrap_failed",
            )
        raise
    return engine, task


async def finalize_resumed_engine(
    engine: AgentEngine,
    *,
    resume_thread_id: str | None,
    entry_skill_id: str,
    initial_history: tuple[ResponseItem, ...],
) -> None:
    """actor 启动后恢复 spawn state，并发出既有 ThreadResumed 事件。

    仅 resume 路径从 parent thread 持久项重建 spawn 句柄、barrier 与 fired
    守卫集，恰好一次；放在 ``run()`` task 启动后，确保 root cancel 已就绪。
    """
    if resume_thread_id is None:
        return
    await engine._rebuild_spawn_state_from_history()  # noqa: SLF001
    await engine._emit_rewind_table_rebuilt()  # noqa: SLF001
    from taifeng.loop.event import EventMsg, ThreadResumed

    await engine._emit(  # noqa: SLF001
        EventMsg(
            submission_id="*",
            msg=ThreadResumed(
                data={
                    "thread_id": resume_thread_id,
                    "item_count": len(initial_history),
                    "entry_skill_id_at_resume": entry_skill_id,
                    "entry_skill_id_recorded": None,
                }
            ),
        )
    )


async def start_skill_watcher(
    pool: EnginePool,
    registry: FilesystemSkillRegistry,
    *,
    enabled: bool,
    poll_interval_seconds: float,
) -> None:
    """按 opt-in 启动 snapshot watcher；关闭时不创建 task。"""
    if not enabled:
        return
    from taifeng.loop.submission import RefreshSnapshot
    from taifeng.skill.watcher import SkillFileWatcher

    async def _on_change(snapshot: SkillSnapshot) -> None:
        logger.info("skill snapshot refreshed via watcher → version=%d", snapshot.version)
        async with pool._lock:  # noqa: SLF001
            for engine in pool._engines.values():  # noqa: SLF001
                await engine.submit(RefreshSnapshot())

    watcher = SkillFileWatcher(
        registry,
        poll_interval_seconds=poll_interval_seconds,
        on_change=_on_change,
    )
    pool._watcher_task = asyncio.create_task(watcher.run())  # noqa: SLF001
    pool._watcher = watcher  # noqa: SLF001


__all__ = [
    "PreparedPoolSession",
    "create_started_pool_engine",
    "finalize_resumed_engine",
    "prepare_pool_session",
    "start_skill_watcher",
]
