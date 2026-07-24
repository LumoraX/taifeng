"""Audited UserMessage actor 的 projector 故障分类与 fail-closed 测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Never

import anyio
import pytest

from taifeng.conversation.journal import ConversationItemV1
from taifeng.conversation.journal.materialization import ProjectionLifecycleError
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditFrozenError,
)
from taifeng.loop.audit_admission import AcceptedUserMessage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import UserMessage
from tests.loop.test_audit_submission_admission import (
    _BlockingClient,
    _engine_with_audit,
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

    with pytest.raises(error_type):
        await engine._run_audited_turn_for(  # noqa: SLF001
            token,
            CancellationToken(name="test-root"),
        )

    assert token.accepted_work.is_completed
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.effect_gate_open
    assert coordinator.projection_snapshot(engine.thread_id) is None
