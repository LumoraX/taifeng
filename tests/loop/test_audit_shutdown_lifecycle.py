"""Audited Shutdown 与 EnginePool 生命周期所有权集成测试。"""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.conversation.journal.records import (
    SubmissionAcceptedV1,
    SubmissionAppliedV1,
)
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.providers.sim import SimClient
from taifeng.loop.audit import SessionFinishingError
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.pool import EnginePool
from taifeng.loop.submission import Shutdown
from taifeng.tool.registry import ToolRegistry
from tests.loop.test_audit_engine_bootstrap import _Registry
from tests.loop.test_audit_submission_release import _build_release_scenario

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.loop.audit import SessionAuditCoordinator, SessionFinishResult


def _count_finish_calls(coordinator: SessionAuditCoordinator) -> list[int]:
    """保留真实 coordinator 行为，只统计 EnginePool 发起的 finish 次数。"""
    calls: list[int] = []
    original = coordinator.finish

    async def counted_finish(
        _coordinator: SessionAuditCoordinator,
        **kwargs: Any,
    ) -> SessionFinishResult:
        calls.append(1)
        return await original(**kwargs)

    coordinator.finish = MethodType(  # type: ignore[method-assign]
        counted_finish,
        coordinator,
    )
    return calls


async def _start_fixed_shutdown(
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> asyncio.Task[str]:
    """以稳定 public Submission id 启动 Shutdown，并让其先进入 admission。"""
    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _size: token,
    )
    task = asyncio.create_task(engine.submit(Shutdown()))
    await anyio.lowlevel.checkpoint()
    return task


def _gate_shutdown_acceptance(core: Any) -> tuple[anyio.Event, anyio.Event]:
    """在真实 core dispatch 前暂停 Shutdown acceptance，固定竞态胜者。"""
    entered = anyio.Event()
    allow = anyio.Event()
    original = core.append_batch

    async def gated_append(
        _core: Any,
        records: tuple[Any, ...],
        **kwargs: Any,
    ) -> Any:
        if any(
            record.record_type == "submission_accepted"
            and record.payload.get("op_kind") == "shutdown"
            for record in records
        ):
            entered.set()
            await allow.wait()
        return await original(records, **kwargs)

    core.append_batch = MethodType(gated_append, core)
    return entered, allow


def _gate_same_id_replay(
    engine: Any,
    coordinator: SessionAuditCoordinator,
) -> tuple[list[int], anyio.Event, anyio.Event]:
    """暂停 same-id replay，并拒绝它第二次请求 pool owner。"""
    original_owner = engine._audit_finish_owner  # noqa: SLF001
    assert original_owner is not None
    owner_calls: list[int] = []
    replay_entered = anyio.Event()
    allow_replay = anyio.Event()
    original_admit = coordinator.admit_shutdown

    async def gated_admit(record: Any) -> bool:
        owner = await original_admit(record)
        if not owner:
            replay_entered.set()
            await allow_replay.wait()
        return owner

    async def counted_owner() -> None:
        if owner_calls:
            pytest.fail("same-id retry requested pool owner again")
        owner_calls.append(1)
        await original_owner()

    coordinator.admit_shutdown = gated_admit  # type: ignore[method-assign]
    engine._audit_finish_owner = counted_owner  # noqa: SLF001
    return owner_calls, replay_entered, allow_replay


def _assert_unique_shutdown_records(
    committed: list[Any],
    finish_calls: list[int],
    close_calls: int,
) -> None:
    """断言首个 Shutdown 独占 acceptance、applied 与 terminal。"""
    acceptances = [
        envelope
        for envelope in committed
        if envelope.record_type == "submission_accepted"
        and SubmissionAcceptedV1.model_validate(envelope.payload).op_kind
        == "shutdown"
    ]
    assert [envelope.submission_id for envelope in acceptances] == [
        "sub_shutdown_first"
    ]
    assert sum(
        envelope.record_type == "submission_applied"
        and envelope.submission_id == "sub_shutdown_first"
        for envelope in committed
    ) == 1
    assert sum(
        envelope.record_type == "session_ended" for envelope in committed
    ) == 1
    assert len(finish_calls) == 1
    assert close_calls == 1


@pytest.mark.asyncio
async def test_release_racing_shutdown_uses_one_finish_and_stable_terminal_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown 先登记后与 release 共享唯一 finish、terminal batch 与 close。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_race"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    coordinator = pool._audit_sessions[session_id].coordinator  # noqa: SLF001
    finish_calls = _count_finish_calls(coordinator)
    acceptance_entered, allow_acceptance = _gate_shutdown_acceptance(core)
    shutdown_id = "sub_shutdown_race"
    shutdown = await _start_fixed_shutdown(
        engine,
        monkeypatch,
        "shutdown_race",
    )
    with anyio.fail_after(1):
        await acceptance_entered.wait()
    release = asyncio.create_task(pool.release(session_id))
    await anyio.lowlevel.checkpoint()
    allow_acceptance.set()

    try:
        await core.terminal_entered.wait()
        before_terminal = [
            envelope async for envelope in real_core.load(session_id)
        ]
        accepted = [
            envelope
            for envelope in before_terminal
            if envelope.record_type == "submission_accepted"
        ]
        assert [envelope.submission_id for envelope in accepted] == [shutdown_id]
        payload = SubmissionAcceptedV1.model_validate(accepted[0].payload)
        assert payload.op_kind == "shutdown"
        assert not shutdown.done()

        core.allow_terminal.set()
        assert await shutdown == shutdown_id
        await release
    finally:
        allow_acceptance.set()
        core.allow_terminal.set()
        await asyncio.gather(shutdown, release, return_exceptions=True)

    committed = [envelope async for envelope in real_core.load(session_id)]
    assert [envelope.record_type for envelope in committed[3:]] == [
        "submission_accepted",
        "submission_applied",
        "thread_terminal",
        "session_ended",
    ]
    lifecycle_id = f"{session_id}:lifecycle:end"
    assert [envelope.operation_id for envelope in committed[-3:]] == [
        shutdown_id,
        lifecycle_id,
        lifecycle_id,
    ]
    assert [envelope.record_id for envelope in committed[-2:]] == [
        f"{lifecycle_id}:thread_terminal:none:0",
        f"{lifecycle_id}:session_ended:none:0",
    ]
    applied = SubmissionAppliedV1.model_validate(committed[-3].payload)
    assert applied.terminal_record_ids == tuple(
        envelope.record_id for envelope in committed[-2:]
    )
    assert len(finish_calls) == 1
    assert core.close_calls == 1

    await pool.close()
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_id_is_unique_and_same_id_retry_reads_old_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同 id acceptance 前拒绝；同 id retry 不再次请求 pool owner。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_identity"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    coordinator = pool._audit_sessions[session_id].coordinator  # noqa: SLF001
    finish_calls = _count_finish_calls(coordinator)
    acceptance_entered, allow_acceptance = _gate_shutdown_acceptance(core)
    owner_calls, replay_entered, allow_replay = _gate_same_id_replay(
        engine,
        coordinator,
    )
    first = await _start_fixed_shutdown(engine, monkeypatch, "shutdown_first")
    second: asyncio.Task[str] | None = None
    retry: asyncio.Task[str] | None = None
    with anyio.fail_after(1):
        await acceptance_entered.wait()
    allow_acceptance.set()

    try:
        await core.terminal_entered.wait()
        assert owner_calls == [1]

        second = await _start_fixed_shutdown(
            engine,
            monkeypatch,
            "shutdown_second",
        )
        with pytest.raises(SessionFinishingError):
            await second

        retry = await _start_fixed_shutdown(
            engine,
            monkeypatch,
            "shutdown_first",
        )
        with anyio.fail_after(1):
            await replay_entered.wait()
        allow_replay.set()
        await anyio.lowlevel.checkpoint()
        assert not retry.done()
        assert owner_calls == [1]

        core.allow_terminal.set()
        assert await first == "sub_shutdown_first"
        assert await retry == "sub_shutdown_first"
    finally:
        allow_acceptance.set()
        allow_replay.set()
        core.allow_terminal.set()
        for task in (second, retry):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            first,
            *(task for task in (second, retry) if task is not None),
            return_exceptions=True,
        )

    committed = [envelope async for envelope in real_core.load(session_id)]
    _assert_unique_shutdown_records(committed, finish_calls, core.close_calls)

    await pool.close()
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_release_winner_rejects_shutdown_before_durable_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """release 先进入 FINISHING 后，Shutdown 不加入旧 finish future。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_release_wins"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    coordinator = pool._audit_sessions[session_id].coordinator  # noqa: SLF001
    finish_calls = _count_finish_calls(coordinator)
    original_owner = engine._audit_finish_owner  # noqa: SLF001
    assert original_owner is not None
    owner_calls: list[int] = []

    async def counted_owner() -> None:
        """晚到 Shutdown 不得请求 pool owner。"""
        owner_calls.append(1)
        await original_owner()

    engine._audit_finish_owner = counted_owner  # noqa: SLF001
    release = asyncio.create_task(pool.release(session_id))
    try:
        await core.terminal_entered.wait()
        before = [envelope async for envelope in real_core.load(session_id)]
        monkeypatch.setattr(
            "taifeng.loop.submission.secrets.token_hex",
            lambda _size: "shutdown_late",
        )

        with pytest.raises(SessionFinishingError) as caught:
            await engine.submit(Shutdown())

        assert caught.value.session_id == session_id
        assert caught.value.lifecycle.value == "finishing"
        assert owner_calls == []
        assert [envelope async for envelope in real_core.load(session_id)] == before

        core.allow_terminal.set()
        await release
    finally:
        core.allow_terminal.set()
        await asyncio.gather(release, return_exceptions=True)

    committed = [envelope async for envelope in real_core.load(session_id)]
    assert not any(
        envelope.record_type == "submission_accepted"
        and envelope.submission_id == "sub_shutdown_late"
        for envelope in committed
    )
    assert sum(
        envelope.record_type == "session_ended" for envelope in committed
    ) == 1
    assert len(finish_calls) == 1
    assert core.close_calls == 1

    await pool.close()
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_frozen_shutdown_emergency_closes_without_fabricated_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journal unavailable 时 Shutdown 只做 root cancel 与 definite lease close。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_frozen"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    coordinator = pool._audit_sessions[session_id].coordinator  # noqa: SLF001
    before = [envelope async for envelope in real_core.load(session_id)]
    coordinator.freeze(OSError("journal unavailable"))
    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _size: "shutdown_frozen",
    )

    with pytest.raises(AuditSessionReleaseError) as caught:
        await engine.submit(Shutdown())

    result = caught.value.finish_result
    with pytest.raises(AuditSessionReleaseError) as retry_caught:
        await engine.submit(Shutdown())
    retry_result = retry_caught.value.finish_result
    assert retry_result == result
    assert retry_result is not result
    assert result.audit_complete is False
    assert result.lease_released is True
    assert result.terminal_record_ids == ()
    assert coordinator.session_root_cancel.is_cancelled
    snapshot = coordinator.snapshot()
    assert snapshot.audit_complete is False
    assert snapshot.lease_released is True
    assert snapshot.lifecycle.value == "closed"
    assert [envelope async for envelope in real_core.load(session_id)] == before
    assert core.close_calls == 1

    await pool.close()
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_caller_cancellation_does_not_truncate_owned_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raw caller cancel 延迟到 durable acceptance、terminal 与 close 后重抛。"""
    real_core, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_caller_cancel"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    coordinator = pool._audit_sessions[session_id].coordinator  # noqa: SLF001
    finish_calls = _count_finish_calls(coordinator)
    acceptance_entered, allow_acceptance = _gate_shutdown_acceptance(core)
    shutdown = await _start_fixed_shutdown(
        engine,
        monkeypatch,
        "shutdown_caller_cancel",
    )
    with anyio.fail_after(1):
        await acceptance_entered.wait()

    shutdown.cancel("caller cancelled")
    await anyio.lowlevel.checkpoint()
    assert not shutdown.done()
    allow_acceptance.set()

    try:
        await core.terminal_entered.wait()
        assert not shutdown.done()
        core.allow_terminal.set()
        with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
            await shutdown
    finally:
        allow_acceptance.set()
        core.allow_terminal.set()
        await asyncio.gather(shutdown, return_exceptions=True)

    committed = [envelope async for envelope in real_core.load(session_id)]
    assert [envelope.record_type for envelope in committed[3:]] == [
        "submission_accepted",
        "submission_applied",
        "thread_terminal",
        "session_ended",
    ]
    assert len(finish_calls) == 1
    assert core.close_calls == 1

    await pool.close()
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_legacy_shutdown_keeps_actor_queue_and_pool_release_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit=None 时 Shutdown 不接入 Journal 或 pool-owned submit hook。"""
    pool = EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=SimClient(turns=[]),
        store=JsonlMessageStore(tmp_path / "legacy-threads"),
        tool_registry=ToolRegistry(),
        compressors=[],
    )
    session_id = "ses_legacy_shutdown"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _size: "shutdown_legacy",
    )

    shutdown_id = await engine.submit(Shutdown())
    with anyio.fail_after(1):
        await pool._engine_tasks[session_id]  # noqa: SLF001

    assert shutdown_id == "sub_shutdown_legacy"
    assert engine._audit_state is None  # noqa: SLF001
    assert engine._audit_finish_owner is None  # noqa: SLF001
    assert pool._audit_sessions == {}  # noqa: SLF001

    await pool.release(session_id)
    await pool.close()


@pytest.mark.asyncio
async def test_audited_shutdown_event_uses_durable_public_submission_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pool-owned actor ShutdownMsg 归因到 durable accepted public id。"""
    _, core, pool, _ = _build_release_scenario(tmp_path)
    session_id = "ses_shutdown_event"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    core.allow_terminal.set()
    tokens = iter(("shutdown_public", "shutdown_internal"))
    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _size: next(tokens),
    )
    events: list[Any] = []

    async def collect_until_shutdown() -> None:
        """订阅 actor 的首个 ShutdownMsg。"""
        async for event in engine.subscribe_all():
            events.append(event)

    collector = asyncio.create_task(collect_until_shutdown())
    await anyio.lowlevel.checkpoint()
    shutdown_id = await engine.submit(Shutdown())
    with anyio.fail_after(1):
        await collector

    assert shutdown_id == "sub_shutdown_public"
    shutdown_events = [event for event in events if event.msg.kind == "shutdown"]
    assert [event.submission_id for event in shutdown_events] == [shutdown_id]
    assert core.close_calls == 1

    await pool.close()
    assert core.close_calls == 1
