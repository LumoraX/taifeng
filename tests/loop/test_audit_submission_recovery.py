"""Audited UserMessage 拒绝、handoff 与 finish 收敛测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal.canonical import canonical_bytes
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditFrozenError,
    SessionFinishingError,
    SessionLifecycle,
)
from taifeng.loop.audit_admission import InvalidAuditedSubmissionError
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import UserMessage
from tests.loop.test_audit_submission_admission import _engine_with_audit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.conversation.journal import (
        JournalAck,
        JournalEnvelope,
        JournalRecord,
        SessionLease,
    )
    from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore


class _UnsafeValue:
    """repr 含敏感信息的非法自由结构值。"""

    def __repr__(self) -> str:
        """模拟禁止进入 Journal 的任意 repr。"""
        return "secret=TOKEN-123 object at 0xDEADBEEF"


class _ObservedQueue(asyncio.Queue[object]):
    """精确暴露满队列 put 已进入等待的测试队列。"""

    def __init__(self, maxsize: int) -> None:
        """创建有界 queue 与 handoff 等待事件。"""
        super().__init__(maxsize=maxsize)
        self.put_blocked = anyio.Event()

    async def put(self, item: object) -> None:
        """满时先通知测试，再复用 asyncio.Queue 原语义。"""
        if self.full():
            self.put_blocked.set()
        await super().put(item)


class _CountingCore:
    """为真实 JSONL core 增加 close 次数观测。"""

    def __init__(self, inner: JsonlSessionJournalCore) -> None:
        """保存委托 core。"""
        self.inner = inner
        self.close_calls = 0

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """委托真实 durable append。"""
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
        """委托真实 strict load。"""
        async for envelope in self.inner.load(session_id, after_seq=after_seq):
            yield envelope

    async def close_session(self, lease: SessionLease) -> None:
        """计数后委托唯一 Session close。"""
        self.close_calls += 1
        await self.inner.close_session(lease)


class _RejectFailingCore(_CountingCore):
    """只让 submission_rejected append 失败的 Journal core。"""

    def __init__(self, inner: JsonlSessionJournalCore, error: BaseException) -> None:
        """保存待注入异常。"""
        super().__init__(inner)
        self.error = error

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """rejection 写入点抛错，其他 batch 委托真实 core。"""
        if any(record.record_type == "submission_rejected" for record in records):
            raise self.error
        return await super().append_batch(
            records,
            lease=lease,
            expected_seq=expected_seq,
        )


class _FatalSink:
    """在 accepted token handoff 时抛进程级 fatal。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.put_calls = 0

    async def put(self, item: object) -> None:
        """不取得 ownership，原样抛 KeyboardInterrupt。"""
        del item
        self.put_calls += 1
        raise KeyboardInterrupt


def _invalid_user_message() -> UserMessage:
    """构造 Pydantic 外形合法、V1 attachment 内容非法的输入。"""
    return UserMessage(
        text="invalid attachment",
        attachments=[
            {
                "kind": "inline",
                "media_type": "text/plain",
                "size": 1,
                "sha256": "0" * 64,
                "encoding": "base64",
                "content": _UnsafeValue(),
            }
        ],
    )


async def _wait_until(predicate: object) -> None:
    """在测试 deadline 内等待同步谓词为真。"""
    assert callable(predicate)
    with anyio.fail_after(1):
        while not predicate():
            await anyio.lowlevel.checkpoint()


@pytest.mark.anyio
async def test_invalid_user_message_writes_one_safe_rejection_without_freezing(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """非法 canonical 输入只写安全 rejection，不排队、不冻结、不执行。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)

    with pytest.raises(InvalidAuditedSubmissionError) as rejected:
        await engine.submit(_invalid_user_message())

    committed = [
        envelope async for envelope in core.load("ses_audit_submission")
    ]
    wire = canonical_bytes([envelope.payload for envelope in committed]).decode()
    assert [envelope.record_type for envelope in committed[3:]] == [
        "submission_rejected"
    ]
    rejection = committed[3]
    submission_id = rejected.value.submission_id
    descriptor_hash = rejected.value.descriptor_hash
    assert rejection.record_id == (
        f"{submission_id}:submission_rejected:none:0"
    )
    assert rejection.operation_id == submission_id
    assert rejection.submission_id == submission_id
    assert rejection.thread_id == engine.thread_id
    assert rejection.payload["op_kind"] == "user_message"
    assert rejection.payload["input_descriptor_hash"] == descriptor_hash
    assert rejection.payload["stable_error"]["descriptor_hash"] == descriptor_hash
    assert rejected.value.__context__ is None
    assert "TOKEN-123" not in wire
    assert "0xDEADBEEF" not in wire
    assert "secret" not in wire
    assert engine._submissions.empty()  # noqa: SLF001
    assert engine._history == []  # noqa: SLF001
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.snapshot().accepted_work_ids == ()


@pytest.mark.anyio
async def test_finishing_lifecycle_wins_over_invalid_input_without_record(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """FINISHING 后非法输入也只返回 lifecycle error，不伪造 rejection。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)

    async def durable_accept() -> None:
        return None

    work = await coordinator.admit_work("sub_finish_blocker", durable_accept)
    finish = asyncio.create_task(
        coordinator.finish(thread_terminals=(), reason="test_finishing")
    )
    await _wait_until(
        lambda: coordinator.snapshot().lifecycle is SessionLifecycle.FINISHING
    )
    before = [envelope async for envelope in core.load("ses_audit_submission")]

    with pytest.raises(SessionFinishingError):
        await engine.submit(_invalid_user_message())

    during = [envelope async for envelope in core.load("ses_audit_submission")]
    assert during == before
    assert engine._submissions.empty()  # noqa: SLF001
    await work.complete()
    result = await finish
    assert result.audit_complete is True


@pytest.mark.anyio
async def test_cancelled_full_queue_handoff_freezes_and_retires_failed_work(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """ack 后 queue.put 取消必须 recovery-required，且失败 token 不得泄漏/执行。"""
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        submission_queue_size=1,
    )
    observed_queue = _ObservedQueue(maxsize=1)
    engine._submissions = observed_queue  # type: ignore[assignment]  # noqa: SLF001
    queued_id = await engine.submit(UserMessage(text="already queued"))
    blocked_submit = asyncio.create_task(
        engine.submit(UserMessage(text="durable but queue full"))
    )
    await observed_queue.put_blocked.wait()
    assert coordinator.expected_seq == 9
    assert len(coordinator.snapshot().accepted_work_ids) == 2

    blocked_submit.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_submit
    after_cancel = coordinator.snapshot()

    root = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(root))
    try:
        await _wait_until(
            lambda: queued_id not in coordinator.snapshot().accepted_work_ids
        )
    finally:
        root.cancel()
        await actor

    assert after_cancel.health is AuditHealth.RECOVERY_REQUIRED
    assert after_cancel.effect_gate_open is False
    assert len(after_cancel.accepted_work_ids) == 1
    assert after_cancel.accepted_work_ids == (queued_id,)
    assert coordinator.snapshot().accepted_work_ids == ()
    assert engine._history == []  # noqa: SLF001


@pytest.mark.anyio
async def test_shutdown_serializes_with_admission_and_finish_converges_queue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """关闭 intake 不得越过 ack→enqueue；queued work 收敛后才写 terminal/close。"""
    from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore

    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    counting_core = _CountingCore(real_core)
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=counting_core,  # type: ignore[arg-type]
        finish_timeout=0.1,
        submission_queue_size=1,
    )
    first_id = await engine.submit(UserMessage(text="first queued"))
    second = asyncio.create_task(engine.submit(UserMessage(text="second accepted")))
    await _wait_until(
        lambda: coordinator.expected_seq == 9
        and len(coordinator.snapshot().accepted_work_ids) == 2
    )
    shutdown = asyncio.create_task(engine.shutdown())
    await anyio.lowlevel.checkpoint()
    late = asyncio.create_task(engine.submit(UserMessage(text="must be rejected")))

    root = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(root))
    done, pending = await asyncio.wait(
        {second, shutdown, late, actor},
        timeout=1,
    )
    converged = not pending
    if pending:
        root.cancel()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    assert converged, "shutdown/admission race left an owner or actor unresolved"

    second_id = second.result()
    shutdown.result()
    with pytest.raises(SessionFinishingError):
        late.result()
    actor.result()

    result = await coordinator.finish(
        thread_terminals=(),
        reason="accepted_queue_converged",
    )
    committed = [
        envelope async for envelope in real_core.load("ses_audit_submission")
    ]
    accepted_ids = [
        envelope.submission_id
        for envelope in committed
        if envelope.record_type == "submission_accepted"
    ]
    assert accepted_ids == [first_id, second_id]
    assert committed[-1].record_type == "session_ended"
    assert result.audit_complete is True
    assert result.lease_released is True
    assert coordinator.snapshot().accepted_work_ids == ()
    assert counting_core.close_calls == 1


@pytest.mark.anyio
async def test_rejection_append_failure_freezes_without_fabricated_record(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """非法输入 rejection 无 definite ack 时仍属于 Journal uncertainty。"""
    from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore

    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    failing_core = _RejectFailingCore(
        real_core,
        OSError("secret=TOKEN-123 at 0xDEADBEEF"),
    )
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        core_override=failing_core,  # type: ignore[arg-type]
    )

    with pytest.raises(SessionAuditFrozenError):
        await engine.submit(_invalid_user_message())

    committed = [
        envelope async for envelope in real_core.load("ses_audit_submission")
    ]
    assert [envelope.record_type for envelope in committed] == [
        "session_started",
        "thread_created",
        "thread_bound",
    ]
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.snapshot().accepted_work_ids == ()
    assert engine._submissions.empty()  # noqa: SLF001


@pytest.mark.anyio
async def test_fatal_handoff_propagates_after_freeze_and_retirement(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """ack 后 fatal 不得被改写，但必须先冻结并精确退休 ownership。"""
    engine, coordinator, _ = await _engine_with_audit(tmp_path, skills_dir)
    sink = _FatalSink()
    engine._submissions = sink  # type: ignore[assignment]  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt):
        await engine.submit(UserMessage(text="fatal handoff"))

    snapshot = coordinator.snapshot()
    assert sink.put_calls == 1
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.accepted_work_ids == ()
    assert engine._history == []  # noqa: SLF001


@pytest.mark.anyio
async def test_submit_after_actor_terminated_freezes_and_retires_acceptance(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """已终止 actor 不能取得新 durable token；acceptance 留给 recovery。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    root = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(root))
    await _wait_until(lambda: engine.introspect()["running"] is True)
    root.cancel()
    await actor

    with pytest.raises(SessionAuditFrozenError):
        await engine.submit(UserMessage(text="actor already stopped"))

    committed = [
        envelope async for envelope in core.load("ses_audit_submission")
    ]
    assert [envelope.record_type for envelope in committed[3:]] == [
        "submission_accepted",
        "conversation_item",
        "submission_applied",
    ]
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert coordinator.snapshot().accepted_work_ids == ()
    assert engine._submissions.empty()  # noqa: SLF001
    assert engine._history == []  # noqa: SLF001
