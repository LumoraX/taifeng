"""AgentEngine 的 Journal-first UserMessage admission 集成测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    ConversationItemV1,
    JournalAck,
    JournalEnvelope,
    ProjectionResult,
    RootThreadDescriptor,
    SessionDescriptor,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditCoordinator,
    SessionAuditFrozenError,
)
from taifeng.loop.audit_admission import (
    AcceptedUserMessage,
    ReplayedUserMessage,
    admit_user_message,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.engine import AgentEngine
from taifeng.loop.submission import Submission, UserMessage
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.conversation.journal import (
        JournalRecord,
        SessionLease,
    )
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest


class _PausingJournalCore:
    """在业务 append 前暂停，同时保留真实 JSONL Journal 行为。"""

    def __init__(self, inner: JsonlSessionJournalCore) -> None:
        self.inner = inner
        self.append_entered = anyio.Event()
        self.release_append = anyio.Event()

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """暂停后委托真实 core durable commit。"""
        self.append_entered.set()
        await self.release_append.wait()
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
    ) -> AsyncIterator[JournalEnvelope]:
        """委托真实 core strict scan。"""
        async for envelope in self.inner.load(session_id, after_seq=after_seq):
            yield envelope

    async def close_session(self, lease: SessionLease) -> None:
        """委托真实 per-Session close。"""
        await self.inner.close_session(lease)


class _AdversarialJournalCore:
    """返回 protocol-compatible 但与刚提交 records 不一致的 receipt。"""

    def __init__(
        self,
        inner: JsonlSessionJournalCore,
        corruption: str,
    ) -> None:
        self.inner = inner
        self.corruption = corruption
        self.returned_ack: JournalAck | None = None

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """真实提交后仅伪造测试指定的 ack 字段。"""
        ack = await self.inner.append_batch(
            records,
            lease=lease,
            expected_seq=expected_seq,
        )
        if self.corruption == "tail":
            ack = ack.model_copy(update={"tail_hash": "f" * 64})
        self.returned_ack = ack
        return ack

    async def load(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """从真实 strict scan 读取，再只篡改一个 authoritative 字段。"""
        envelopes = [
            envelope
            async for envelope in self.inner.load(session_id, after_seq=after_seq)
        ]
        if self.corruption != "tail":
            envelopes = self._corrupt(envelopes)
        for envelope in envelopes:
            yield envelope

    def _corrupt(
        self,
        envelopes: list[JournalEnvelope],
    ) -> list[JournalEnvelope]:
        """构造仍满足 JournalEnvelope DTO 的单字段伪 receipt。"""
        target = 1
        envelope = envelopes[target]
        if self.corruption == "thread":
            changed = envelope.model_copy(update={"thread_id": "thr_forged"})
        elif self.corruption == "source":
            changed = envelope.model_copy(
                update={"actor": ActorRef(kind="user", source="forged")}
            )
        elif self.corruption == "type":
            changed = envelope.model_copy(update={"record_type": "turn_started"})
        else:
            payload = dict(envelope.payload)
            payload["item_id"] = "item_forged"
            changed = envelope.model_copy(update={"payload": payload})
        return [*envelopes[:target], changed, *envelopes[target + 1 :]]

    async def close_session(self, lease: SessionLease) -> None:
        """委托真实 per-Session close。"""
        await self.inner.close_session(lease)


class _BlockingSession:
    """让 actor 停在首个 LLM 调用，便于观察 admission 已应用状态。"""

    def __init__(self) -> None:
        self.release = anyio.Event()

    async def __aenter__(self) -> _BlockingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """在测试释放前不产生 provider 事件。"""
        del request
        await self.release.wait()
        if False:
            yield


class _BlockingClient:
    """每个 turn 返回同一可控阻塞 session。"""

    def __init__(self) -> None:
        self.blocking_session = _BlockingSession()

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> _BlockingSession:
        """忽略模型参数，返回无网络 effect 的测试 session。"""
        del cancel, model
        return self.blocking_session


async def _engine_with_audit(
    tmp_path: Path,
    skills_dir: Path,
    *,
    core_override: _PausingJournalCore | _AdversarialJournalCore | None = None,
    model_client: object | None = None,
) -> tuple[AgentEngine, SessionAuditCoordinator, JsonlSessionJournalCore]:
    """使用真实 Engine、Coordinator、Journal 和 projector 建立审计会话。"""
    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None
    thread_id = "thr_audit_submission"
    session_id = "ses_audit_submission"
    core = core_override.inner if core_override is not None else JsonlSessionJournalCore(
        tmp_path / "journal"
    )
    created = await core.create_session(
        SessionDescriptor(
            session_id=session_id,
            creation_operation_id=f"{session_id}:create",
            writer_id="writer_test",
            root_thread=RootThreadDescriptor(
                thread_id=thread_id,
                entry_skill_id=entry.id,
                source="test",
            ),
            config={"audit_required": True},
        )
    )
    append_core = core_override or core
    coordinator = SessionAuditCoordinator(
        core=append_core,
        lease=created.lease,
        expected_seq=created.ack.last_seq,
    )
    store = JsonlMessageStore(tmp_path / "threads")
    projector = JournalConversationProjector(store)
    await projector.bootstrap_thread(
        thread_id=thread_id,
        cwd=None,
        entry_skill_id=entry.id,
        source=f"session:{session_id}",
        extra={
            "audit_required": True,
            "journal_session_id": session_id,
            "journal_schema_version": 1,
        },
    )
    engine = AgentEngine(
        entry_skill=entry,
        skill_snapshot=registry.snapshot(),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        model_client=model_client or SimClient(turns=[]),
        store=store,
        thread_id=thread_id,
        session_id=session_id,
    )
    engine._audit_state = SimpleNamespace(  # type: ignore[attr-defined]  # noqa: SLF001
        thread_id=thread_id,
        coordinator=coordinator,
        projector=projector,
        max_attachment_bytes=1024,
        max_total_attachment_bytes=4096,
    )
    return engine, coordinator, core


async def _wait_for_applied_history(engine: AgentEngine) -> None:
    """等待 actor 把 durable user conversation item 应用到 hot history。"""
    with anyio.fail_after(1):
        while not engine._history:  # noqa: SLF001
            await anyio.lowlevel.checkpoint()


@pytest.mark.anyio
async def test_audited_submit_waits_for_durable_ack_before_enqueue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """Journal 三记录未 durable ack 前，queue/history/projection 均不得前进。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    pausing_core = _PausingJournalCore(real_core)
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=pausing_core,
    )
    submission_id: str | None = None

    async def submit() -> None:
        nonlocal submission_id
        submission_id = await engine.submit(UserMessage(text="durable first"))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(submit)
        with anyio.fail_after(1):
            await pausing_core.append_entered.wait()

        assert pausing_core.append_entered.is_set()
        assert engine._submissions.empty()  # noqa: SLF001
        assert engine._history == []  # noqa: SLF001
        assert coordinator.expected_seq == 3
        assert (
            engine._audit_state.projector.state(engine.thread_id).projected_seq == 0  # type: ignore[attr-defined]  # noqa: SLF001
        )

        pausing_core.release_append.set()

    assert submission_id is not None
    assert engine._submissions.qsize() == 1  # noqa: SLF001
    token = engine._submissions.get_nowait()  # noqa: SLF001
    assert token.submission_id == submission_id
    assert token.ack.record_ids == token.accepted_record_ids
    assert [envelope.record_type for envelope in token.envelopes] == [
        "submission_accepted",
        "conversation_item",
        "submission_applied",
    ]
    envelopes = [item async for item in real_core.load("ses_audit_submission")]
    assert [item.record_type for item in envelopes[3:]] == [
        "submission_accepted",
        "conversation_item",
        "submission_applied",
    ]


@pytest.mark.anyio
async def test_actor_applies_only_acknowledged_user_envelope_then_completes_work(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """actor 消费 token 后才更新 hot history/projector，并在 finally 退休 work。"""
    client = _BlockingClient()
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    submission_id = await engine.submit(UserMessage(text="apply acknowledged"))
    token = engine._submissions.get_nowait()  # noqa: SLF001
    engine._submissions.put_nowait(token)  # noqa: SLF001
    user_envelope = token.envelopes[1]
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    try:
        await _wait_for_applied_history(engine)
        with anyio.fail_after(1):
            while (
                engine._audit_state.projector.state(engine.thread_id).projected_seq  # type: ignore[attr-defined]  # noqa: SLF001
                < user_envelope.seq
            ):
                await anyio.lowlevel.checkpoint()
        projected = engine._audit_state.projector.state(engine.thread_id)  # type: ignore[attr-defined]  # noqa: SLF001
        assert [item.id for item in engine._history] == [  # noqa: SLF001
            user_envelope.payload["item_id"]
        ]
        assert projected.projected_seq == user_envelope.seq
        assert projected.stale is False
        assert coordinator.health is AuditHealth.HEALTHY
        committed = [item async for item in core.load("ses_audit_submission")]
        assert user_envelope == committed[4]
    finally:
        cancel.cancel()
        await actor

    assert submission_id not in coordinator.snapshot().accepted_work_ids


@pytest.mark.anyio
async def test_projection_failure_marks_stale_without_freezing_or_losing_hot_history(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """投影 stale 不影响 durable user item 成为 hot history 权威事实。"""
    client = _BlockingClient()
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    captured: list[tuple[tuple[JournalEnvelope, ...], JournalAck]] = []

    async def fail_projection(
        projector: JournalConversationProjector,
        envelopes: tuple[JournalEnvelope, ...],
        ack: JournalAck,
    ) -> ProjectionResult:
        del projector
        captured.append((envelopes, ack))
        return ProjectionResult(
            thread_id=engine.thread_id,
            projected_seq=0,
            stale=True,
            failure_class="append_failed",
            failure_record_id=envelopes[0].record_id,
        )

    monkeypatch.setattr(JournalConversationProjector, "project", fail_projection)
    await engine.submit(UserMessage(text="hot history remains"))
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    try:
        await _wait_for_applied_history(engine)
        with anyio.fail_after(1):
            while coordinator.projection_snapshot(engine.thread_id) is None:
                await anyio.lowlevel.checkpoint()
        projection = coordinator.projection_snapshot(engine.thread_id)
        assert projection is not None and projection.stale
        assert coordinator.health is AuditHealth.HEALTHY
        assert coordinator.effect_gate_open
        assert engine._history[0].payload["text"] == "hot history remains"  # noqa: SLF001
        assert captured[0][1].record_ids[1] == captured[0][0][0].record_id
    finally:
        cancel.cancel()
        await actor


@pytest.mark.anyio
async def test_legacy_submit_keeps_raw_submission_queue_behavior(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """未注入 audit state 时仍逐字保持原始 Submission enqueue 路径。"""
    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None
    engine = AgentEngine(
        entry_skill=entry,
        skill_snapshot=registry.snapshot(),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        model_client=SimClient(turns=[]),
        store=JsonlMessageStore(tmp_path / "legacy"),
        thread_id="thr_legacy",
    )

    submission_id = await engine.submit(UserMessage(text="legacy"))
    queued = engine._submissions.get_nowait()  # noqa: SLF001

    assert isinstance(queued, Submission)
    assert queued.id == submission_id
    assert queued.op == UserMessage(text="legacy")


@pytest.mark.anyio
async def test_same_logical_submission_retries_with_exact_idempotent_ack(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """相同 id/op/submitted_at 重建三记录必须得到原 ack，且 coordinator 不冻结。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    submitted_at = datetime(2026, 7, 24, 8, 30, tzinfo=UTC)
    submission = Submission(
        id="sub_stable_retry",
        submitted_at=submitted_at,
        op=UserMessage(text="same logical input"),
    )
    state = engine._audit_state  # type: ignore[attr-defined]  # noqa: SLF001

    first = await admit_user_message(state, submission, turn_index=0)
    assert isinstance(first, AcceptedUserMessage)
    await first.accepted_work.complete()
    second = await admit_user_message(state, submission, turn_index=0)

    assert isinstance(second, ReplayedUserMessage)
    assert second.ack == first.ack
    assert second.envelopes == first.envelopes
    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    assert len(committed) == 6
    assert ConversationItemV1.model_validate(committed[4].payload).created_at == submitted_at
    assert coordinator.expected_seq == first.ack.last_seq
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.snapshot().accepted_work_ids == ()


@pytest.mark.anyio
async def test_completed_actor_submission_replay_is_a_noop(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """真实 actor 完成后重放完整 frozen Submission 不得再次入队或执行。"""
    submission = Submission(
        id="sub_completed_replay",
        submitted_at=datetime(2026, 7, 24, 9, 15, tzinfo=UTC),
        op=UserMessage(text="exact replay"),
    )
    client = SimClient(turns=[SimTurn(text="first and only response")])
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    try:
        first_id = await engine._submit_frozen_submission(  # noqa: SLF001
            submission,
            accepted_turn_index=0,
        )
        with anyio.fail_after(2):
            while (
                coordinator.snapshot().accepted_work_ids
                or engine._turn_index == 0  # noqa: SLF001
            ):
                await anyio.lowlevel.checkpoint()

        history = tuple(engine._history)  # noqa: SLF001
        projection = engine._audit_state.projector.state(engine.thread_id)  # type: ignore[attr-defined]  # noqa: SLF001
        projected_seq = projection.projected_seq
        committed = [
            envelope async for envelope in core.load("ses_audit_submission")
        ]
        assert first_id == "sub_completed_replay"
        assert len(client.ledger.requests()) == 1
        assert engine._submissions.empty()  # noqa: SLF001

        second_id = await engine._submit_frozen_submission(  # noqa: SLF001
            submission,
            accepted_turn_index=0,
        )

        assert second_id == first_id
        assert engine._submissions.empty()  # noqa: SLF001
        assert tuple(engine._history) == history  # noqa: SLF001
        assert (
            engine._audit_state.projector.state(engine.thread_id).projected_seq  # type: ignore[attr-defined]  # noqa: SLF001
            == projected_seq
        )
        assert len(client.ledger.requests()) == 1
        assert [
            envelope async for envelope in core.load("ses_audit_submission")
        ] == committed
        assert [envelope.record_type for envelope in committed[3:]] == [
            "submission_accepted",
            "conversation_item",
            "submission_applied",
        ]
        assert committed[3].payload["turn_index"] == 0
        assert coordinator.health is AuditHealth.HEALTHY
    finally:
        cancel.cancel()
        await actor


@pytest.mark.anyio
async def test_incomplete_accepted_submission_cannot_execute_twice(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """同 work_id 尚未 complete 时重试必须拒绝，且不生成第二份执行 token。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    submission = Submission(
        id="sub_incomplete_retry",
        submitted_at=datetime(2026, 7, 24, 9, 30, tzinfo=UTC),
        op=UserMessage(text="still running"),
    )
    state = engine._audit_state  # type: ignore[attr-defined]  # noqa: SLF001
    first = await admit_user_message(state, submission, turn_index=0)
    assert isinstance(first, AcceptedUserMessage)

    with pytest.raises(ValueError, match="work already accepted"):
        await admit_user_message(state, submission, turn_index=0)

    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    assert len(committed) == 6
    assert coordinator.snapshot().accepted_work_ids == (submission.id,)
    assert coordinator.health is AuditHealth.HEALTHY
    await first.accepted_work.complete()


def test_submission_timestamp_roundtrips_without_changing_legacy_op() -> None:
    """内部稳定时间参与 retry，但 legacy Op 判别与字段保持原语义。"""
    submitted_at = datetime(2026, 7, 24, 8, 30, tzinfo=UTC)
    submission = Submission(
        id="sub_roundtrip",
        submitted_at=submitted_at,
        op=UserMessage(text="legacy shape"),
    )

    restored = Submission.model_validate_json(submission.model_dump_json())

    assert restored == submission
    assert restored.submitted_at == submitted_at
    assert restored.op == UserMessage(text="legacy shape")


@pytest.mark.anyio
@pytest.mark.parametrize("corruption", ["thread", "source", "tail", "type", "payload"])
async def test_forged_acknowledged_receipt_freezes_before_enqueue(
    tmp_path: Path,
    skills_dir: Path,
    corruption: str,
) -> None:
    """ack/tail 或 envelope 与 expected records 不一致时不得产生 queue token。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    core = _AdversarialJournalCore(real_core, corruption)
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=core,
    )

    with pytest.raises(SessionAuditFrozenError):
        await engine.submit(UserMessage(text="reject forged receipt"))

    assert engine._submissions.empty()  # noqa: SLF001
    assert engine._history == []  # noqa: SLF001
    assert engine._audit_state.projector.state(engine.thread_id).projected_seq == 0  # type: ignore[attr-defined]  # noqa: SLF001
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.effect_gate_open is False


@pytest.mark.anyio
@pytest.mark.parametrize("corruption", ["source_link", "mismatched_thread"])
async def test_actor_revalidates_full_token_before_hot_history_mutation(
    tmp_path: Path,
    skills_dir: Path,
    corruption: str,
) -> None:
    """即使内部 token 被低层篡改，actor 也先冻结且不更新 hot/projection。"""
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=_BlockingClient(),
    )
    await engine.submit(UserMessage(text="valid durable input"))
    token = engine._submissions.get_nowait()  # noqa: SLF001
    envelopes = list(token.envelopes)
    if corruption == "source_link":
        payload = dict(envelopes[1].payload)
        payload["source_record_id"] = "forged-source"
        envelopes[1] = envelopes[1].model_copy(update={"payload": payload})
    else:
        envelopes[2] = envelopes[2].model_copy(update={"thread_id": "thr_forged"})
    object.__setattr__(token, "envelopes", tuple(envelopes))
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

    assert engine._history == []  # noqa: SLF001
    assert engine._audit_state.projector.state(engine.thread_id).projected_seq == 0  # type: ignore[attr-defined]  # noqa: SLF001
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
