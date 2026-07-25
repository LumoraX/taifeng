"""真实 AgentEngine operation task 的 release ownership 回归测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng.loop.pool_lifecycle as lifecycle_module
from taifeng.loop.engine import AgentEngine
from taifeng.loop.pool_lifecycle import EnginePoolUnresponsiveError
from taifeng.loop.submission import UserMessage
from tests.loop.test_audit_engine_bootstrap import _JournalCore, _pool

if TYPE_CHECKING:
    from pathlib import Path


async def _task_error(task: asyncio.Task[None]) -> BaseException | None:
    """取回 task 结果，避免失败分支遗留未检索异常。"""
    try:
        await task
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


@pytest.mark.asyncio
# 注：audit 引擎仅 UserMessage/CancelTurn/Shutdown 可派发；Rewind/Resume/spawn-Resume
# 等能力面外 Op 现由动态门在 submit() 前 durable 拒绝（见
# test_unsupported_dynamic_op_is_rejected_before_effect），故不再作为「真实派发收敛」
# 参数——它们不会进入 actor dispatch。此处保留会派发的 UserMessage 与 TTL 定时器两类。
@pytest.mark.parametrize(
    ("handler_name", "operation", "spawn_resume"),
    [
        ("_run_turn_for", UserMessage(text="block"), False),
        ("_ttl_expire_after", None, False),
    ],
)
async def test_release_cancels_and_awaits_real_engine_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler_name: str,
    operation: object | None,
    spawn_resume: bool,
) -> None:
    """五类真实 operation 派发必须先收敛，再写 Journal terminal。"""
    events: list[str] = []
    core = _JournalCore(events)
    pool = _pool(tmp_path, core, events)
    engine = await pool.get_or_create(
        session_id=f"ses-owned-{handler_name}",
        entry_skill_id="entry",
    )
    assert type(engine) is AgentEngine
    started = asyncio.Event()
    escape = asyncio.Event()
    finished = asyncio.Event()
    operation_tasks: list[asyncio.Task[Any]] = []

    async def _blocked_handler(*args: object) -> None:
        """记录真实 actor 派发出的 operation task 生命周期。"""
        del args
        task = asyncio.current_task()
        assert task is not None
        operation_tasks.append(task)
        started.set()
        try:
            await escape.wait()
        except asyncio.CancelledError:
            events.append("operation_cancelled")
            raise
        finally:
            finished.set()

    monkeypatch.setattr(engine, handler_name, _blocked_handler)
    if spawn_resume:
        monkeypatch.setattr(
            engine,
            "_match_suspended_spawn",
            lambda thread_id: object(),
        )
    if operation is None:
        engine._arm_ttl_timer(  # noqa: SLF001
            {
                "record_id": "ttl-owned",
                "thread_id": engine.thread_id,
                "expires_at": 2**31,
            }
        )
    else:
        await engine.submit(operation)  # type: ignore[arg-type]
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await asyncio.wait_for(
        pool.release(f"ses-owned-{handler_name}"),
        timeout=1.0,
    )
    converged_before_release = finished.is_set()
    terminal_after_convergence = (
        "operation_cancelled" in events
        and events.index("operation_cancelled") < events.index("journal_terminal")
    )

    escape.set()
    await asyncio.gather(*operation_tasks, return_exceptions=True)
    await pool.close()

    assert converged_before_release is True
    assert terminal_after_convergence is True
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_unresponsive_real_operation_preserves_pool_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """operation 越过 cancel grace 时不得写 terminal、关 lease 或丢 owner。"""
    events: list[str] = []
    core = _JournalCore(events)
    pool = _pool(tmp_path, core, events)
    session_id = "ses-unresponsive-operation"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    assert type(engine) is AgentEngine
    started = asyncio.Event()
    cancelled = asyncio.Event()
    escape = asyncio.Event()
    operation_tasks: list[asyncio.Task[Any]] = []

    async def _resistant_turn(*args: object) -> None:
        """吞取消直到测试放行，模拟违反 cooperative cancellation 的 turn。"""
        del args
        task = asyncio.current_task()
        assert task is not None
        operation_tasks.append(task)
        started.set()
        while not escape.is_set():
            try:
                await escape.wait()
            except asyncio.CancelledError:
                cancelled.set()

    monkeypatch.setattr(engine, "_run_turn_for", _resistant_turn)
    actor = pool._engine_tasks[session_id]  # noqa: SLF001
    monkeypatch.setattr(
        lifecycle_module,
        "_ACTOR_CONVERGENCE_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_CANCELLATION_GRACE_SECONDS",
        0.01,
    )
    await engine.submit(UserMessage(text="block"))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    release = asyncio.create_task(pool.release(session_id))
    error = await asyncio.wait_for(_task_error(release), timeout=1.0)
    ownership_preserved = (
        session_id in pool._engines  # noqa: SLF001
        and session_id in pool._engine_tasks  # noqa: SLF001
        and session_id in pool._audit_sessions  # noqa: SLF001
        and session_id in pool._release_tasks  # noqa: SLF001
    )
    terminal_count = events.count("journal_terminal")
    close_calls = core.close_calls

    escape.set()
    await asyncio.gather(*operation_tasks, return_exceptions=True)
    await asyncio.wait({actor}, timeout=1.0)
    close_error: BaseException | None = None
    try:
        await pool.close()
    except BaseException as exc:  # noqa: BLE001
        close_error = exc

    assert getattr(error, "code", None) == "engine_pool_release_unresponsive"
    assert getattr(error, "stage", None) == "engine_task"
    assert cancelled.is_set()
    assert ownership_preserved is True
    assert terminal_count == 0
    assert close_calls == 0
    assert getattr(close_error, "code", None) == "engine_pool_release_unresponsive"


@pytest.mark.asyncio
async def test_unresponsive_actor_late_exception_is_retrieved() -> None:
    """actor 越过 grace 后才失败，其异常仍须由 lifecycle callback 检索。"""
    cancelled = asyncio.Event()
    started = asyncio.Event()
    escape = asyncio.Event()

    async def _late_failure() -> None:
        """吞首次取消，放行后以异常终结。"""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await escape.wait()
        raise RuntimeError("late actor failure")

    task = asyncio.create_task(_late_failure())
    await started.wait()
    with pytest.raises(EnginePoolUnresponsiveError):
        await lifecycle_module._bounded_actor_convergence(  # noqa: SLF001
            task,
            session_id="ses-late-actor",
            cancel_first=True,
        )
    assert cancelled.is_set()

    escape.set()
    await asyncio.wait({task}, timeout=1.0)
    await asyncio.sleep(0)

    assert task.done()
    assert getattr(task, "_log_traceback", True) is False


@pytest.mark.asyncio
async def test_real_engine_startup_exception_converges_owned_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冷重武装异常也必须经过 run finally，取消并等待已登记 operation。"""
    events: list[str] = []
    core = _JournalCore(events)
    operation_started = asyncio.Event()
    operation_cancelled = asyncio.Event()
    escape = asyncio.Event()
    operation_tasks: list[asyncio.Task[Any]] = []

    async def _failing_rearm(engine: AgentEngine) -> None:
        """先登记 operation，再模拟 actor 启动异常。"""

        async def _operation() -> None:
            task = asyncio.current_task()
            assert task is not None
            operation_tasks.append(task)
            operation_started.set()
            try:
                await escape.wait()
            except asyncio.CancelledError:
                operation_cancelled.set()
                raise

        engine._start_operation(  # noqa: SLF001
            _operation(),
            name="startup",
        )
        await operation_started.wait()
        raise RuntimeError("startup failed")

    monkeypatch.setattr(AgentEngine, "_rearm_ttl_timers_cold", _failing_rearm)
    pool = _pool(tmp_path, core, events)
    await pool.get_or_create(
        session_id="ses-startup-failure",
        entry_skill_id="entry",
    )
    actor = pool._engine_tasks["ses-startup-failure"]  # noqa: SLF001
    await operation_started.wait()
    await asyncio.wait({actor}, timeout=1.0)
    cancelled_before_cleanup = operation_cancelled.is_set()

    escape.set()
    await asyncio.gather(*operation_tasks, return_exceptions=True)
    with pytest.raises(RuntimeError):
        await pool.release("ses-startup-failure")
    await pool.close()

    assert cancelled_before_cleanup is True
