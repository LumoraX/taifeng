"""EnginePool lifecycle 与 factory ownership 的复审回归测试。"""

from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING, Any

import pytest

import taifeng.loop.pool as pool_module
import taifeng.loop.pool_lifecycle as lifecycle_module
from taifeng.conversation.journal.materialization import _TARGETS
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.providers.sim import SimClient
from taifeng.loop.pool import EnginePool
from taifeng.loop.pool_lifecycle import EnginePoolReleaseError
from taifeng.tool.registry import ToolRegistry
from tests.loop.test_audit_engine_bootstrap import (
    _EngineSpy,
    _JournalCore,
    _pool,
    _Registry,
)

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.loop.cancellation import CancellationToken


class _ResistantShutdownEngine(_EngineSpy):
    """shutdown 取消后仍等待外部放行，模拟不协作的第三方清理。"""

    def __init__(
        self,
        *,
        shutdown_started: asyncio.Event,
        shutdown_escape: asyncio.Event,
        actor_exit: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._shutdown_started = shutdown_started
        self._shutdown_escape = shutdown_escape
        self._actor_exit = actor_exit
        self.shutdown_task: asyncio.Task[object] | None = None

    async def run(self, cancel: CancellationToken) -> None:
        """保持 actor 存活，直到 release 取消或测试显式放行。"""
        del cancel
        self._events.append("actor_run")
        await self._actor_exit.wait()

    async def shutdown(self) -> None:
        """吞掉一次取消，证明 release 不会无限等待该 coroutine。"""
        self._events.append("engine_shutdown")
        self.shutdown_task = asyncio.current_task()
        self._shutdown_started.set()
        try:
            await self._shutdown_escape.wait()
        except asyncio.CancelledError:
            await self._shutdown_escape.wait()


class _ResistantActorEngine(_EngineSpy):
    """actor 取消后仍等待放行，模拟取消不协作的 actor。"""

    def __init__(
        self,
        *,
        actor_cancelled: asyncio.Event,
        actor_escape: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._actor_cancelled = actor_cancelled
        self._actor_escape = actor_escape

    async def run(self, cancel: CancellationToken) -> None:
        """吞掉一次取消，直到测试放行。"""
        del cancel
        self._events.append("actor_run")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._actor_cancelled.set()
            await self._actor_escape.wait()


class _OrphaningShutdownEngine(_EngineSpy):
    """shutdown 吞取消后等待无外部 owner 的 Future。"""

    def __init__(self, *, actor_exit: asyncio.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._actor_exit = actor_exit

    async def run(self, cancel: CancellationToken) -> None:
        """保持 actor 存活到 release 进入 shutdown。"""
        del cancel
        self._events.append("actor_run")
        await self._actor_exit.wait()

    async def shutdown(self) -> None:
        """吞掉取消后永久等待，暴露 background task 强 ownership 缺口。"""
        self._events.append("engine_shutdown")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()


async def _release_exception(task: asyncio.Task[None]) -> BaseException | None:
    """取回 release 结果，避免 RED 清理阶段遗留后台异常。"""
    try:
        await task
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


@pytest.mark.asyncio
async def test_shutdown_timeout_is_bounded_when_shutdown_suppresses_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown 不响应取消时，release 仍须有界终结并释放审计 ownership。"""
    events: list[str] = []
    core = _JournalCore(events)
    shutdown_started = asyncio.Event()
    shutdown_escape = asyncio.Event()
    actor_exit = asyncio.Event()
    created: list[_ResistantShutdownEngine] = []

    def _factory(**kwargs: Any) -> _ResistantShutdownEngine:
        engine = _ResistantShutdownEngine(
            events=events,
            shutdown_started=shutdown_started,
            shutdown_escape=shutdown_escape,
            actor_exit=actor_exit,
            **kwargs,
        )
        created.append(engine)
        return engine

    monkeypatch.setattr(pool_module, "AgentEngine", _factory)
    monkeypatch.setattr(
        lifecycle_module,
        "_ENGINE_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_ACTOR_CONVERGENCE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    pool = _pool(tmp_path, core, events)
    await pool.get_or_create(session_id="ses-stuck-shutdown", entry_skill_id="entry")
    release = asyncio.create_task(pool.release("ses-stuck-shutdown"))
    await shutdown_started.wait()

    done, _ = await asyncio.wait({release}, timeout=0.2)
    completed_in_bound = release in done
    shutdown_escape.set()
    actor_exit.set()
    actor_task = pool._engine_tasks.get("ses-stuck-shutdown")  # noqa: SLF001
    if actor_task is not None and not actor_task.done():
        actor_task.cancel()
    error = await asyncio.wait_for(_release_exception(release), timeout=1.0)
    shutdown_task = created[0].shutdown_task
    if shutdown_task is not None and shutdown_task is not release:
        await asyncio.wait({shutdown_task}, timeout=1.0)

    assert completed_in_bound is True
    assert isinstance(error, EnginePoolReleaseError)
    assert error.stage == "engine_shutdown"
    assert core.close_calls == 1
    assert "ses-stuck-shutdown" not in pool._audit_sessions  # noqa: SLF001
    await pool.close()


@pytest.mark.asyncio
async def test_timed_out_shutdown_task_is_retained_until_it_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试侧无强引用时，超时 shutdown task 也不能被 GC 提前销毁。"""
    events: list[str] = []
    core = _JournalCore(events)
    actor_exit = asyncio.Event()
    contexts: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _OrphaningShutdownEngine(
            events=events,
            actor_exit=actor_exit,
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_ENGINE_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_ACTOR_CONVERGENCE_TIMEOUT_SECONDS",
        0.01,
    )
    pool = _pool(tmp_path, core, events)
    await pool.get_or_create(session_id="ses-orphan-shutdown", entry_skill_id="entry")
    loop.set_exception_handler(lambda unused_loop, context: contexts.append(context))
    try:
        with pytest.raises(EnginePoolReleaseError):
            await pool.release("ses-orphan-shutdown")
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)
        actor_exit.set()

    destroyed = [
        context
        for context in contexts
        if context.get("message") == "Task was destroyed but it is pending!"
    ]
    assert destroyed == []
    drains = tuple(lifecycle_module._BACKGROUND_DRAINS)  # noqa: SLF001
    for task in drains:
        task.cancel()
    await asyncio.gather(*drains, return_exceptions=True)
    await pool.close()


@pytest.mark.asyncio
async def test_actor_timeout_is_bounded_when_actor_suppresses_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """actor 不响应取消时，release 不得在取消等待上失去时间上界。"""
    events: list[str] = []
    core = _JournalCore(events)
    actor_cancelled = asyncio.Event()
    actor_escape = asyncio.Event()

    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _ResistantActorEngine(
            events=events,
            actor_cancelled=actor_cancelled,
            actor_escape=actor_escape,
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_ACTOR_CONVERGENCE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    pool = _pool(tmp_path, core, events)
    await pool.get_or_create(session_id="ses-stuck-actor", entry_skill_id="entry")
    actor_task = pool._engine_tasks["ses-stuck-actor"]  # noqa: SLF001
    release = asyncio.create_task(pool.release("ses-stuck-actor"))

    done, _ = await asyncio.wait({release}, timeout=0.2)
    completed_in_bound = release in done
    actor_escape.set()
    if not actor_task.done():
        actor_task.cancel()
    error = await asyncio.wait_for(_release_exception(release), timeout=1.0)
    await asyncio.wait({actor_task}, timeout=1.0)

    assert completed_in_bound is True
    assert actor_cancelled.is_set()
    assert isinstance(error, EnginePoolReleaseError)
    assert error.stage == "engine_task"
    assert core.close_calls == 1
    assert "ses-stuck-actor" not in pool._audit_sessions  # noqa: SLF001
    await pool.close()


class _DirectorySpy:
    """只观察 factory-owned/caller-owned directory 的 close ownership。"""

    def __init__(self, *, close_failure: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_failure = close_failure

    async def close(self) -> None:
        """记录关闭，可注入 cleanup failure。"""
        self.close_calls += 1
        if self.close_failure is not None:
            raise self.close_failure


class _HookRunnerSpy:
    """记录 factory cleanup 是否收敛已创建的 HookRunner。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.shutdown_calls = 0

    async def shutdown(self, *, grace_seconds: float) -> None:
        """记录关闭次数。"""
        del grace_seconds
        self.shutdown_calls += 1


@pytest.mark.asyncio
async def test_factory_owned_directory_closes_once_but_injected_directory_does_not(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常 close 只关闭 factory 创建的额外 directory，且重复 close 幂等。"""
    owned = _DirectorySpy()
    monkeypatch.setattr(
        pool_module,
        "SqliteThreadDirectory",
        lambda *args, **kwargs: owned,
    )
    pool = await EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "owned",
        model_client=SimClient(turns=[]),
        compressors=[],
    )

    await pool.close()
    await pool.close()

    injected = _DirectorySpy()
    injected_pool = await EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "injected",
        model_client=SimClient(turns=[]),
        compressors=[],
        thread_directory=injected,  # type: ignore[arg-type]
    )
    await injected_pool.close()

    assert owned.close_calls == 1
    assert injected.close_calls == 0


@pytest.mark.asyncio
async def test_directory_constructor_failure_cleans_created_store(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """store 创建后的 directory 构造失败仍须移除 projection handle。"""
    root = (tmp_path / "directory-failure").resolve()
    failure = RuntimeError("directory-construction-sentinel")

    def _fail_directory(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise failure

    monkeypatch.setattr(pool_module, "SqliteThreadDirectory", _fail_directory)

    with pytest.raises(RuntimeError) as caught:
        await EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=root,
            model_client=SimClient(turns=[]),
            compressors=[],
        )

    assert caught.value is failure
    assert root not in _TARGETS


@pytest.mark.asyncio
async def test_hook_runner_failure_cleans_store_and_owned_directory_without_masking(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HookRunner 构造失败后，cleanup failure 不能覆盖原始创建异常。"""
    root = (tmp_path / "hook-failure").resolve()
    failure = RuntimeError("hook-construction-sentinel")
    owned = _DirectorySpy(close_failure=RuntimeError("cleanup-sentinel"))

    monkeypatch.setattr(
        pool_module,
        "SqliteThreadDirectory",
        lambda *args, **kwargs: owned,
    )

    def _fail_hook(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise failure

    monkeypatch.setattr(pool_module, "HookRunner", _fail_hook)

    with pytest.raises(RuntimeError) as caught:
        await EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=root,
            model_client=SimClient(turns=[]),
            compressors=[],
        )

    assert caught.value is failure
    assert owned.close_calls == 1
    assert root not in _TARGETS


@pytest.mark.asyncio
async def test_tool_registration_failure_cleans_all_created_factory_resources(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool 注册失败也位于统一 factory cleanup 边界内。"""
    root = (tmp_path / "tool-failure").resolve()
    failure = RuntimeError("tool-registration-sentinel")
    owned = _DirectorySpy()
    hook = _HookRunnerSpy()

    class _FailingRegistry:
        """首个 built-in 注册即失败。"""

        def register(self, spec: object) -> None:
            del spec
            raise failure

    monkeypatch.setattr(
        pool_module,
        "SqliteThreadDirectory",
        lambda *args, **kwargs: owned,
    )
    monkeypatch.setattr(pool_module, "HookRunner", lambda *args, **kwargs: hook)
    monkeypatch.setattr(pool_module, "ToolRegistry", _FailingRegistry)

    with pytest.raises(RuntimeError) as caught:
        await EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=root,
            model_client=SimClient(turns=[]),
            compressors=[],
        )

    assert caught.value is failure
    assert hook.shutdown_calls == 1
    assert owned.close_calls == 1
    assert root not in _TARGETS


@pytest.mark.asyncio
async def test_watcher_start_failure_closes_constructed_pool_resources(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watcher 成功交接前失败时，已构造 pool 仍由 factory 负责关闭。"""
    root = (tmp_path / "watcher-failure").resolve()
    failure = RuntimeError("watcher-start-sentinel")
    owned = _DirectorySpy()
    hook = _HookRunnerSpy()

    async def _fail_watcher(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise failure

    monkeypatch.setattr(
        pool_module,
        "SqliteThreadDirectory",
        lambda *args, **kwargs: owned,
    )
    monkeypatch.setattr(pool_module, "HookRunner", lambda *args, **kwargs: hook)
    monkeypatch.setattr(pool_module, "start_skill_watcher", _fail_watcher)

    with pytest.raises(RuntimeError) as caught:
        await EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=root,
            model_client=SimClient(turns=[]),
            compressors=[],
            auto_watch_skills=True,
        )

    assert caught.value is failure
    assert hook.shutdown_calls == 1
    assert owned.close_calls == 1
    assert root not in _TARGETS


@pytest.mark.asyncio
async def test_close_consumes_already_failed_watcher_and_keeps_cleaning(
    tmp_path: Path,
) -> None:
    """done watcher 的异常是 close 首错，但 store/root 仍须清理。"""
    root = (tmp_path / "failed-watcher").resolve()
    store = JsonlMessageStore(root)
    pool = EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=SimClient(turns=[]),
        store=store,
        tool_registry=ToolRegistry(),
        compressors=[],
    )
    failure = RuntimeError("watcher-sentinel")

    async def _fail() -> None:
        raise failure

    task = asyncio.create_task(_fail())
    await asyncio.sleep(0)
    pool._watcher_task = task  # noqa: SLF001

    try:
        with pytest.raises(RuntimeError) as caught:
            await pool.close()
        retrieved_by_close = not task._log_traceback  # noqa: SLF001
    finally:
        if task.done() and task._log_traceback:  # noqa: SLF001
            task.exception()

    assert caught.value is failure
    assert retrieved_by_close is True
    assert pool._root_cancel.is_cancelled is True  # noqa: SLF001
    assert root not in _TARGETS
