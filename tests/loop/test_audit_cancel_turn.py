"""SessionAuditCoordinator 的 target cancellation registry 契约测试。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.conversation.journal import (
    SubmissionAcceptedV1,
    SubmissionAppliedV1,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.client import OneNetworkAttemptModelClient
from taifeng.llm.events import completed, text_delta
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditCoordinator,
    SessionAuditFrozenError,
    SessionFinishingError,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import CancelTurn, Submission, UserMessage
from tests.loop.test_audit_coordinator import _coordinator, _RecordingCore
from tests.loop.test_audit_submission_admission import _engine_with_audit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable
    from pathlib import Path

    from taifeng.conversation.journal import JournalAck, JournalRecord, SessionLease
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest


async def _capture_result(
    operation: Awaitable[Any],
    sink: list[Any],
) -> None:
    """等待 target cancel 收敛，并保存 frozen 结果。"""
    sink.append(await operation)


@pytest.mark.anyio
async def test_active_target_cancel_is_scoped_and_same_submission_replays() -> None:
    """active target 只取消自身 subtree；同 cancel id 重试复用原结果。"""
    root = CancellationToken(name="session:ses_1")
    coordinator = _coordinator(_RecordingCore(), root=root)
    target = coordinator.register_target("sub_target")
    target_child = target.child("tool-effect")
    peer = coordinator.register_target("sub_peer")
    results: list[Any] = []
    terminal_ids = ("turn_target:turn_cancelled:none:0",)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            _capture_result,
            coordinator.resolve_target_cancel(
                cancel_submission_id="sub_cancel_1",
                target_submission_id="sub_target",
            ),
            results,
        )
        with anyio.fail_after(1):
            await target.wait_cancelled()

        assert target.is_cancelled
        assert target_child.is_cancelled
        assert not peer.is_cancelled
        assert not root.is_cancelled

        assert coordinator.register_target_terminal(
            "sub_target",
            target,
            terminal_record_ids=terminal_ids,
        )

    assert len(results) == 1
    first = results[0]
    replay = await coordinator.resolve_target_cancel(
        cancel_submission_id="sub_cancel_1",
        target_submission_id="sub_target",
    )

    assert first.result_status == "cancelled"
    assert first.terminal_record_ids == terminal_ids
    assert replay == first
    assert replay is not first
    assert not peer.is_cancelled
    assert not root.is_cancelled


@pytest.mark.anyio
async def test_terminal_and_missing_targets_have_stable_results() -> None:
    """terminal 命中 already_terminal；missing 命中 not_found 且无伪造 ids。"""
    coordinator: SessionAuditCoordinator = _coordinator(_RecordingCore())
    target = coordinator.register_target("sub_terminal")
    terminal_ids = ("turn_terminal:turn_cancelled:none:0",)
    assert coordinator.register_target_terminal(
        "sub_terminal",
        target,
        terminal_record_ids=terminal_ids,
    )

    terminal = await coordinator.resolve_target_cancel(
        cancel_submission_id="sub_cancel_terminal",
        target_submission_id="sub_terminal",
    )
    terminal_replay = await coordinator.resolve_target_cancel(
        cancel_submission_id="sub_cancel_terminal",
        target_submission_id="sub_terminal",
    )
    missing = await coordinator.resolve_target_cancel(
        cancel_submission_id="sub_cancel_missing",
        target_submission_id="sub_missing",
    )
    missing_replay = await coordinator.resolve_target_cancel(
        cancel_submission_id="sub_cancel_missing",
        target_submission_id="sub_missing",
    )

    assert terminal.result_status == "already_terminal"
    assert terminal.terminal_record_ids == terminal_ids
    assert terminal_replay == terminal
    assert terminal_replay is not terminal
    assert missing.result_status == "not_found"
    assert missing.terminal_record_ids == ()
    assert missing_replay == missing
    assert missing_replay is not missing


@pytest.mark.anyio
async def test_freeze_wakes_pending_target_resolution_with_frozen_error() -> None:
    """等待 terminal 的 CancelTurn 遇 freeze 后不得永久悬挂或伪造结果。"""
    coordinator = _coordinator(_RecordingCore())
    target = coordinator.register_target("sub_target")

    async def resolve() -> None:
        """等待 active target 的最终裁定。"""
        await coordinator.resolve_target_cancel(
            cancel_submission_id="sub_cancel",
            target_submission_id="sub_target",
        )

    task = asyncio.create_task(resolve())
    with anyio.fail_after(1):
        await target.wait_cancelled()
    coordinator.freeze(OSError("journal unavailable"))

    with anyio.fail_after(1), pytest.raises(SessionAuditFrozenError):
        await task


class _ControlledSession:
    """按 user marker 阻塞并暴露真实 TurnRunner token。"""

    def __init__(
        self,
        client: _ControlledClient,
        cancel: CancellationToken,
    ) -> None:
        """保存共享控制器与本 turn token。"""
        self._client = client
        self._cancel = cancel

    async def __aenter__(self) -> _ControlledSession:
        """返回当前 session。"""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """无额外资源需要释放。"""

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """target 等待取消；peer 在取消完成后仍可继续真实 LLM effect。"""
        marker = str(request.messages[-1].content)
        if "cancel-me" in marker:
            self._client.target_token = self._cancel
            self._client.target_entered.set()
            await self._cancel.wait_cancelled()
            self._cancel.raise_if_cancelled()
            return
        self._client.peer_token = self._cancel
        self._client.peer_entered.set()
        await self._client.release_peer.wait()
        self._cancel.raise_if_cancelled()
        self._client.peer_effect_completed.set()
        yield text_delta("peer survived")
        yield completed(response_id=None, usage=TokenUsage(), end_turn=True)


class _ControlledClient(OneNetworkAttemptModelClient):
    """控制两个并发真实 TurnRunner 的 effect 时序。"""

    def __init__(self) -> None:
        """初始化 token 观测点与 peer effect barriers。"""
        self.target_entered = anyio.Event()
        self.peer_entered = anyio.Event()
        self.release_peer = anyio.Event()
        self.peer_effect_completed = anyio.Event()
        self.target_token: CancellationToken | None = None
        self.peer_token: CancellationToken | None = None

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> _ControlledSession:
        """返回绑定真实 turn token 的可控 session。"""
        del model
        return _ControlledSession(self, cancel)


class _ImmediateSession:
    """自然完成一次 LLM effect，并暴露收到的 turn token。"""

    def __init__(
        self,
        client: _ImmediateClient,
        cancel: CancellationToken,
    ) -> None:
        """保存 client 与本 turn token。"""
        self._client = client
        self._cancel = cancel

    async def __aenter__(self) -> _ImmediateSession:
        """返回当前 session。"""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """无额外资源需要释放。"""

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """立即产出自然完成结果。"""
        del request
        self._client.token = self._cancel
        yield text_delta("already complete")
        yield completed(response_id=None, usage=TokenUsage(), end_turn=True)


class _ImmediateClient(OneNetworkAttemptModelClient):
    """构造可观测自然完成 turn。"""

    def __init__(self) -> None:
        """初始化 token 观测槽。"""
        self.token: CancellationToken | None = None

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> _ImmediateSession:
        """返回自然完成 session。"""
        del model
        return _ImmediateSession(self, cancel)


class _AppliedPausingCore:
    """在 CancelTurn 单条 applied dispatch 前暂停的真实 Journal wrapper。"""

    def __init__(self, inner: JsonlSessionJournalCore) -> None:
        """保存真实 core 与 applied barrier。"""
        self.inner = inner
        self.applied_entered = anyio.Event()
        self.release_applied = anyio.Event()

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """仅暂停 CancelTurn 的单条 submission_applied。"""
        if len(records) == 1 and records[0].record_type == "submission_applied":
            self.applied_entered.set()
            await self.release_applied.wait()
        return await self.inner.append_batch(
            records,
            lease=lease,
            expected_seq=expected_seq,
        )

    async def load(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[Any]:
        """委托真实 core strict load。"""
        async for envelope in self.inner.load(session_id, after_seq=after_seq):
            yield envelope

    async def close_session(self, lease: SessionLease) -> None:
        """委托真实 per-Session close。"""
        await self.inner.close_session(lease)


async def _load_journal(
    core: JsonlSessionJournalCore,
) -> list[Any]:
    """读取测试 Session 的完整 committed Journal。"""
    return [
        envelope
        async for envelope in core.load("ses_audit_submission")
    ]


async def _wait_for_record(
    core: JsonlSessionJournalCore,
    *,
    record_type: str,
    submission_id: str,
) -> list[Any]:
    """等待指定 submission 的 durable record 后返回完整 Journal。"""
    with anyio.fail_after(2):
        while True:
            envelopes = await _load_journal(core)
            if any(
                envelope.record_type == record_type
                and envelope.submission_id == submission_id
                for envelope in envelopes
            ):
                return envelopes
            await anyio.lowlevel.checkpoint()


async def _stop_actor(actor: asyncio.Task[None]) -> None:
    """取消测试 actor 并检索其终态，避免泄漏 operation。"""
    if actor.done():
        return
    actor.cancel()
    with suppress(asyncio.CancelledError):
        await actor


@pytest.mark.anyio
async def test_engine_cancel_turn_is_durable_targeted_and_peer_can_continue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """accepted→turn_cancelled→applied，且 peer 仍可完成后续 effect。"""
    client = _ControlledClient()
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    actor_root = CancellationToken(name="test-actor-root")
    actor = asyncio.create_task(engine.run(actor_root))
    try:
        target_id = await engine.submit(UserMessage(text="cancel-me"))
        await engine.submit(UserMessage(text="keep-going"))
        with anyio.fail_after(2):
            await client.target_entered.wait()
            await client.peer_entered.wait()
        assert client.target_token is not None
        assert client.peer_token is not None
        target_child = client.target_token.child("child-effect")

        cancel_id = await engine.submit(CancelTurn(submission_id=target_id))
        envelopes = await _wait_for_record(
            core,
            record_type="submission_applied",
            submission_id=cancel_id,
        )

        assert client.target_token.is_cancelled
        assert target_child.is_cancelled
        assert not client.peer_token.is_cancelled
        assert not actor_root.is_cancelled
        assert not coordinator.session_root_cancel.is_cancelled
        assert coordinator.health is AuditHealth.HEALTHY
        await coordinator.ensure_effect_allowed()

        client.release_peer.set()
        with anyio.fail_after(2):
            await client.peer_effect_completed.wait()

        cancellation_records = [
            envelope
            for envelope in envelopes
            if (
                envelope.submission_id == cancel_id
                and envelope.record_type
                in ("submission_accepted", "submission_applied")
            )
            or (
                envelope.submission_id == target_id
                and envelope.record_type == "turn_cancelled"
            )
        ]
        assert [record.record_type for record in cancellation_records] == [
            "submission_accepted",
            "turn_cancelled",
            "submission_applied",
        ]
        accepted = SubmissionAcceptedV1.model_validate(
            cancellation_records[0].payload
        )
        applied = SubmissionAppliedV1.model_validate(
            cancellation_records[2].payload
        )
        assert accepted.op_kind == "cancel_turn"
        assert accepted.target_submission_id == target_id
        assert applied.result_status == "cancelled"
        assert applied.terminal_record_ids == (
            cancellation_records[1].record_id,
        )
    finally:
        await _stop_actor(actor)


def _applied_result(
    envelopes: list[Any],
    submission_id: str,
) -> SubmissionAppliedV1:
    """从完整 Journal 中读取指定 CancelTurn 的 applied payload。"""
    payload = next(
        envelope.payload
        for envelope in envelopes
        if envelope.submission_id == submission_id
        and envelope.record_type == "submission_applied"
    )
    return SubmissionAppliedV1.model_validate(payload)


async def _assert_missing_cancel_result(engine: Any, core: Any) -> None:
    """验证 missing target 的稳定无终态结果。"""
    missing_id = await engine.submit(CancelTurn(submission_id="sub_missing"))
    records = await _wait_for_record(
        core,
        record_type="submission_applied",
        submission_id=missing_id,
    )
    missing = _applied_result(records, missing_id)
    assert missing.result_status == "not_found"
    assert missing.terminal_record_ids == ()


async def _submit_and_replay_cancel(
    engine: Any,
    core: Any,
    *,
    target_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> SubmissionAppliedV1:
    """验证相同 CancelTurn id 重放不重复 durable facts。"""
    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _: "stablecancel",
    )
    first_id = await engine.submit(CancelTurn(submission_id=target_id))
    first_records = await _wait_for_record(
        core,
        record_type="submission_applied",
        submission_id=first_id,
    )
    first_applied = _applied_result(first_records, first_id)

    second_id = await engine.submit(CancelTurn(submission_id=target_id))
    assert first_id == second_id == "sub_stablecancel"
    assert await _load_journal(core) == first_records
    assert first_applied.result_status == "cancelled"
    assert len(first_applied.terminal_record_ids) == 1
    return first_applied


async def _assert_already_terminal_result(
    engine: Any,
    core: Any,
    *,
    target_id: str,
    first_applied: SubmissionAppliedV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证新 CancelTurn 对 durable terminal 返回 already_terminal。"""
    monkeypatch.setattr(
        "taifeng.loop.submission.secrets.token_hex",
        lambda _: "terminalcancel",
    )
    terminal_id = await engine.submit(CancelTurn(submission_id=target_id))
    records = await _wait_for_record(
        core,
        record_type="submission_applied",
        submission_id=terminal_id,
    )
    terminal = _applied_result(records, terminal_id)
    assert terminal.result_status == "already_terminal"
    assert terminal.terminal_record_ids == first_applied.terminal_record_ids
    assert sum(
        envelope.record_type == "turn_cancelled"
        and envelope.submission_id == target_id
        for envelope in records
    ) == 1


@pytest.mark.anyio
async def test_engine_cancel_results_are_durable_and_retry_idempotent(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """not_found/already_terminal 稳定；同 cancel id 重试不重复事实。"""
    client = _ControlledClient()
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    actor = asyncio.create_task(
        engine.run(CancellationToken(name="test-actor-root"))
    )
    try:
        await _assert_missing_cancel_result(engine, core)
        target_id = await engine.submit(UserMessage(text="cancel-me"))
        with anyio.fail_after(2):
            await client.target_entered.wait()
        first_applied = await _submit_and_replay_cancel(
            engine,
            core,
            target_id=target_id,
            monkeypatch=monkeypatch,
        )
        await _assert_already_terminal_result(
            engine,
            core,
            target_id=target_id,
            first_applied=first_applied,
            monkeypatch=monkeypatch,
        )
        assert coordinator.health is AuditHealth.HEALTHY
    finally:
        await _stop_actor(actor)


@pytest.mark.anyio
async def test_frozen_cancel_turn_degrades_without_durable_facts_or_enqueue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """Journal frozen 时仅做安全取消，不伪造 accepted/applied。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    target = coordinator.register_target("sub_target")
    before = await _load_journal(core)
    coordinator.freeze(OSError("journal unavailable"))

    cancel_id = await engine.submit(CancelTurn(submission_id="sub_target"))
    after = await _load_journal(core)

    assert cancel_id
    assert target.is_cancelled
    assert after == before
    assert engine._submissions.empty()  # noqa: SLF001
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_finishing_rejects_healthy_cancel_before_accept_or_enqueue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """FINISHING healthy Session 不接收新的 CancelTurn durable fact。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    await coordinator.close_intake()
    before = await _load_journal(core)

    with pytest.raises(SessionFinishingError):
        await engine.submit(CancelTurn(submission_id="sub_target"))

    assert await _load_journal(core) == before
    assert engine._submissions.empty()  # noqa: SLF001


@pytest.mark.anyio
async def test_cancel_caller_raw_cancellation_waits_for_applied_then_reraises(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """caller raw cancel 不截断 terminal/applied/AcceptedWork 收敛。"""
    client = _ControlledClient()
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    pausing_core = _AppliedPausingCore(real_core)
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=pausing_core,  # type: ignore[arg-type]
        model_client=client,
    )
    actor = asyncio.create_task(
        engine.run(CancellationToken(name="test-actor-root"))
    )
    try:
        target_id = await engine.submit(UserMessage(text="cancel-me"))
        with anyio.fail_after(2):
            await client.target_entered.wait()
        monkeypatch.setattr(
            "taifeng.loop.submission.secrets.token_hex",
            lambda _: "rawcancel",
        )

        cancel_task = asyncio.create_task(
            engine.submit(CancelTurn(submission_id=target_id))
        )
        with anyio.fail_after(2):
            await pausing_core.applied_entered.wait()
        cancel_task.cancel("caller raw cancellation")
        pausing_core.release_applied.set()

        with pytest.raises(asyncio.CancelledError, match="caller raw cancellation"):
            await cancel_task
        records = await _wait_for_record(
            core,
            record_type="submission_applied",
            submission_id="sub_rawcancel",
        )

        assert [
            envelope.record_type
            for envelope in records
            if (
                envelope.submission_id == "sub_rawcancel"
                and envelope.record_type
                in ("submission_accepted", "submission_applied")
            )
            or (
                envelope.submission_id == target_id
                and envelope.record_type == "turn_cancelled"
            )
        ] == [
            "submission_accepted",
            "turn_cancelled",
            "submission_applied",
        ]
        assert coordinator.snapshot().accepted_work_ids == ()
        assert coordinator.health is AuditHealth.HEALTHY
    finally:
        pausing_core.release_applied.set()
        await _stop_actor(actor)


@pytest.mark.anyio
async def test_late_cancel_after_natural_outcome_does_not_fabricate_cancelled(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """无正常 durable terminal 的当前切片把自然完成竞争稳定裁为 not_found。"""
    client = _ImmediateClient()
    engine, _, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    writeback_entered = anyio.Event()
    release_writeback = anyio.Event()
    original_writeback = engine._writeback_turn_runner  # noqa: SLF001

    async def blocking_writeback(runner: Any) -> None:
        """在自然 outcome 已形成后暂停 Engine writeback。"""
        writeback_entered.set()
        await release_writeback.wait()
        await original_writeback(runner)

    engine._writeback_turn_runner = blocking_writeback  # type: ignore[method-assign]  # noqa: SLF001
    actor = asyncio.create_task(
        engine.run(CancellationToken(name="test-actor-root"))
    )
    try:
        target_id = await engine.submit(UserMessage(text="complete-first"))
        with anyio.fail_after(2):
            await writeback_entered.wait()
        assert client.token is not None

        cancel_task = asyncio.create_task(
            engine.submit(CancelTurn(submission_id=target_id))
        )
        with anyio.fail_after(2):
            await client.token.wait_cancelled()
        release_writeback.set()
        cancel_id = await cancel_task
        records = await _wait_for_record(
            core,
            record_type="submission_applied",
            submission_id=cancel_id,
        )
        applied = SubmissionAppliedV1.model_validate(
            next(
                envelope.payload
                for envelope in records
                if envelope.submission_id == cancel_id
                and envelope.record_type == "submission_applied"
            )
        )

        assert applied.result_status == "not_found"
        assert applied.terminal_record_ids == ()
        assert not any(
            envelope.record_type == "turn_cancelled"
            and envelope.submission_id == target_id
            for envelope in records
        )
    finally:
        release_writeback.set()
        await _stop_actor(actor)


@pytest.mark.anyio
async def test_legacy_cancel_turn_keeps_raw_submission_queue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """audit=None 时 CancelTurn 保持既有 actor queue 语义。"""
    engine, _, _ = await _engine_with_audit(tmp_path, skills_dir)
    engine._audit_state = None  # type: ignore[attr-defined]  # noqa: SLF001

    cancel_id = await engine.submit(CancelTurn(submission_id="sub_target"))
    queued = engine._submissions.get_nowait()  # noqa: SLF001

    assert isinstance(queued, Submission)
    assert queued.id == cancel_id
    assert queued.op == CancelTurn(submission_id="sub_target")
