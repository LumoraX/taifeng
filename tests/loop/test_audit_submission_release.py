"""Audited accepted queue 经真实 EnginePool.release 收敛的集成测试。"""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.loop.audit import SessionFinishingError
from taifeng.loop.audit_config import AttemptObservableModelClient, AuditConfig
from taifeng.loop.pool import EnginePool
from taifeng.loop.submission import UserMessage
from taifeng.tool.registry import ToolRegistry
from tests.loop.test_audit_engine_bootstrap import _Registry
from tests.loop.test_audit_submission_admission import _BlockingClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.conversation.journal import (
        JournalAck,
        JournalEnvelope,
        JournalRecord,
        SessionCreateResult,
        SessionDescriptor,
        SessionLease,
    )
    from taifeng.conversation.journal.materialization import ProjectionFileIdentity
    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.client import ModelClientSession
    from taifeng.loop.cancellation import CancellationToken


class _BlockingObservedClient(AttemptObservableModelClient):
    """满足 audit nominal gate、但绝不访问网络的阻塞 client。"""

    def __init__(self) -> None:
        """复用可控的无网络阻塞 session。"""
        self.inner = _BlockingClient()

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """委托普通阻塞 session。"""
        return self.inner.session(cancel=cancel, model=model)

    def session_with_attempt_observer(
        self,
        *,
        cancel: CancellationToken,
        attempt_observer: object,
        model: str | None = None,
    ) -> ModelClientSession:
        """Task 7 前 observer 只满足静态注入边界。"""
        del attempt_observer
        return self.inner.session(cancel=cancel, model=model)


class _ObservedJournalCore:
    """包装真实 JSONL core，暂停 terminal append 并计数 close。"""

    def __init__(self, inner: JsonlSessionJournalCore) -> None:
        """初始化 terminal 观测点。"""
        self.inner = inner
        self.terminal_entered = anyio.Event()
        self.allow_terminal = anyio.Event()
        self.close_calls = 0

    async def create_session(
        self,
        descriptor: SessionDescriptor,
    ) -> SessionCreateResult:
        """委托真实初始化。"""
        return await self.inner.create_session(descriptor)

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """terminal batch 在真实写入前暴露 FINISHING 检查窗口。"""
        if any(record.record_type == "session_ended" for record in records):
            self.terminal_entered.set()
            await self.allow_terminal.wait()
        return await self.inner.append_batch(
            records,
            lease=lease,
            expected_seq=expected_seq,
        )

    def load(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """委托 strict load。"""
        return self.inner.load(session_id, after_seq=after_seq)

    async def close_session(self, lease: SessionLease) -> None:
        """观测唯一 per-Session close。"""
        self.close_calls += 1
        await self.inner.close_session(lease)


async def _wait_until(predicate: object) -> None:
    """在测试 deadline 内等待同步谓词成立。"""
    assert callable(predicate)
    with anyio.fail_after(1):
        while not predicate():
            await anyio.lowlevel.checkpoint()


@pytest.mark.asyncio
async def test_real_pool_release_waits_for_accepted_application_and_orders_journal(
    tmp_path: Path,
) -> None:
    """release 必须等 accepted projection 收敛后才 terminal/close。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    core = _ObservedJournalCore(real_core)
    store = JsonlMessageStore(tmp_path / "threads")
    second_projection_entered = anyio.Event()
    allow_second_projection = anyio.Event()
    projection_calls = 0
    original_projection = store.append_projection_batch

    async def pause_second_projection(
        _store: JsonlMessageStore,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """在第二个 accepted item 的物化写入前建立确定竞态窗口。"""
        nonlocal projection_calls
        projection_calls += 1
        if projection_calls == 2:
            second_projection_entered.set()
            await allow_second_projection.wait()
        await original_projection(thread_id, items, expected_identity)

    store.append_projection_batch = MethodType(  # type: ignore[method-assign]
        pause_second_projection,
        store,
    )
    pool = EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=_BlockingObservedClient(),
        store=store,
        tool_registry=ToolRegistry(),
        compressors=[],
        submission_queue_size=1,
        audit=AuditConfig(
            journal_core=core,
            writer_id="writer-release",
            max_attachment_bytes=1024,
            max_total_attachment_bytes=4096,
        ),
    )
    session_id = "ses_release_queue"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    state = pool._audit_sessions[session_id]  # noqa: SLF001
    first_id = await engine.submit(UserMessage(text="first accepted"))
    await _wait_until(lambda: projection_calls == 1)
    second_id = await engine.submit(UserMessage(text="second accepted"))
    await second_projection_entered.wait()

    release = asyncio.create_task(pool.release(session_id))
    await anyio.sleep(0.05)
    assert not release.done()
    assert not core.terminal_entered.is_set()

    allow_second_projection.set()
    await core.terminal_entered.wait()
    assert state.coordinator.snapshot().accepted_work_ids == ()
    assert len(engine._history) == 2  # noqa: SLF001
    assert (
        state.projector.state(engine.thread_id).projected_seq
        == state.coordinator.expected_seq - 1
    )
    before_late = [item async for item in real_core.load(session_id)]
    with pytest.raises(SessionFinishingError):
        await engine.submit(UserMessage(text="late finishing"))
    assert [item async for item in real_core.load(session_id)] == before_late

    core.allow_terminal.set()
    await release
    committed = [item async for item in real_core.load(session_id)]
    assert [item.record_type for item in committed] == [
        "session_started",
        "thread_created",
        "thread_bound",
        "submission_accepted",
        "conversation_item",
        "submission_applied",
        "submission_accepted",
        "conversation_item",
        "submission_applied",
        "thread_terminal",
        "session_ended",
    ]
    for offset, submission_id in ((3, first_id), (6, second_id)):
        batch = committed[offset : offset + 3]
        assert [item.submission_id for item in batch] == [submission_id] * 3
        assert batch[0].record_id == (
            f"{submission_id}:submission_accepted:none:0"
        )
        assert batch[1].record_id == f"{submission_id}:conversation_item:none:0"
        assert batch[2].record_id == (
            f"{submission_id}:submission_applied:none:0"
        )
    assert committed[-2].thread_id == engine.thread_id
    assert committed[-1].record_type == "session_ended"
    assert state.coordinator.snapshot().accepted_work_ids == ()
    assert core.close_calls == 1

    await pool.close()
    assert core.close_calls == 1
