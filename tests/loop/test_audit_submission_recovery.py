"""Audited UserMessage 拒绝、handoff 与 finish 收敛测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.conversation.journal.canonical import canonical_bytes
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditFrozenError,
    SessionFinishingError,
    SessionLifecycle,
)
from taifeng.loop.audit_admission import (
    AcceptedUserMessage,
    InvalidAuditedSubmissionError,
)
from taifeng.loop.audit_descriptor import (
    safe_user_message_input_descriptor,
    user_message_input_descriptor_hash,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import Submission, UserMessage
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


class _ExplosiveValue:
    """任何用户 hook 被调用都会让测试立即失败。"""

    def __init__(self) -> None:
        """初始化 hook 调用计数。"""
        self.calls = 0

    def _explode(self) -> None:
        """记录并拒绝所有任意用户代码执行入口。"""
        self.calls += 1
        raise AssertionError("descriptor walker executed an arbitrary hook")

    def __repr__(self) -> str:
        """禁止 descriptor 调用 repr。"""
        self._explode()

    def __str__(self) -> str:
        """禁止 descriptor 调用 str。"""
        self._explode()

    def __hash__(self) -> int:
        """禁止 descriptor 调用 hash。"""
        self._explode()

    def __iter__(self) -> Any:
        """禁止 descriptor 把任意对象当容器遍历。"""
        self._explode()


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


class _ActorGetBlockedQueue(_ObservedQueue):
    """让 actor 停在 dequeue 内，同时保留 full-queue handoff。"""

    def __init__(self, maxsize: int) -> None:
        """创建 dequeue 观测点。"""
        super().__init__(maxsize)
        self.get_blocked = anyio.Event()

    async def get(self) -> object:
        """通知 actor 已等待 queue，但不转移 token ownership。"""
        self.get_blocked.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable")


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


def _deep_list(depth: int) -> list[object]:
    """构造超过 descriptor 深度上限的 builtin list。"""
    root: list[object] = []
    cursor = root
    for _ in range(depth):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    return root


def _message_with_raw_value(
    value: object,
    *,
    text: str = "invalid bounded descriptor",
    key: object = "content",
) -> UserMessage:
    """绕过 Any 内层约束，构造自由结构 attachment。"""
    message = UserMessage(text=text)
    message.attachments = [
        {
            "kind": "inline",
            "media_type": "text/plain",
            "size": 1,
            "sha256": "0" * 64,
            "encoding": "base64",
            key: value,
        }
    ]
    return message


def _descriptor_cases() -> tuple[tuple[str, UserMessage, str], ...]:
    """返回 bounded walker 必须稳定分类的输入边界。"""
    cyclic: list[object] = []
    cyclic.append(cyclic)
    return (
        (
            "huge_integer",
            _message_with_raw_value(10**400),
            "integer_out_of_range",
        ),
        (
            "long_body",
            _message_with_raw_value(
                _UnsafeValue(),
                text="SAFE_PREFIX" + "S" * 100_000 + "SECRET_BODY_SUFFIX",
            ),
            "string_too_long",
        ),
        (
            "wide_list",
            _message_with_raw_value(list(range(10_000))),
            "container_truncated",
        ),
        (
            "wide_dict",
            _message_with_raw_value({f"key-{index}": index for index in range(10_000)}),
            "container_truncated",
        ),
        ("cycle", _message_with_raw_value(cyclic), "cycle"),
        ("depth", _message_with_raw_value(_deep_list(64)), "depth_limit"),
        (
            "non_string_key",
            _message_with_raw_value("value", key=10**400),
            "non_string_mapping_key",
        ),
        (
            "long_key",
            _message_with_raw_value("value", key="K" * 100_000),
            "mapping_key_too_long",
        ),
    )


async def _wait_until(predicate: object) -> None:
    """在测试 deadline 内等待同步谓词为真。"""
    assert callable(predicate)
    with anyio.fail_after(1):
        while not predicate():
            await anyio.lowlevel.checkpoint()


@pytest.mark.parametrize(
    ("case_name", "message", "marker"),
    _descriptor_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_invalid_input_descriptor_is_total_bounded_and_deterministic(
    case_name: str,
    message: UserMessage,
    marker: str,
) -> None:
    """自由输入 descriptor 必须 bounded、deterministic 且不复制秘密。"""
    del case_name
    submission = Submission(id="sub_fixed_descriptor", op=message)
    descriptor = safe_user_message_input_descriptor(submission)
    repeated = safe_user_message_input_descriptor(submission)
    wire = canonical_bytes(descriptor)

    assert descriptor == repeated
    assert len(wire) <= 16_384
    assert marker.encode() in wire
    assert b"SECRET_BODY_SUFFIX" not in wire
    assert b"TOKEN-123" not in wire
    assert b"0xDEADBEEF" not in wire


def test_invalid_input_descriptor_never_executes_arbitrary_hooks() -> None:
    """unsupported object 只能映射为稳定 marker，不得执行用户代码。"""
    value = _ExplosiveValue()
    submission = Submission(
        id="sub_no_hooks",
        op=_message_with_raw_value(value),
    )

    first = user_message_input_descriptor_hash(submission)
    second = user_message_input_descriptor_hash(submission)

    assert first == second
    assert value.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case_name", "message", "_marker"),
    _descriptor_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
async def test_every_noncanonical_boundary_writes_exactly_one_safe_rejection(
    tmp_path: Path,
    skills_dir: Path,
    case_name: str,
    message: UserMessage,
    _marker: str,
) -> None:
    """descriptor 普通失败不得逃逸，非法输入统一 durable reject 一次。"""
    del case_name, _marker
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)

    with pytest.raises(InvalidAuditedSubmissionError) as rejected:
        await engine.submit(message)

    committed = [
        envelope async for envelope in core.load("ses_audit_submission")
    ]
    rejections = [
        envelope
        for envelope in committed
        if envelope.record_type == "submission_rejected"
    ]
    wire = canonical_bytes([envelope.payload for envelope in committed])
    assert len(rejections) == 1
    assert (
        rejections[0].payload["input_descriptor_hash"]
        == rejected.value.descriptor_hash
    )
    assert b"SECRET_BODY_SUFFIX" not in wire
    assert b"TOKEN-123" not in wire
    assert b"0xDEADBEEF" not in wire
    assert engine._submissions.empty()  # noqa: SLF001
    assert engine._history == []  # noqa: SLF001
    assert coordinator.health is AuditHealth.HEALTHY
    assert coordinator.snapshot().accepted_work_ids == ()


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
        with pytest.raises(SessionAuditFrozenError):
            await actor

    assert after_cancel.health is AuditHealth.RECOVERY_REQUIRED
    assert after_cancel.effect_gate_open is False
    assert len(after_cancel.accepted_work_ids) == 1
    assert after_cancel.accepted_work_ids == (queued_id,)
    assert coordinator.snapshot().accepted_work_ids == ()
    assert engine._history == []  # noqa: SLF001


@pytest.mark.anyio
async def test_actor_termination_wakes_full_queue_handoff_and_retires_all_tokens(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """actor 终止必须接管 blocked put 与既有 queued token，不留 hidden work。"""
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        submission_queue_size=1,
    )
    queue = _ActorGetBlockedQueue(maxsize=1)
    engine._submissions = queue  # type: ignore[assignment]  # noqa: SLF001
    await engine.submit(UserMessage(text="queued before actor"))
    root = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(root))
    await queue.get_blocked.wait()
    blocked = asyncio.create_task(
        engine.submit(UserMessage(text="blocked while actor exits"))
    )
    await queue.put_blocked.wait()

    actor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actor
    done, pending = await asyncio.wait({blocked}, timeout=1)
    if pending:
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)

    assert done, "actor finalizer lost the blocked handoff wakeup"
    with pytest.raises(SessionAuditFrozenError):
        blocked.result()
    snapshot = coordinator.snapshot()
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.effect_gate_open is False
    assert snapshot.accepted_work_ids == ()
    assert engine._history == []  # noqa: SLF001


@pytest.mark.anyio
async def test_actor_termination_after_dequeue_before_claim_retires_token(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """dequeue 后、operation claim 前终止仍必须冻结并退休 accepted work。"""
    from taifeng.llm.providers.sim import SimClient

    client = SimClient(turns=[])
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    before_claim = anyio.Event()

    async def pause_before_claim(
        sub: object,
        root_cancel: CancellationToken,
    ) -> None:
        """暴露 dequeue→claim 窗口，并让 actor cancellation 发生在其中。"""
        del sub, root_cancel
        before_claim.set()
        await anyio.sleep_forever()

    engine._start_queued_user_message = pause_before_claim  # type: ignore[method-assign]  # noqa: SLF001
    await engine.submit(UserMessage(text="dequeued but unclaimed"))
    root = CancellationToken(name="test-root")
    actor = asyncio.create_task(engine.run(root))
    await before_claim.wait()

    actor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actor

    snapshot = coordinator.snapshot()
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.effect_gate_open is False
    assert snapshot.accepted_work_ids == ()
    assert engine._history == []  # noqa: SLF001
    assert client.ledger.requests() == []


@pytest.mark.anyio
async def test_actor_termination_after_claim_before_child_start_retires_token(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """claim 后 child 零步执行时 finalizer 必须接管并唤醒 finish。"""
    from taifeng.llm.providers.sim import SimClient

    client = SimClient(turns=[])
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
        finish_timeout=0.05,
    )
    submission_id = await engine.submit(UserMessage(text="claimed but unstarted"))
    queued = engine._submissions._queue[0]  # type: ignore[attr-defined]  # noqa: SLF001
    assert isinstance(queued, AcceptedUserMessage)
    work = queued.accepted_work
    child: asyncio.Task[None] | None = None
    child_started = False
    child_start = anyio.Event()
    original_start = engine._start_operation  # noqa: SLF001

    def start_then_cancel_actor(
        coroutine: Any,
        *,
        name: str,
        submission_id: str | None = None,
    ) -> asyncio.Task[None]:
        """create_task 返回后、child 首步前同步取消当前 actor。"""
        nonlocal child, child_started

        async def hold_child_start() -> None:
            """精确阻止 run_owned 首步，并在取消时关闭未启动 coroutine。"""
            nonlocal child_started
            try:
                await child_start.wait()
                child_started = True
                await coroutine
            finally:
                if not child_started:
                    coroutine.close()

        child = original_start(hold_child_start(), name=name, submission_id=submission_id)
        actor = asyncio.current_task()
        assert actor is not None
        actor.cancel()
        return child

    engine._start_operation = start_then_cancel_actor  # type: ignore[method-assign]  # noqa: SLF001
    actor = asyncio.create_task(engine.run(CancellationToken(name="test-root")))
    with pytest.raises(asyncio.CancelledError):
        await actor

    assert child is not None
    assert child.cancelled()
    assert child_started is False
    result = await coordinator.finish(
        thread_terminals=(),
        reason="claimed_unstarted_converged",
    )
    snapshot = coordinator.snapshot()
    assert result.failure is not None
    assert result.failure.code == "accepted_work_handoff_failed"
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.accepted_work_ids == ()
    assert work.is_completed
    assert submission_id not in snapshot.accepted_work_ids
    assert engine._history == []  # noqa: SLF001
    assert client.ledger.requests() == []


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
