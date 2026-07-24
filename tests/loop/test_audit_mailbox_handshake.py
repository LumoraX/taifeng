"""Audited mailbox claim→child-start handshake 的线性化测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.loop.audit import AuditHealth
from taifeng.loop.audit_admission import AcceptedUserMessage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import UserMessage
from tests.loop.test_audit_submission_admission import _engine_with_audit

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_finalizer_wins_before_late_child_handshake(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """finalizer 先接管 claimed token 后，迟启动 child 不得应用或双退。"""
    from taifeng.llm.providers.sim import SimClient

    client = SimClient(turns=[])
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
        finish_timeout=0.05,
    )
    await engine.submit(UserMessage(text="finalizer wins handshake"))
    queued = engine._submissions._queue[0]  # type: ignore[attr-defined]  # noqa: SLF001
    assert isinstance(queued, AcceptedUserMessage)
    work = queued.accepted_work
    child_start = anyio.Event()
    child_started = False
    child: asyncio.Task[None] | None = None
    original_start = engine._start_operation  # noqa: SLF001

    def start_then_cancel_actor(
        coroutine: Any,
        *,
        name: str,
    ) -> asyncio.Task[None]:
        """登记 operation 后让 actor 先进入 mailbox finalizer。"""
        nonlocal child, child_started

        async def delayed_child() -> None:
            """只在 mailbox close 已线性化后运行原 child coroutine。"""
            nonlocal child_started
            try:
                await child_start.wait()
                child_started = True
                await coroutine
            finally:
                if not child_started:
                    coroutine.close()

        child = original_start(delayed_child(), name=name)
        actor = asyncio.current_task()
        assert actor is not None
        actor.cancel()
        return child

    original_close = engine._audited_mailbox.close  # noqa: SLF001

    async def close_then_start_child() -> tuple[AcceptedUserMessage, ...]:
        """先让 finalizer 赢锁，再释放迟到的 child handshake。"""
        tokens = await original_close()
        child_start.set()
        await anyio.lowlevel.checkpoint()
        return tokens

    engine._start_operation = start_then_cancel_actor  # type: ignore[method-assign]  # noqa: SLF001
    engine._audited_mailbox.close = close_then_start_child  # type: ignore[method-assign]  # noqa: SLF001
    actor = asyncio.create_task(engine.run(CancellationToken(name="test-root")))
    with pytest.raises(asyncio.CancelledError):
        await actor

    assert child is not None
    assert child_started is True
    assert child.done()
    result = await coordinator.finish(
        thread_terminals=(),
        reason="finalizer_handshake_won",
    )
    assert result.failure is not None
    assert result.failure.code == "accepted_work_handoff_failed"
    snapshot = coordinator.snapshot()
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.accepted_work_ids == ()
    assert work.is_completed
    await work.complete()
    assert coordinator.snapshot().accepted_work_ids == ()
    assert engine._history == []  # noqa: SLF001
    assert client.ledger.requests() == []
