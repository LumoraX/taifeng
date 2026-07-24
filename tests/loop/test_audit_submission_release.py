"""Audited accepted queue 经真实 EnginePool.release 收敛的集成测试。"""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit import SessionFinishingError
from taifeng.loop.audit_config import AuditConfig
from taifeng.loop.pool import EnginePool
from taifeng.loop.submission import UserMessage
from taifeng.tool.registry import ToolRegistry
from tests.loop.test_audit_engine_bootstrap import _Registry

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
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.engine import AgentEngine


def _blocking_observed_client() -> AttemptObservableClientAdapter:
    """用 exact reviewed Sim + signal 建立无网络阻塞 client。"""
    return AttemptObservableClientAdapter(
        SimClient(
            turns=[SimTurn(text="unused", await_signal="release-blocked")],
        ),
        provider="sim",
        default_model="sim-model",
    )


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


class _ProjectionGate:
    """在第二次 projection 写入前建立确定竞态窗口。"""

    def __init__(self, store: JsonlMessageStore) -> None:
        """保存真实 projection 方法并安装可控 gate。"""
        self.calls = 0
        self.entered = anyio.Event()
        self.allow = anyio.Event()
        self._original = store.append_projection_batch
        store.append_projection_batch = MethodType(  # type: ignore[method-assign]
            self._pause_second_projection,
            store,
        )

    async def _pause_second_projection(
        self,
        _store: JsonlMessageStore,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """第二次 accepted item 物化前等待测试放行。"""
        self.calls += 1
        if self.calls == 2:
            self.entered.set()
            await self.allow.wait()
        await self._original(thread_id, items, expected_identity)


async def _wait_until(predicate: object) -> None:
    """在测试 deadline 内等待同步谓词成立。"""
    assert callable(predicate)
    with anyio.fail_after(1):
        while not predicate():
            await anyio.lowlevel.checkpoint()


def _build_release_scenario(
    tmp_path: Path,
) -> tuple[
    JsonlSessionJournalCore,
    _ObservedJournalCore,
    EnginePool,
    _ProjectionGate,
]:
    """构建使用真实 Journal、projection store 与 release 的场景。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    core = _ObservedJournalCore(real_core)
    store = JsonlMessageStore(tmp_path / "threads")
    gate = _ProjectionGate(store)
    pool = EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=_blocking_observed_client(),
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
    return real_core, core, pool, gate


async def _begin_release_and_assert_application_convergence(
    *,
    pool: EnginePool,
    session_id: str,
    engine: AgentEngine,
    state: AuditedSessionState,
    core: _ObservedJournalCore,
    real_core: JsonlSessionJournalCore,
    gate: _ProjectionGate,
) -> asyncio.Task[None]:
    """验证 release 等待 application 收敛且 FINISHING 拒绝新输入。"""
    release = asyncio.create_task(pool.release(session_id))
    await anyio.sleep(0.05)
    assert not release.done()
    assert not core.terminal_entered.is_set()

    gate.allow.set()
    await core.terminal_entered.wait()
    assert state.coordinator.snapshot().accepted_work_ids == ()
    assert len(engine._history) == 2  # noqa: SLF001
    before_late = [item async for item in real_core.load(session_id)]
    projected = state.projector.state(engine.thread_id)
    conversation_seqs = [
        item.seq
        for item in before_late
        if item.record_type == "conversation_item"
    ]
    assert projected.projected_seq == max(conversation_seqs)
    with pytest.raises(SessionFinishingError):
        await engine.submit(UserMessage(text="late finishing"))
    assert [item async for item in real_core.load(session_id)] == before_late
    return release


def _assert_exact_committed_journal(
    committed: list[JournalEnvelope],
    *,
    first_id: str,
    second_id: str,
    thread_id: str,
) -> None:
    """断言两个 accepted submission 与 terminal 的精确 Journal 序列。"""
    business_records = [
        item
        for item in committed
        if item.record_type not in {
            "llm_request_committed",
            "llm_response_checkpoint",
        }
    ]
    assert [item.record_type for item in business_records] == [
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
        batch = business_records[offset : offset + 3]
        assert [item.submission_id for item in batch] == [submission_id] * 3
        assert batch[0].record_id == (
            f"{submission_id}:submission_accepted:none:0"
        )
        assert batch[1].record_id == f"{submission_id}:conversation_item:none:0"
        assert batch[2].record_id == (
            f"{submission_id}:submission_applied:none:0"
        )
    attempts = [
        item
        for item in committed
        if item.record_type == "llm_request_committed"
    ]
    assert len(attempts) == 1
    assert attempts[0].submission_id == first_id
    checkpoints = [
        item
        for item in committed
        if item.record_type == "llm_response_checkpoint"
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0].operation_id == attempts[0].operation_id
    assert checkpoints[0].attempt_id == attempts[0].attempt_id
    assert checkpoints[0].causation_id == attempts[0].record_id
    assert business_records[-2].thread_id == thread_id
    assert business_records[-1].record_type == "session_ended"


@pytest.mark.asyncio
async def test_real_pool_release_waits_for_accepted_application_and_orders_journal(
    tmp_path: Path,
) -> None:
    """release 必须等 accepted projection 收敛后才 terminal/close。"""
    real_core, core, pool, gate = _build_release_scenario(tmp_path)
    session_id = "ses_release_queue"
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="entry",
    )
    state = pool._audit_sessions[session_id]  # noqa: SLF001
    first_id = await engine.submit(UserMessage(text="first accepted"))
    await _wait_until(lambda: gate.calls == 1)
    second_id = await engine.submit(UserMessage(text="second accepted"))
    await gate.entered.wait()
    release = await _begin_release_and_assert_application_convergence(
        pool=pool,
        session_id=session_id,
        engine=engine,
        state=state,
        core=core,
        real_core=real_core,
        gate=gate,
    )

    core.allow_terminal.set()
    await release
    committed = [item async for item in real_core.load(session_id)]
    _assert_exact_committed_journal(
        committed,
        first_id=first_id,
        second_id=second_id,
        thread_id=engine.thread_id,
    )
    assert state.coordinator.snapshot().accepted_work_ids == ()
    assert core.close_calls == 1

    await pool.close()
    assert core.close_calls == 1
