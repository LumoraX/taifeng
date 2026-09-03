"""Audited Shutdown 的 detached ownership 与 fatal precedence 复审回归。"""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_shutdown import _submit_shutdown
from taifeng.loop.audit_support import SessionAuditFrozenError
from taifeng.loop.pool_lifecycle import (
    EnginePoolReleaseError,
    EnginePoolUnresponsiveError,
)
from taifeng.loop.submission import Shutdown, Submission
from tests.loop.test_audit_shutdown_lifecycle import (
    _inject_shutdown_acceptance_failure,
    _start_fixed_shutdown,
)
from tests.loop.test_audit_submission_release import _build_release_scenario

if TYPE_CHECKING:
    from pathlib import Path


class _ControlledRunner:
    """收到真实 spawn cancel 后暂停，固定 teardown 顺序窗口。"""

    def __init__(
        self,
        cancel: Any,
        *,
        started: anyio.Event,
        cancel_seen: anyio.Event,
        allow_terminal: anyio.Event,
    ) -> None:
        self.cancel = cancel
        self.started = started
        self.cancel_seen = cancel_seen
        self.allow_terminal = allow_terminal

    async def run(self) -> Any:
        """等待 root cancel，再由测试放行 cancelled outcome。"""
        self.started.set()
        await self.cancel.wait_cancelled()
        self.cancel_seen.set()
        await self.allow_terminal.wait()
        return SimpleNamespace(
            end_reason="cancelled",
            final_text=None,
            error=None,
        )


def _install_controlled_runner(
    engine: Any,
    *,
    started: anyio.Event,
    cancel_seen: anyio.Event,
    allow_terminal: anyio.Event,
) -> None:
    """让 public spawn_skill 仍走真实 _drive_spawn，仅替换 child runner。"""
    def build_controlled_runner(
        _engine: Any,
        _target: Any,
        _child_thread_id: str,
        _seed: Any,
        cancel: Any,
        **_kwargs: Any,
    ) -> _ControlledRunner:
        return _ControlledRunner(
            cancel,
            started=started,
            cancel_seen=cancel_seen,
            allow_terminal=allow_terminal,
        )

    engine._build_child_runner = MethodType(  # type: ignore[method-assign]  # noqa: SLF001
        build_controlled_runner,
        engine,
    )


async def _assert_spawn_blocks_terminal(
    *,
    engine: Any,
    pool: Any,
    core: Any,
    session_id: str,
    handle_id: str,
    shutdown: asyncio.Task[str],
    cancel_seen: anyio.Event,
) -> None:
    """证明 root cancel 后真实 spawn 未收敛时 terminal/close 均不得推进。"""
    with anyio.fail_after(1):
        await cancel_seen.wait()
    await anyio.sleep(0.05)
    assert not shutdown.done()
    assert not core.terminal_entered.is_set()
    assert core.close_calls == 0
    assert session_id in pool._engines  # noqa: SLF001
    assert engine.spawn_status([handle_id])[handle_id]["status"] == "running"
    assert len(engine._spawn._owned_tasks) == 1  # noqa: SLF001


async def _release_spawn_to_terminal(
    *,
    engine: Any,
    pool: Any,
    core: Any,
    session_id: str,
    handle_id: str,
    shutdown: asyncio.Task[str],
    allow_terminal: anyio.Event,
) -> None:
    """放行 spawn 后证明句柄/slot/task 先于 Session terminal 收敛。"""
    allow_terminal.set()
    with anyio.fail_after(1):
        await core.terminal_entered.wait()
    assert engine.spawn_status([handle_id])[handle_id]["status"] == "cancelled"
    assert engine._spawn._owned_tasks == set()  # noqa: SLF001
    assert engine._spawn_registry.snapshot()["active"] == 0  # noqa: SLF001
    assert not shutdown.done()
    assert core.close_calls == 0
    assert session_id in pool._engines  # noqa: SLF001


@pytest.mark.asyncio
async def test_shutdown_acceptance_fatal_precedes_completed_release_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finish 已发布后 EnginePoolReleaseError 不得覆盖 acceptance fatal。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_fatal_release_wrapper"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    state = pool._audit_sessions[session_id]  # noqa: SLF001
    fatal = SystemExit("shutdown-acceptance-fatal")
    _inject_shutdown_acceptance_failure(core, fatal)
    original_owner = engine._audit_finish_owner  # noqa: SLF001
    assert original_owner is not None
    owner_calls: list[int] = []

    async def failing_engine_shutdown() -> None:
        """让 release worker 保留 engine_shutdown first failure。"""
        raise RuntimeError("engine shutdown failed")

    async def counted_owner() -> None:
        """保留真实 pool release wrapper，仅统计 handoff。"""
        owner_calls.append(1)
        await original_owner()

    engine.shutdown = failing_engine_shutdown  # type: ignore[method-assign]
    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _size: "shutdown_fatal_release_wrapper",
    )
    submission = Submission(op=Shutdown())

    with pytest.raises(SystemExit) as raised:
        await _submit_shutdown(
            state,
            submission,
            engine._audited_admission_lock,  # noqa: SLF001
            counted_owner,
        )

    with anyio.fail_after(1):
        with pytest.raises(AuditSessionReleaseError) as retry_caught:
            await _submit_shutdown(
                state,
                submission,
                engine._audited_admission_lock,  # noqa: SLF001
                counted_owner,
            )

    result = retry_caught.value.finish_result
    assert raised.value is fatal
    assert owner_calls == [1]
    assert result.audit_complete is False
    assert result.lease_released is True
    assert result.failure is not None
    assert result.failure.class_name == "SystemExit"
    assert [envelope async for envelope in real_core.load(session_id)][3:] == []
    assert core.close_calls == 1
    assert session_id not in pool._engines  # noqa: SLF001

    await pool.close()
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_unpublished_finish_wrapper_wakes_same_id_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 finish_result 时仍重抛原 fatal，并让 same-id retry fail-fast。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_unpublished_finish"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    state = pool._audit_sessions[session_id]  # noqa: SLF001
    fatal = SystemExit("shutdown-acceptance-fatal")
    _inject_shutdown_acceptance_failure(core, fatal)
    original_owner = engine._audit_finish_owner  # noqa: SLF001
    assert original_owner is not None

    async def unpublished_owner() -> None:
        """模拟 release wrapper 未获得 canonical finish result。"""
        raise EnginePoolReleaseError(
            session_id,
            stage="audit_finish",
            finish_result=None,
        )

    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _size: "shutdown_unpublished_finish",
    )
    submission = Submission(op=Shutdown())

    with pytest.raises(SystemExit) as raised:
        await _submit_shutdown(
            state,
            submission,
            engine._audited_admission_lock,  # noqa: SLF001
            unpublished_owner,
        )
    with anyio.fail_after(1):
        with pytest.raises(SessionAuditFrozenError):
            await _submit_shutdown(
                state,
                submission,
                engine._audited_admission_lock,  # noqa: SLF001
                unpublished_owner,
            )

    assert raised.value is fatal
    snapshot = state.coordinator.snapshot()
    assert snapshot.audit_complete is None
    assert snapshot.lease_released is None
    assert core.close_calls == 0
    assert [envelope async for envelope in real_core.load(session_id)][3:] == []
    with pytest.raises(AuditSessionReleaseError):
        await original_owner()
    await pool.close()


@pytest.mark.asyncio
async def test_unresponsive_finish_owner_preserves_fatal_and_wakes_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 wrapper owner 失败也不得覆盖 acceptance fatal 或挂住 retry。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_unresponsive_finish"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    state = pool._audit_sessions[session_id]  # noqa: SLF001
    fatal = SystemExit("shutdown-acceptance-fatal")
    _inject_shutdown_acceptance_failure(core, fatal)
    original_owner = engine._audit_finish_owner  # noqa: SLF001
    assert original_owner is not None

    async def unresponsive_owner() -> None:
        """模拟真实 pool stop engine 超过 cooperative cancellation grace。"""
        raise EnginePoolUnresponsiveError(
            session_id,
            stage="engine_shutdown",
        )

    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _size: "shutdown_unresponsive_finish",
    )
    submission = Submission(op=Shutdown())
    first_error: BaseException | None = None
    retry_error: BaseException | None = None

    try:
        await _submit_shutdown(
            state,
            submission,
            engine._audited_admission_lock,  # noqa: SLF001
            unresponsive_owner,
        )
    except BaseException as error:  # noqa: BLE001
        first_error = error
    # 1s 是「有没有卡死」的判据上限,不是延迟 SLA:真卡住会一直阻塞到 release
    # 之后,1s 照样判失败;而 50ms 会被负载下的调度抖动误伤(曾在完整套件里红过)。
    with anyio.move_on_after(1.0) as retry_scope:
        try:
            await _submit_shutdown(
                state,
                submission,
                engine._audited_admission_lock,  # noqa: SLF001
                unresponsive_owner,
            )
        except BaseException as error:  # noqa: BLE001
            retry_error = error

    observed = (
        type(first_error).__name__,
        retry_scope.cancel_called,
        type(retry_error).__name__,
    )
    snapshot = state.coordinator.snapshot()
    close_calls = core.close_calls
    records = [envelope async for envelope in real_core.load(session_id)][3:]
    with pytest.raises(AuditSessionReleaseError):
        await original_owner()
    await pool.close()

    assert first_error is fatal, f"observed={observed}"
    assert not retry_scope.cancel_called, f"observed={observed}"
    assert isinstance(retry_error, SessionAuditFrozenError), f"observed={observed}"
    assert snapshot.audit_complete is None
    assert snapshot.lease_released is None
    assert close_calls == 0
    assert records == []


@pytest.mark.asyncio
async def test_explicit_audited_shutdown_forces_live_spawn_convergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 Shutdown 等真实 detached task 收敛后才 terminal/close。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_live_spawn"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    with anyio.fail_after(1):
        await engine._spawn._await_root_cancel_ready()  # noqa: SLF001
    runner_started = anyio.Event()
    cancel_seen = anyio.Event()
    allow_spawn_terminal = anyio.Event()
    _install_controlled_runner(
        engine,
        started=runner_started,
        cancel_seen=cancel_seen,
        allow_terminal=allow_spawn_terminal,
    )
    spawned = await engine.spawn_skill(
        skill_id="child",
        args={"mode": "shutdown-order"},
        reason="shutdown-order",
    )
    handle_id = spawned["handle_id"]
    with anyio.fail_after(1):
        await runner_started.wait()
    await pool.release(session_id)
    assert session_id in pool._engines  # noqa: SLF001
    assert engine.spawn_status([handle_id])[handle_id]["status"] == "running"
    assert core.close_calls == 0
    shutdown = await _start_fixed_shutdown(
        engine,
        monkeypatch,
        "shutdown_live_spawn",
    )
    try:
        await _assert_spawn_blocks_terminal(
            engine=engine,
            pool=pool,
            core=core,
            session_id=session_id,
            handle_id=handle_id,
            shutdown=shutdown,
            cancel_seen=cancel_seen,
        )
        await _release_spawn_to_terminal(
            engine=engine,
            pool=pool,
            core=core,
            session_id=session_id,
            handle_id=handle_id,
            shutdown=shutdown,
            allow_terminal=allow_spawn_terminal,
        )

        core.allow_terminal.set()
        shutdown_id = await shutdown
        assert shutdown_id == "sub_shutdown_live_spawn"
        assert session_id not in pool._engines  # noqa: SLF001
        assert session_id not in pool._engine_tasks  # noqa: SLF001
        assert session_id not in pool._audit_sessions  # noqa: SLF001
        committed = [envelope async for envelope in real_core.load(session_id)]
        assert [envelope.record_type for envelope in committed[3:]] == [
            "submission_accepted",
            "submission_applied",
            "thread_terminal",
            "session_ended",
        ]
        assert committed[3].submission_id == shutdown_id
        assert core.close_calls == 1
    finally:
        allow_spawn_terminal.set()
        core.allow_terminal.set()
        await asyncio.gather(shutdown, return_exceptions=True)
        await pool.close()
