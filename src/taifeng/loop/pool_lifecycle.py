"""EnginePool Session 释放与全池关闭的 cancellation-independent 收敛。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_lifecycle import SessionFinishResult, ThreadTerminalRequest

if TYPE_CHECKING:
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.pool import EnginePool

type _ReleaseStage = Literal["engine_shutdown", "engine_task", "audit_finish"]


class EnginePoolReleaseError(RuntimeError):
    """Engine 生命周期异常，但 Journal finish 已独立收敛。"""

    code = "engine_pool_release_failed"

    def __init__(
        self,
        session_id: str,
        *,
        stage: _ReleaseStage,
        finish_result: SessionFinishResult | None,
    ) -> None:
        """只暴露稳定 session/stage/result，不拼接任意异常文本。"""
        super().__init__(f"{self.code}: session={session_id}, stage={stage}")
        self.session_id = session_id
        self.stage = stage
        self.finish_result = finish_result


class EnginePoolSessionReleasingError(RuntimeError):
    """同一 Session 已进入 release，不能再返回或创建 Engine。"""

    code = "engine_pool_session_releasing"

    def __init__(self, session_id: str) -> None:
        """只暴露稳定 code/session id。"""
        super().__init__(f"{self.code}: session={session_id}")
        self.session_id = session_id


@dataclass(frozen=True, slots=True)
class _ReleaseSnapshot:
    """release worker 独占的对象 identity 快照。"""

    session_id: str
    engine: AgentEngine | None
    actor_task: asyncio.Task[None] | None
    audit_state: AuditedSessionState | None


@dataclass(frozen=True, slots=True)
class _FirstFailure:
    """记录首个失败 stage/cause，终结后再稳定化。"""

    stage: _ReleaseStage
    cause: BaseException


async def release_pool_session(
    pool: EnginePool,
    session_id: str,
    *,
    force: bool,
) -> None:
    """共享一个 release worker；caller cancel 只中止等待，不中止收敛。"""
    async with pool._lock:  # noqa: SLF001
        worker = pool._release_tasks.get(session_id)  # noqa: SLF001
        if worker is None:
            snapshot = _claim_release_snapshot(pool, session_id, force=force)
            if snapshot is None:
                return
            worker = asyncio.create_task(
                _drive_release(pool, snapshot),
                name=f"pool-release:{session_id}",
            )
            worker.add_done_callback(_consume_task_exception)
            pool._release_tasks[session_id] = worker  # noqa: SLF001
    await asyncio.shield(worker)


def _claim_release_snapshot(
    pool: EnginePool,
    session_id: str,
    *,
    force: bool,
) -> _ReleaseSnapshot | None:
    """在 pool lock 内检查保活闸并保留 ownership 到 worker 收尾。"""
    engine = pool._engines.get(session_id)  # noqa: SLF001
    if engine is not None and not force and engine.has_live_spawns():
        return None
    actor_task = pool._engine_tasks.get(session_id)  # noqa: SLF001
    audit_state = pool._audit_sessions.get(session_id)  # noqa: SLF001
    if engine is None and actor_task is None and audit_state is None:
        return None
    return _ReleaseSnapshot(session_id, engine, actor_task, audit_state)


async def _drive_release(pool: EnginePool, snapshot: _ReleaseSnapshot) -> None:
    """无论 Engine 阶段如何失败，都尝试唯一 finish 后再移除 ownership。"""
    first = await _stop_engine(snapshot)
    finish_result: SessionFinishResult | None = None
    try:
        finish_result = await _finish_audited_session(snapshot)
    except BaseException as exc:  # noqa: BLE001
        first = first or _FirstFailure("audit_finish", exc)
    await _drop_released_ownership(pool, snapshot)
    if first is not None:
        raise EnginePoolReleaseError(
            snapshot.session_id,
            stage=first.stage,
            finish_result=finish_result,
        ) from first.cause
    if finish_result is not None and (
        not finish_result.audit_complete or not finish_result.lease_released
    ):
        raise AuditSessionReleaseError(
            snapshot.session_id,
            finish_result=finish_result,
        )


async def _stop_engine(snapshot: _ReleaseSnapshot) -> _FirstFailure | None:
    """依次 shutdown/等待 actor，保留首错但不跳过后续阶段。"""
    first: _FirstFailure | None = None
    if snapshot.engine is not None:
        try:
            await snapshot.engine.shutdown()
        except BaseException as exc:  # noqa: BLE001
            first = _FirstFailure("engine_shutdown", exc)
    if snapshot.actor_task is not None:
        try:
            await asyncio.wait_for(snapshot.actor_task, timeout=5.0)
        except BaseException as exc:  # noqa: BLE001
            first = first or _FirstFailure("engine_task", exc)
    return first


async def _finish_audited_session(
    snapshot: _ReleaseSnapshot,
) -> SessionFinishResult | None:
    """以确定 root thread 请求调用 coordinator 的唯一 finish。"""
    state = snapshot.audit_state
    if state is None:
        return None
    return await state.coordinator.finish(
        thread_terminals=(
            ThreadTerminalRequest(
                thread_id=state.thread_id,
                status="complete",
                end_reason="session_released",
            ),
        ),
        reason="session_released",
        status="complete",
    )


async def _drop_released_ownership(
    pool: EnginePool,
    snapshot: _ReleaseSnapshot,
) -> None:
    """finish 已有确定结果后，按 identity 移除缓存与 release worker。"""
    async with pool._lock:  # noqa: SLF001
        if pool._engines.get(snapshot.session_id) is snapshot.engine:  # noqa: SLF001
            pool._engines.pop(snapshot.session_id, None)  # noqa: SLF001
        if pool._engine_tasks.get(snapshot.session_id) is snapshot.actor_task:  # noqa: SLF001
            pool._engine_tasks.pop(snapshot.session_id, None)  # noqa: SLF001
        if pool._audit_sessions.get(snapshot.session_id) is snapshot.audit_state:  # noqa: SLF001
            pool._audit_sessions.pop(snapshot.session_id, None)  # noqa: SLF001
        current = asyncio.current_task()
        if pool._release_tasks.get(snapshot.session_id) is current:  # noqa: SLF001
            pool._release_tasks.pop(snapshot.session_id, None)  # noqa: SLF001


async def close_engine_pool(pool: EnginePool) -> None:
    """共享 close worker，确保所有清理阶段 best-effort 执行。"""
    async with pool._lock:  # noqa: SLF001
        worker = pool._close_task  # noqa: SLF001
        if worker is None:
            pool._closed = True  # noqa: SLF001
            worker = asyncio.create_task(
                _drive_pool_close(pool),
                name="engine-pool-close",
            )
            worker.add_done_callback(_consume_task_exception)
            pool._close_task = worker  # noqa: SLF001
    await asyncio.shield(worker)


async def _drive_pool_close(pool: EnginePool) -> None:
    """保留首错，同时清理全部 Session、watcher、root、hook 与 store。"""
    first: BaseException | None = None
    async with pool._lock:  # noqa: SLF001
        session_ids = tuple(
            dict.fromkeys(
                (
                    *pool._engines,  # noqa: SLF001
                    *pool._audit_sessions,  # noqa: SLF001
                    *pool._release_tasks,  # noqa: SLF001
                )
            )
        )
    for session_id in session_ids:
        try:
            await release_pool_session(pool, session_id, force=True)
        except BaseException as exc:  # noqa: BLE001
            first = first or exc
    first = await _cleanup_pool_resources(pool, first)
    if first is not None:
        raise first


async def _cleanup_pool_resources(
    pool: EnginePool,
    first: BaseException | None,
) -> BaseException | None:
    """逐段清理非 Session 资源，每段失败均不阻断后续资源。"""
    first = await _stop_watcher(pool, first)
    pool._root_cancel.cancel()  # noqa: SLF001
    if pool._hook_runner is not None:  # noqa: SLF001
        try:
            await pool._hook_runner.shutdown(grace_seconds=5.0)  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001
            first = first or exc
    try:
        await pool._store.close()  # noqa: SLF001
    except BaseException as exc:  # noqa: BLE001
        first = first or exc
    return first


async def _stop_watcher(
    pool: EnginePool,
    first: BaseException | None,
) -> BaseException | None:
    """停止 watcher；异常只记录为首错，不阻断 hook/store 清理。"""
    task = pool._watcher_task  # noqa: SLF001
    if task is None or task.done():
        return first
    try:
        if pool._watcher is not None:  # noqa: SLF001
            pool._watcher.stop()  # noqa: SLF001
    except BaseException as exc:  # noqa: BLE001
        first = first or exc
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    except BaseException as exc:  # noqa: BLE001
        first = first or exc
    return first


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """取走后台 worker 异常，避免 caller 已取消时产生未检索告警。"""
    if not task.cancelled():
        task.exception()


__all__ = [
    "EnginePoolReleaseError",
    "EnginePoolSessionReleasingError",
    "close_engine_pool",
    "release_pool_session",
]
