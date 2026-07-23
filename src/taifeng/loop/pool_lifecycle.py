"""EnginePool Session 释放与全池关闭的 cancellation-independent 收敛。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from taifeng.conversation.journal.records import StableErrorV1
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_lifecycle import SessionFinishResult, ThreadTerminalRequest

if TYPE_CHECKING:
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.pool import EnginePool

type _ReleaseStage = Literal["engine_shutdown", "engine_task", "audit_finish"]
type _EngineStage = Literal["engine_shutdown", "engine_task"]

_ENGINE_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_ACTOR_CONVERGENCE_TIMEOUT_SECONDS = 5.0
_WATCHER_STOP_TIMEOUT_SECONDS = 3.0
_CANCELLATION_GRACE_SECONDS = 1.0


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


class EnginePoolUnresponsiveError(RuntimeError):
    """内部 lifecycle task 违反 cooperative cancellation 契约。"""

    code = "engine_pool_release_unresponsive"

    def __init__(self, session_id: str, *, stage: _EngineStage) -> None:
        """只暴露稳定 session/stage，不携带任意 task 异常文本。"""
        super().__init__(f"{self.code}: session={session_id}, stage={stage}")
        self.session_id = session_id
        self.stage = stage


class EnginePoolWatcherTimeoutError(RuntimeError):
    """watcher 未在 stop/cancel deadline 内收敛。"""

    code = "engine_pool_watcher_timeout"

    def __init__(self) -> None:
        """构造无任意异常文本的稳定 timeout failure。"""
        super().__init__(self.code)


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
    """协作终结后 finish；不响应 invariant violation 原样保留 ownership。"""
    first = await _stop_engine(pool, snapshot)
    finish_result: SessionFinishResult | None = None
    try:
        finish_result = await _finish_audited_session(snapshot, first)
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


async def _stop_engine(
    pool: EnginePool,
    snapshot: _ReleaseSnapshot,
) -> _FirstFailure | None:
    """先观察 actor，再按 graceful/cancel-grace 两阶段终结内部 task。"""
    actor_task = snapshot.actor_task
    actor_finished = actor_task is not None and actor_task.done()
    if actor_task is not None and actor_task.done():
        actor_failure = _done_task_failure(actor_task, stage="engine_task")
        if actor_failure is not None:
            return actor_failure
    first: _FirstFailure | None = None
    unresponsive: EnginePoolUnresponsiveError | None = None
    if snapshot.engine is not None:
        try:
            first = await _bounded_engine_shutdown(pool, snapshot)
        except EnginePoolUnresponsiveError as exc:
            unresponsive = exc
    if actor_task is not None and not actor_finished:
        try:
            actor_failure = await _bounded_actor_convergence(
                actor_task,
                session_id=snapshot.session_id,
                cancel_first=first is not None or unresponsive is not None,
            )
            first = first or actor_failure
        except EnginePoolUnresponsiveError as exc:
            unresponsive = unresponsive or exc
    if unresponsive is not None:
        raise unresponsive
    return first


async def _bounded_engine_shutdown(
    pool: EnginePool,
    snapshot: _ReleaseSnapshot,
) -> _FirstFailure | None:
    """由 session supervisor 持有 shutdown，超时后执行 cancel-grace。"""
    assert snapshot.engine is not None
    task = asyncio.create_task(
        snapshot.engine.shutdown(),
        name=f"pool-engine-shutdown:{snapshot.session_id}",
    )
    _supervise_teardown_task(pool, snapshot.session_id, task)
    done, _ = await asyncio.wait(
        {task},
        timeout=_ENGINE_SHUTDOWN_TIMEOUT_SECONDS,
    )
    if task in done:
        _forget_teardown_task(pool, snapshot.session_id, task)
        return _done_task_failure(task, stage="engine_shutdown")
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_CANCELLATION_GRACE_SECONDS)
    if task not in done:
        raise EnginePoolUnresponsiveError(
            snapshot.session_id,
            stage="engine_shutdown",
        )
    _forget_teardown_task(pool, snapshot.session_id, task)
    return _done_task_failure(task, stage="engine_shutdown") or _FirstFailure(
        "engine_shutdown",
        TimeoutError(),
    )


async def _bounded_actor_convergence(
    task: asyncio.Task[None],
    *,
    session_id: str,
    cancel_first: bool,
) -> _FirstFailure | None:
    """有界观察 actor；超时取消后由 callback 回收最终异常。"""
    if cancel_first:
        task.cancel()
        done, _ = await asyncio.wait(
            {task},
            timeout=_CANCELLATION_GRACE_SECONDS,
        )
        if task not in done:
            task.add_done_callback(_consume_task_exception)
            raise EnginePoolUnresponsiveError(session_id, stage="engine_task")
        return _done_task_failure(task, stage="engine_task")
    done, _ = await asyncio.wait(
        {task},
        timeout=_ACTOR_CONVERGENCE_TIMEOUT_SECONDS,
    )
    if task in done:
        return _done_task_failure(task, stage="engine_task")
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_CANCELLATION_GRACE_SECONDS)
    if task not in done:
        task.add_done_callback(_consume_task_exception)
        raise EnginePoolUnresponsiveError(session_id, stage="engine_task")
    return _done_task_failure(task, stage="engine_task") or _FirstFailure(
        "engine_task",
        TimeoutError(),
    )


def _done_task_failure(
    task: asyncio.Task[object],
    *,
    stage: _ReleaseStage,
) -> _FirstFailure | None:
    """同步检索 done task 结果，稳定映射为对应 lifecycle stage。"""
    try:
        task.result()
    except BaseException as exc:  # noqa: BLE001
        return _FirstFailure(stage, exc)
    return None


async def _finish_audited_session(
    snapshot: _ReleaseSnapshot,
    first: _FirstFailure | None,
) -> SessionFinishResult | None:
    """以确定 root thread 请求调用 coordinator 的唯一 finish。"""
    state = snapshot.audit_state
    if state is None:
        return None
    status = "complete"
    reason = "session_released"
    stable_error: StableErrorV1 | None = None
    if first is not None:
        status = "error"
        reason = f"{first.stage}_failed"
        stable_error = StableErrorV1(
            code=reason,
            class_name=_release_failure_class(first.stage),
            failure_class="lifecycle",
            retryable=False,
        )
    return await state.coordinator.finish(
        thread_terminals=(
            ThreadTerminalRequest(
                thread_id=state.thread_id,
                status=status,
                end_reason=reason,
                stable_error=stable_error,
            ),
        ),
        reason=reason,
        status=status,
    )


def _release_failure_class(stage: _ReleaseStage) -> str:
    """返回不依赖任意异常类型或文本的稳定错误类名。"""
    if stage == "engine_shutdown":
        return "EngineShutdownFailure"
    if stage == "engine_task":
        return "EngineTaskFailure"
    return "AuditFinishFailure"


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
    owned_directory = pool._owned_directory  # noqa: SLF001
    pool._owned_directory = None  # noqa: SLF001
    if owned_directory is not None:
        try:
            await owned_directory.close()
        except BaseException as exc:  # noqa: BLE001
            first = first or exc
    return first


async def _stop_watcher(
    pool: EnginePool,
    first: BaseException | None,
) -> BaseException | None:
    """停止 watcher；异常只记录为首错，不阻断 hook/store 清理。"""
    task = pool._watcher_task  # noqa: SLF001
    if task is None:
        return first
    if task.done():
        pool._watcher_task = None  # noqa: SLF001
        return first or _done_task_exception(task)
    try:
        if pool._watcher is not None:  # noqa: SLF001
            pool._watcher.stop()  # noqa: SLF001
    except BaseException as exc:  # noqa: BLE001
        first = first or exc
    done, _ = await asyncio.wait(
        {task},
        timeout=_WATCHER_STOP_TIMEOUT_SECONDS,
    )
    if task in done:
        pool._watcher_task = None  # noqa: SLF001
        return first or _done_task_exception(task)
    timeout_failure = EnginePoolWatcherTimeoutError()
    first = first or timeout_failure
    task.cancel()
    done, _ = await asyncio.wait(
        {task},
        timeout=_CANCELLATION_GRACE_SECONDS,
    )
    if task not in done:
        task.add_done_callback(lambda done: _forget_watcher_task(pool, done))
        return first
    pool._watcher_task = None  # noqa: SLF001
    _consume_task_exception(task)
    return first


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """取走后台 worker 异常，避免 caller 已取消时产生未检索告警。"""
    _done_task_exception(task)


def _done_task_exception(task: asyncio.Task[None]) -> BaseException | None:
    """检索 done task 的非取消异常。"""
    if task.cancelled():
        return None
    try:
        task.result()
    except asyncio.CancelledError:
        return None
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


def _supervise_teardown_task(
    pool: EnginePool,
    session_id: str,
    task: asyncio.Task[None],
) -> None:
    """由 pool/session supervisor 强持有 teardown task 到确定终态。"""
    tasks = pool._teardown_tasks.setdefault(session_id, set())  # noqa: SLF001
    tasks.add(task)
    task.add_done_callback(
        lambda done: _forget_teardown_task(pool, session_id, done)
    )


def _forget_teardown_task(
    pool: EnginePool,
    session_id: str,
    task: asyncio.Task[None],
) -> None:
    """检索异常并从明确的 pool/session ownership 中移除 done task。"""
    _consume_task_exception(task)
    tasks = pool._teardown_tasks.get(session_id)  # noqa: SLF001
    if tasks is None:
        return
    tasks.discard(task)
    if not tasks:
        pool._teardown_tasks.pop(session_id, None)  # noqa: SLF001


def _forget_watcher_task(pool: EnginePool, task: asyncio.Task[None]) -> None:
    """检索 late watcher 终态并释放 pool ownership。"""
    _consume_task_exception(task)
    if pool._watcher_task is task:  # noqa: SLF001
        pool._watcher_task = None  # noqa: SLF001


__all__ = [
    "EnginePoolReleaseError",
    "EnginePoolSessionReleasingError",
    "EnginePoolUnresponsiveError",
    "EnginePoolWatcherTimeoutError",
    "close_engine_pool",
    "release_pool_session",
]
