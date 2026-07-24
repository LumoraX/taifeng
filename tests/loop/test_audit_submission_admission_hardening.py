"""Audited UserMessage actor 的 projector 故障分类与 fail-closed 测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Never

import anyio
import pytest

from taifeng.conversation.journal import ConversationItemV1
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.materialization import ProjectionLifecycleError
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditFrozenError,
)
from taifeng.loop.audit_admission import AcceptedUserMessage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import Shutdown, UserMessage
from tests.loop.test_audit_submission_admission import (
    _BlockingClient,
    _engine_with_audit,
    _LoadRaisingJournalCore,
    _PausingJournalCore,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.conversation.journal.materialization import ProjectionFileIdentity
    from taifeng.conversation.models import ResponseItem


class _ScopeFailingMessageStore(JsonlMessageStore):
    """在真实 MessageStore 投影 scope 准入时注入一次可恢复 IO 失败。"""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_projection_scope_once = True

    @asynccontextmanager
    async def projection_scope(self, thread_id: str) -> AsyncIterator[None]:
        """首个 scope 抛真实 handle lifecycle 错误，后续调用恢复真实行为。"""
        if self.fail_projection_scope_once:
            self.fail_projection_scope_once = False
            raise ProjectionLifecycleError("projection store handle is closed")
        async with super().projection_scope(thread_id):
            yield


class _IdentityMismatchingMessageStore(JsonlMessageStore):
    """返回与 audited transcript 不一致的 Journal Session identity。"""

    async def expected_projection_session_id(self, thread_id: str) -> str:
        """先读取真实绑定，再注入稳定的 identity mismatch。"""
        expected = await super().expected_projection_session_id(thread_id)
        return f"{expected}_mismatch"


class _WriteFailingMessageStore(JsonlMessageStore):
    """在真实 projection append 边界注入一次可恢复 IO 失败。"""

    async def append_projection_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """写入前失败，保留原始 transcript target 不变。"""
        del thread_id, items, expected_identity
        raise OSError("injected projection write failure")


class _UnclassifiedInvariantMessageStore(JsonlMessageStore):
    """让 projector 泄漏一个未分类、非取消、非 fatal 的不变量异常。"""

    async def expected_projection_session_id(self, thread_id: str) -> Never:
        """模拟第三方 store 违反 projection protocol。"""
        raise RuntimeError(f"injected unclassified invariant: {thread_id}")


class _BoundaryRaisingMessageStore(JsonlMessageStore):
    """从真实 projector store 边界抛指定取消或 fatal 异常。"""

    def __init__(self, root: Path, error: BaseException) -> None:
        super().__init__(root)
        self.error = error

    async def expected_projection_session_id(self, thread_id: str) -> Never:
        """验证 Engine 不会把取消/fatal 误分类为普通不变量。"""
        del thread_id
        raise self.error


class _BlockingProjectionMessageStore(JsonlMessageStore):
    """在 projection append 中阻塞并记录 child 收到的原始取消。"""

    def __init__(self, root: Path) -> None:
        """初始化写入 barrier 与取消观测点。"""
        super().__init__(root)
        self.entered = anyio.Event()
        self.cancelled: asyncio.CancelledError | None = None

    async def append_projection_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """在任何健康或 stale result 形成前等待 raw child cancellation。"""
        del thread_id, items, expected_identity
        self.entered.set()
        try:
            await anyio.Event().wait()
        except asyncio.CancelledError as error:
            self.cancelled = error
            raise


def _audited_turn_operation(
    engine: object,
    submission_id: str,
) -> asyncio.Task[None]:
    """从 Engine-owned operation 中定位指定 audited turn child。"""
    tasks = engine._operation_tasks  # type: ignore[attr-defined]  # noqa: SLF001
    return next(
        task
        for task in tasks
        if task.get_name().endswith(f"turn:{submission_id}")
    )


async def _stop_actor(actor: asyncio.Task[None]) -> None:
    """失败断言清理仍在运行的 actor，避免泄漏 operation。"""
    if actor.done():
        return
    actor.cancel()
    with suppress(asyncio.CancelledError):
        await actor


@pytest.mark.anyio
async def test_freeze_during_definite_accept_retires_hidden_work(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """definite ack 后若已冻结，未交付的 AcceptedWork 必须精确退休。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    pausing_core = _PausingJournalCore(real_core)
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=pausing_core,
        finish_timeout=0.01,
    )
    submission_error: BaseException | None = None

    async def submit() -> None:
        nonlocal submission_error
        try:
            await engine.submit(UserMessage(text="definite before freeze"))
        except BaseException as error:
            submission_error = error

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(submit)
        await pausing_core.append_entered.wait()
        coordinator.freeze(OSError("external freeze"))
        pausing_core.release_append.set()

    committed = [
        envelope async for envelope in real_core.load("ses_audit_submission")
    ]
    assert [item.record_type for item in committed[3:]] == [
        "submission_accepted",
        "conversation_item",
        "submission_applied",
    ]
    assert isinstance(submission_error, SessionAuditFrozenError)
    assert coordinator.snapshot().accepted_work_ids == ()

    result = await coordinator.finish(thread_terminals=(), reason="frozen")

    assert result.failure is not None
    assert result.failure.class_name == "OSError"
    assert coordinator.snapshot().accepted_work_ids == ()


@pytest.mark.anyio
async def test_actor_rejects_coordinated_payload_tamper_with_stale_hashes(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """三 envelope 业务字段即使协调一致，旧 hash receipt 也不得应用到 hot history。"""
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=_BlockingClient(),
    )
    await engine.submit(UserMessage(text="durable original"))
    token = engine._submissions.get_nowait()  # noqa: SLF001
    assert isinstance(token, AcceptedUserMessage)
    envelopes = list(token.envelopes)
    accepted_payload = dict(envelopes[0].payload)
    accepted_payload["text"] = "forged but coordinated"
    envelopes[0] = envelopes[0].model_copy(update={"payload": accepted_payload})
    conversation_payload = dict(envelopes[1].payload)
    item_payload = dict(conversation_payload["payload"])
    item_payload["text"] = "forged but coordinated"
    conversation_payload["payload"] = item_payload
    envelopes[1] = envelopes[1].model_copy(update={"payload": conversation_payload})
    object.__setattr__(token, "envelopes", tuple(envelopes))
    engine._submissions.put_nowait(token)  # noqa: SLF001
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    try:
        with anyio.fail_after(1):
            while not token.accepted_work.is_completed and not engine._history:  # noqa: SLF001
                await anyio.lowlevel.checkpoint()
    finally:
        cancel.cancel()
        with pytest.raises(SessionAuditFrozenError):
            await actor

    assert engine._history == []  # noqa: SLF001
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.effect_gate_open is False


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
async def test_strict_load_fatal_propagates_after_retiring_work(
    tmp_path: Path,
    skills_dir: Path,
    error_type: type[BaseException],
) -> None:
    """strict load fatal 冻结首因并原样传播，不能被 admission 改写。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    core = _LoadRaisingJournalCore(real_core, error_type)
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=core,
    )

    with pytest.raises(error_type):
        await engine.submit(UserMessage(text="fatal strict load"))

    snapshot = coordinator.snapshot()
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.first_failure is not None
    assert snapshot.first_failure.class_name == error_type.__name__
    assert snapshot.accepted_work_ids == ()


@pytest.mark.anyio
async def test_strict_load_cancellation_propagates_after_retiring_work(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """strict load 取消原样传播，冻结恢复首因且不遗留 ownership。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    core = _LoadRaisingJournalCore(real_core, asyncio.CancelledError)
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=core,
    )

    with pytest.raises(asyncio.CancelledError):
        await engine.submit(UserMessage(text="cancel strict load"))

    snapshot = coordinator.snapshot()
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.first_failure is not None
    assert snapshot.first_failure.class_name == "CancelledError"
    assert snapshot.accepted_work_ids == ()


@pytest.mark.anyio
async def test_cancelled_projection_checkpoint_stops_before_next_token(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """application cancel 必须阻止 actor dequeue 后续 accepted token。"""
    store = _BlockingProjectionMessageStore(tmp_path / "threads")
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=_BlockingClient(),
        store_override=store,
    )
    first_id = await engine.submit(UserMessage(text="first projection blocks"))
    await engine.submit(UserMessage(text="second must remain queued"))
    first, second = tuple(engine._submissions._queue)  # type: ignore[attr-defined]  # noqa: SLF001
    actor = asyncio.create_task(engine.run(CancellationToken(name="test-root")))
    try:
        await store.entered.wait()
        _audited_turn_operation(engine, first_id).cancel("raw projector child cancel")
        with anyio.fail_after(1):
            while not actor.done() and engine._submissions.qsize() == 1:  # noqa: SLF001
                await anyio.lowlevel.checkpoint()
        assert actor.done()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await actor
        assert cancelled.value is store.cancelled
        assert tuple(engine._submissions._queue) == (second,)  # type: ignore[attr-defined]  # noqa: SLF001
        assert first.accepted_work.is_completed
        assert second.accepted_work.is_completed
        assert coordinator.snapshot().accepted_work_ids == ()
    finally:
        await _stop_actor(actor)


@pytest.mark.anyio
async def test_cancelled_projection_checkpoint_stops_before_shutdown(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """application cancel 必须由 actor 原样传播，不能继续消费 Shutdown。"""
    store = _BlockingProjectionMessageStore(tmp_path / "threads")
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=_BlockingClient(),
        store_override=store,
    )
    first_id = await engine.submit(UserMessage(text="projection before shutdown"))
    first = engine._submissions._queue[0]  # type: ignore[attr-defined]  # noqa: SLF001
    await engine.shutdown()
    actor = asyncio.create_task(engine.run(CancellationToken(name="test-root")))
    try:
        await store.entered.wait()
        _audited_turn_operation(engine, first_id).cancel("raw projector child cancel")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            with anyio.fail_after(1):
                await actor
        assert cancelled.value is store.cancelled
        queued = tuple(engine._submissions._queue)  # type: ignore[attr-defined]  # noqa: SLF001
        assert len(queued) == 1 and isinstance(queued[0].op, Shutdown)
        assert first.accepted_work.is_completed
        assert coordinator.snapshot().accepted_work_ids == ()
        committed = [
            envelope async for envelope in core.load("ses_audit_submission")
        ]
        assert "session_ended" not in [item.record_type for item in committed]
    finally:
        await _stop_actor(actor)


@pytest.mark.anyio
@pytest.mark.parametrize("failure_point", ["scope", "write"])
async def test_projection_failure_marks_stale_without_freezing_or_losing_hot_history(
    tmp_path: Path,
    skills_dir: Path,
    failure_point: str,
) -> None:
    """真实 target IO 失败只使投影 stale，Journal/hot history 仍是权威事实。"""
    client = _BlockingClient()
    store = (
        _ScopeFailingMessageStore(tmp_path / "threads")
        if failure_point == "scope"
        else _WriteFailingMessageStore(tmp_path / "threads")
    )
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
        store_override=store,
    )
    await engine.submit(UserMessage(text="hot history remains"))
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    try:
        with anyio.fail_after(1):
            while True:
                projection = coordinator.projection_snapshot(engine.thread_id)
                if projection is not None:
                    break
                await anyio.lowlevel.checkpoint()
        assert projection.stale
        assert coordinator.health is AuditHealth.HEALTHY
        assert coordinator.effect_gate_open
        assert engine._history[0].payload["text"] == "hot history remains"  # noqa: SLF001
        committed = [item async for item in core.load("ses_audit_submission")]
        durable_item = ConversationItemV1.model_validate(committed[4].payload)
        assert durable_item.payload["text"] == "hot history remains"
    finally:
        cancel.cancel()
        await actor


@pytest.mark.anyio
async def test_projection_identity_invariant_freezes_before_effect_dispatch(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """Journal/transcript identity 不一致必须 fail-closed，不能降级为 stale。"""
    store = _IdentityMismatchingMessageStore(tmp_path / "threads")
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=_BlockingClient(),
        store_override=store,
    )
    await engine.submit(UserMessage(text="identity invariant"))
    token = engine._submissions.get_nowait()  # noqa: SLF001
    assert isinstance(token, AcceptedUserMessage)
    engine._submissions.put_nowait(token)  # noqa: SLF001
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    try:
        with anyio.fail_after(1):
            while not token.accepted_work.is_completed:
                await anyio.lowlevel.checkpoint()
    finally:
        cancel.cancel()
        with pytest.raises(SessionAuditFrozenError):
            await actor

    assert [item.payload["text"] for item in engine._history] == [  # noqa: SLF001
        "identity invariant"
    ]
    assert coordinator.projection_snapshot(engine.thread_id) is None
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.effect_gate_open is False
    with pytest.raises(SessionAuditFrozenError):
        await coordinator.ensure_effect_allowed()


@pytest.mark.anyio
async def test_unclassified_projector_invariant_freezes_coordinator(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """普通未分类异常不得被 operation callback 吞掉后健康 complete。"""
    store = _UnclassifiedInvariantMessageStore(tmp_path / "threads")
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=_BlockingClient(),
        store_override=store,
    )
    await engine.submit(UserMessage(text="unclassified invariant"))
    token = engine._submissions.get_nowait()  # noqa: SLF001
    assert isinstance(token, AcceptedUserMessage)
    engine._submissions.put_nowait(token)  # noqa: SLF001
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    try:
        with anyio.fail_after(1):
            while not token.accepted_work.is_completed:
                await anyio.lowlevel.checkpoint()
    finally:
        cancel.cancel()
        with pytest.raises(SessionAuditFrozenError):
            await actor

    assert coordinator.projection_snapshot(engine.thread_id) is None
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.effect_gate_open is False
    with pytest.raises(SessionAuditFrozenError):
        await coordinator.ensure_effect_allowed()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error_type",
    [asyncio.CancelledError, KeyboardInterrupt, SystemExit],
)
async def test_projector_cancel_and_fatal_errors_propagate_without_freeze(
    tmp_path: Path,
    skills_dir: Path,
    error_type: type[BaseException],
) -> None:
    """取消与进程级 fatal 必须原样传播，accepted work 仍可靠退休。"""
    store = _BoundaryRaisingMessageStore(
        tmp_path / "threads",
        error_type("injected boundary error"),
    )
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=_BlockingClient(),
        store_override=store,
    )
    await engine.submit(UserMessage(text="boundary error"))
    token = engine._submissions.get_nowait()  # noqa: SLF001
    assert isinstance(token, AcceptedUserMessage)
    assert await engine._audited_mailbox.claim(token)  # noqa: SLF001

    with pytest.raises(error_type):
        await engine._run_claimed_audited_turn(  # noqa: SLF001
            token,
            CancellationToken(name="test-root"),
        )

    assert token.accepted_work.is_completed
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.effect_gate_open
    assert coordinator.projection_snapshot(engine.thread_id) is None
