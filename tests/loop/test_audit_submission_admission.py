"""AgentEngine 的 Journal-first UserMessage admission 集成测试。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    ConversationItemV1,
    JournalAck,
    JournalEnvelope,
    RootThreadDescriptor,
    SessionDescriptor,
    SubmissionAcceptedV1,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers.sim import RoutingSimClient, SimClient, SimTurn
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditCoordinator,
    SessionAuditFrozenError,
)
from taifeng.loop.audit_admission import (
    AcceptedUserMessage,
    AuditedUserMessageSubmission,
    InvalidAuditedSubmissionError,
    ReplayedUserMessage,
    admit_user_message,
    prepare_user_message,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.engine import AgentEngine
from taifeng.loop.submission import (
    CompactNow,
    InjectSystemMessage,
    RefreshSnapshot,
    Resume,
    Rewind,
    Submission,
    ThreadRollback,
    UpdateInstructions,
    UserMessage,
)
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


class _LoadRaisingJournalCore:
    """真实 append 后在 strict load 边界抛指定 BaseException。"""

    def __init__(
        self,
        inner: JsonlSessionJournalCore,
        error_type: type[BaseException],
    ) -> None:
        self.inner = inner
        self.error_type = error_type

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """委托真实 core 产生 definite durable ack。"""
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
        """在 strict receipt load 入口抛测试指定异常。"""
        del session_id, after_seq
        raise self.error_type("injected strict load boundary")
        if False:  # pragma: no cover - 保持 async generator protocol
            yield

    async def close_session(self, lease: SessionLease) -> None:
        """委托真实 per-Session close。"""
        await self.inner.close_session(lease)


def _blocking_sim_client(*, signal: str = "release-blocked") -> SimClient:
    """用 reviewed Sim signal 构造确定性阻塞的单次采样。"""
    return SimClient(
        turns=[SimTurn(text="unused", await_signal=signal)],
    )


def _observed_test_client(model_client: object | None) -> AttemptObservableClientAdapter:
    """只把 exact reviewed Sim client 转成官方 observer adapter。"""
    if type(model_client) is AttemptObservableClientAdapter:
        return cast("AttemptObservableClientAdapter", model_client)
    selected = model_client if model_client is not None else SimClient(turns=[])
    if type(selected) not in (SimClient, RoutingSimClient):
        raise TypeError("audited test requires an exact reviewed Sim client")
    return AttemptObservableClientAdapter(
        cast("SimClient | RoutingSimClient", selected),
        provider="test",
        default_model="mock-model",
    )


async def _create_audit_runtime(
    tmp_path: Path,
    skills_dir: Path,
    *,
    core_override: (
        _PausingJournalCore | _AdversarialJournalCore | _LoadRaisingJournalCore | None
    ),
    finish_timeout: float,
) -> tuple[
    object,
    JsonlSessionJournalCore,
    SessionAuditCoordinator,
    str,
    str,
]:
    """建立 entry、Journal session 与 coordinator。"""
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
    coordinator = SessionAuditCoordinator(
        core=core_override or core,
        lease=created.lease,
        expected_seq=created.ack.last_seq,
        finish_timeout=finish_timeout,
    )
    return (registry, core, coordinator, thread_id, session_id)


async def _create_projector(
    store: JsonlMessageStore,
    *,
    thread_id: str,
    session_id: str,
    entry_skill_id: str,
) -> JournalConversationProjector:
    """建立 audited conversation projection target。"""
    projector = JournalConversationProjector(store)
    await projector.bootstrap_thread(
        thread_id=thread_id,
        cwd=None,
        entry_skill_id=entry_skill_id,
        source=f"session:{session_id}",
        extra={
            "audit_required": True,
            "journal_session_id": session_id,
            "journal_schema_version": 1,
        },
    )
    return projector


async def _engine_with_audit(
    tmp_path: Path,
    skills_dir: Path,
    *,
    core_override: (
        _PausingJournalCore | _AdversarialJournalCore | _LoadRaisingJournalCore | None
    ) = None,
    model_client: object | None = None,
    store_override: JsonlMessageStore | None = None,
    finish_timeout: float = 30.0,
    submission_queue_size: int = 256,
    image_input_policy: ImageInputPolicy | None = None,
) -> tuple[AgentEngine, SessionAuditCoordinator, JsonlSessionJournalCore]:
    """使用真实 Engine、Coordinator、Journal 和 projector 建立审计会话。"""
    registry, core, coordinator, thread_id, session_id = await _create_audit_runtime(
        tmp_path,
        skills_dir,
        core_override=core_override,
        finish_timeout=finish_timeout,
    )
    entry = registry.get("code-reviewer")
    assert entry is not None
    store = store_override or JsonlMessageStore(tmp_path / "threads")
    projector = await _create_projector(
        store,
        thread_id=thread_id,
        session_id=session_id,
        entry_skill_id=entry.id,
    )
    engine = AgentEngine(
        entry_skill=entry,
        skill_snapshot=registry.snapshot(),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        model_client=_observed_test_client(model_client),
        store=store,
        thread_id=thread_id,
        session_id=session_id,
        submission_queue_size=submission_queue_size,
        image_input_policy=image_input_policy,
    )
    engine._audit_state = SimpleNamespace(  # type: ignore[attr-defined]  # noqa: SLF001
        thread_id=thread_id,
        coordinator=coordinator,
        projector=projector,
        max_attachment_bytes=1024,
        max_total_attachment_bytes=4096,
    )
    return engine, coordinator, core


@pytest.mark.anyio
async def test_audited_disabled_image_is_rejected_before_conversation_commit(
    tmp_path: Path, skills_dir: Path
) -> None:
    """strict audit 默认关闭图片时只写脱敏拒绝，不写图片 conversation item。"""
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    attachment = ImageAttachmentV1(
        media_type="image/png",
        size=len(image_bytes),
        sha256=hashlib.sha256(image_bytes).hexdigest(),
        content=base64.b64encode(image_bytes).decode("ascii"),
    ).model_dump()
    client = SimClient(
        turns=[],
        capabilities=ModelCapabilities(
            input_modalities=frozenset({"text", "image"}),
            provider="sim",
            protocol="sim",
        ),
    )
    engine, _, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )

    with pytest.raises(InvalidAuditedSubmissionError):
        await engine.submit(UserMessage(text="inspect", attachments=[attachment]))

    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    assert [envelope.record_type for envelope in committed[-1:]] == [
        "submission_rejected"
    ]
    assert not any(
        envelope.record_type == "conversation_item" for envelope in committed
    )
    assert attachment["content"] not in repr(committed)
    assert engine._submissions.empty()  # noqa: SLF001
    assert engine._history == []  # noqa: SLF001


@pytest.mark.anyio
async def test_audited_enabled_image_preserves_detail_in_journal(
    tmp_path: Path, skills_dir: Path
) -> None:
    """strict audit 启用图片后，合法图片及 detail 必须进入原子 acceptance。"""
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    attachment = ImageAttachmentV1(
        media_type="image/png",
        size=len(image_bytes),
        sha256=hashlib.sha256(image_bytes).hexdigest(),
        content=base64.b64encode(image_bytes).decode("ascii"),
        detail="high",
    ).model_dump()
    client = SimClient(
        turns=[],
        capabilities=ModelCapabilities(
            input_modalities=frozenset({"text", "image"}),
            provider="sim",
            protocol="sim",
        ),
    )
    engine, _, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
        image_input_policy=ImageInputPolicy(
            enabled=True,
            max_images=1,
            max_item_bytes=1024,
            max_total_bytes=1024,
            allowed_media_types=frozenset({"image/png"}),
        ),
    )

    prepared = prepare_user_message(
        engine._audit_state,  # type: ignore[attr-defined]  # noqa: SLF001
        Submission(op=UserMessage(text="inspect", attachments=[attachment])),
        image_input_policy=engine._image_input_policy,  # noqa: SLF001
        model_input_capabilities=engine._model_client.capabilities,  # noqa: SLF001
    )
    assert prepared.attachments[0].detail == "high"

    submission_id = await engine.submit(
        UserMessage(text="inspect", attachments=[attachment])
    )

    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    accepted_envelope = next(
        envelope
        for envelope in committed
        if envelope.record_type == "submission_accepted"
        and envelope.submission_id == submission_id
    )
    accepted = SubmissionAcceptedV1.model_validate(accepted_envelope.payload)
    assert accepted.attachments is not None
    assert accepted.attachments[0].detail == "high"
    conversation = next(
        envelope
        for envelope in committed
        if envelope.record_type == "conversation_item"
        and envelope.submission_id == submission_id
    )
    assert conversation.payload["payload"]["attachments"][0]["detail"] == "high"


async def _wait_for_applied_history(engine: AgentEngine) -> None:
    """等待 actor 把 durable user conversation item 应用到 hot history。"""
    with anyio.fail_after(1):
        while not engine._history:  # noqa: SLF001
            await anyio.lowlevel.checkpoint()


def _audited_submission(
    submission_id: str,
    text: str,
    *,
    submitted_at: datetime,
    turn_index: int = 0,
) -> AuditedUserMessageSubmission:
    """构造完整 audited-only frozen submission。"""
    return AuditedUserMessageSubmission(
        submission_id=submission_id,
        submitted_at=submitted_at,
        accepted_turn_index=turn_index,
        text=text,
        attachments=(),
    )


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
    client = _blocking_sim_client()
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
async def test_queued_user_messages_receive_unique_durable_turn_indexes(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """首 turn 阻塞时排队/并发 admission 仍必须按单一顺序点分配 index。"""
    client = _blocking_sim_client()
    engine, _, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    cancel = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(cancel))
    queued_ids: list[str] = []

    async def submit(text: str) -> None:
        queued_ids.append(await engine.submit(UserMessage(text=text)))

    try:
        await engine.submit(UserMessage(text="first"))
        await _wait_for_applied_history(engine)
        assert engine._turn_index == 0  # noqa: SLF001

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(submit, "second")
            tasks.start_soon(submit, "third")

        committed = [
            envelope async for envelope in core.load("ses_audit_submission")
        ]
        accepted = [
            SubmissionAcceptedV1.model_validate(envelope.payload)
            for envelope in committed
            if envelope.record_type == "submission_accepted"
        ]
        assert len(queued_ids) == 2
        assert [item.turn_index for item in accepted] == [0, 1, 2]
    finally:
        cancel.cancel()
        await actor


@pytest.mark.anyio
@pytest.mark.parametrize(
    "op",
    [
        CompactNow(),
        Rewind(node_id="n1"),
        Resume(thread_id="t1", resolutions={}),
        InjectSystemMessage(text="x"),
        RefreshSnapshot(),
        ThreadRollback(),
        UpdateInstructions(layer_name="L", new_source="s"),
    ],
)
async def test_unsupported_dynamic_op_is_rejected_before_effect(
    tmp_path: Path,
    skills_dir: Path,
    op: object,
) -> None:
    """audit 能力面外的动态 Op：durable submission_rejected，不入队、不执行。"""
    from taifeng.loop.audit_admission import UnsupportedAuditedOperationError

    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)

    with pytest.raises(UnsupportedAuditedOperationError):
        await engine.submit(op)  # type: ignore[arg-type]

    # 未入队（不执行）
    assert engine._submissions.empty()  # noqa: SLF001
    # durable 落一条稳定 submission_rejected（capability），不含 Op 原文
    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    rejected = next(
        e for e in committed if e.record_type == "submission_rejected"
    )
    assert rejected.payload["op_kind"] == op.kind  # type: ignore[attr-defined]
    assert rejected.payload["stable_error"]["code"] == "audit_unsupported_operation"
    assert rejected.payload["stable_error"]["failure_class"] == "capability"
    # 拒绝不冻结：Session 仍健康、可继续接受合法提交
    assert coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_two_sessions_one_frozen_does_not_block_other(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """两个独立 audited Session：一个冻结不影响另一个的效果闸与提交。"""
    engine_a, coord_a, core_a = await _engine_with_audit(
        tmp_path / "a", skills_dir
    )
    engine_b, coord_b, core_b = await _engine_with_audit(
        tmp_path / "b", skills_dir
    )

    # 冻结 A
    coord_a.freeze(RuntimeError("boom"))
    assert coord_a.health is AuditHealth.RECOVERY_REQUIRED
    assert coord_a.effect_gate_open is False

    # B 完全不受影响：健康、效果闸开、能正常接受 UserMessage
    assert coord_b.health is AuditHealth.HEALTHY
    assert coord_b.effect_gate_open is True
    sub_id = await engine_b.submit(UserMessage(text="still works"))
    assert sub_id
    committed_b = [e async for e in core_b.load("ses_audit_submission")]
    assert any(e.record_type == "submission_accepted" for e in committed_b)


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


def test_legacy_submission_dump_has_only_id_and_op() -> None:
    """audit 内部字段不得扩张公开 Submission 的序列化外形。"""
    submission = Submission(
        id="sub_legacy_dump",
        op=UserMessage(text="legacy dump"),
    )

    assert submission.model_dump(mode="json") == {
        "id": "sub_legacy_dump",
        "op": {
            "kind": "user_message",
            "text": "legacy dump",
            "attachments": [],
        },
    }


def test_legacy_submission_schema_has_only_id_and_op() -> None:
    """公开 JSON schema 必须保留 audit 接入前的 id/op 字段集合。"""
    schema = Submission.model_json_schema()

    assert set(schema["properties"]) == {"id", "op"}
    assert set(schema["required"]) == {"op"}


def test_legacy_submission_remains_mutable() -> None:
    """既有 caller 仍可更新 Submission identity 与 Op。"""
    submission = Submission(op=UserMessage(text="before"))

    submission.id = "sub_mutated"
    submission.op = UserMessage(text="after")

    assert submission.id == "sub_mutated"
    assert submission.op == UserMessage(text="after")


@pytest.mark.anyio
async def test_same_logical_submission_retries_with_exact_idempotent_ack(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """相同 id/op/submitted_at 重建三记录必须得到原 ack，且 coordinator 不冻结。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    submitted_at = datetime(2026, 7, 24, 8, 30, tzinfo=UTC)
    submission = _audited_submission(
        "sub_stable_retry",
        "same logical input",
        submitted_at=submitted_at,
    )
    state = engine._audit_state  # type: ignore[attr-defined]  # noqa: SLF001

    first = await admit_user_message(state, submission)
    assert isinstance(first, AcceptedUserMessage)
    await first.accepted_work.complete()
    second = await admit_user_message(state, submission)

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
    submission = _audited_submission(
        "sub_completed_replay",
        "exact replay",
        submitted_at=datetime(2026, 7, 24, 9, 15, tzinfo=UTC),
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
        first_id = await engine._submit_audited_user_message(submission)  # noqa: SLF001
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

        second_id = await engine._submit_audited_user_message(submission)  # noqa: SLF001

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
            "llm_request_committed",
            "llm_response_checkpoint",
            # 7.6：最终逻辑响应 + assistant 会话项在 checkpoint 之后同批 durable
            "llm_response_committed",
            "conversation_item",
        ]
        assert committed[3].payload["turn_index"] == 0
        # 最终响应引用本 turn 的 request/checkpoint lineage，assistant 会话项紧随其后
        assert committed[-2].record_type == "llm_response_committed"
        assert committed[-2].payload["checkpoint_record_id"] == committed[7].record_id
        assert committed[-1].record_type == "conversation_item"
        assert committed[-1].causation_id == committed[-2].record_id
        assert (
            ConversationItemV1.model_validate(committed[-1].payload).item_kind
            == "assistant_message"
        )
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
    submission = _audited_submission(
        "sub_incomplete_retry",
        "still running",
        submitted_at=datetime(2026, 7, 24, 9, 30, tzinfo=UTC),
    )
    state = engine._audit_state  # type: ignore[attr-defined]  # noqa: SLF001
    first = await admit_user_message(state, submission)
    assert isinstance(first, AcceptedUserMessage)

    with pytest.raises(ValueError, match="work already accepted"):
        await admit_user_message(state, submission)

    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    assert len(committed) == 6
    assert coordinator.snapshot().accepted_work_ids == (submission.id,)
    assert coordinator.health is AuditHealth.HEALTHY
    await first.accepted_work.complete()


def test_audited_submission_timestamp_and_turn_index_are_frozen_internal_facts() -> None:
    """审计时间/index 只存在于内部 immutable DTO。"""
    submitted_at = datetime(2026, 7, 24, 8, 30, tzinfo=UTC)
    submission = _audited_submission(
        "sub_roundtrip",
        "legacy shape",
        submitted_at=submitted_at,
    )

    with pytest.raises(FrozenInstanceError):
        submission.accepted_turn_index = 2  # type: ignore[misc]

    assert submission.submitted_at == submitted_at
    assert submission.accepted_turn_index == 0
    assert submission.text == "legacy shape"


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
        model_client=_blocking_sim_client(),
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
        with pytest.raises(SessionAuditFrozenError):
            await actor

    assert engine._history == []  # noqa: SLF001
    assert engine._audit_state.projector.state(engine.thread_id).projected_seq == 0  # type: ignore[attr-defined]  # noqa: SLF001
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
