"""审计模式下的挂起与恢复：turn 停下等人、``Resume`` 带着答复继续（ADR 0097）。

```text
turn 内   工具调用停下等人 → 意图保持未结算 → turn_suspended + suspension 对话项
提交时   Resume 先过校验 → resume_accepted 落账 → 入队
处置时   被拒 / 直接作答的调用各自结算 → suspension_resolved + 结清标记
         → 续跑的 turn（获批的调用在其中重跑，结果记在原调用名下）→ resume_applied
```

只支持 root thread 上「等人对一次工具调用作答」的挂起，且一次 ``Resume`` 须答复全部待答请求。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from taifeng.conversation.journal.canonical import canonical_hash, validate_json_value
from taifeng.conversation.journal.errors import NonCanonicalValueError
from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    StableErrorV1,
    SubmissionRejectedV1,
    ToolStatus,
    record_id,
)
from taifeng.conversation.journal.suspension_records import (
    RESUME_ACCEPTED_RECORD_TYPE,
    RESUME_APPLIED_RECORD_TYPE,
    SUSPENSION_RESOLVED_RECORD_TYPE,
    TURN_SUSPENDED_RECORD_TYPE,
    AwaitedRequestV1,
    ResolvedRequestV1,
    ResumeAcceptedV1,
    ResumeAppliedV1,
    SuspensionResolvedV1,
    TurnSuspendedV1,
)
from taifeng.conversation.models import ResponseItem, system_injection
from taifeng.loop.audit_awaiting import clear_awaiting, mark_awaiting
from taifeng.loop.audit_cancel import finalize_cancelled_target
from taifeng.loop.audit_compaction import commit_record_with_item
from taifeng.loop.audit_tool import AwaitedCall, AwaitedToolConvergence
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.event import EventMsg, SuspensionResolved, SuspensionResolveRejected
from taifeng.loop.submission import Resume, Submission
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch, parse_tool_arguments
from taifeng.loop.tool_output import tool_result_cap
from taifeng.suspend.resolver import ResolveError, SuspensionResolver
from taifeng.tool.spec import ToolResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.audit_lifecycle import AcceptedWork
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine
    from taifeng.suspend.reason import PendingRequest
    from taifeng.suspend.record import SuspensionRecord

logger = logging.getLogger(__name__)

SUSPENDED_RECORD_KEY = "journal_suspended_record_id"
"""``suspension`` 对话项 metadata 里承载它的 ``turn_suspended`` record id 的键。"""

AWAITED_INTENTS_KEY = "journal_awaited_intents"
"""``suspension`` 对话项 metadata 里承载「调用 id → 意图 record id」的键。"""

_INTENT_SUFFIX = ":tool_intent_committed:none:0"


class AuditedResumeRejectedError(ValueError):
    """``Resume`` 在审计模式下被拒绝：已 durable 记录拒绝，没有执行任何处置。"""

    code = "audit_resume_rejected"

    def __init__(self, submission_id: str, reason: str) -> None:
        """
        Args:
            submission_id: 被拒的 submission。
            reason: 稳定的拒绝原因。
        """
        super().__init__(f"{self.code}: submission={submission_id}, reason={reason}")
        self.submission_id = submission_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _AcceptedResume:
    """一次已接受、尚未处置完的 ``Resume``。"""

    accepted_record_id: str
    turn_index: int
    suspension_id: str
    work: AcceptedWork


# engine → {submission id → 已接受的 Resume}
_ACCEPTED: WeakKeyDictionary[AgentEngine, dict[str, _AcceptedResume]] = WeakKeyDictionary()


def _factory(state: AuditedSessionState, submission_id: str, *, source: str) -> JournalRecordFactory:
    """绑定当前 Session / thread / submission 的 record factory。"""
    return JournalRecordFactory(
        session_id=state.coordinator.session_id,
        actor=ActorRef(kind="user" if source == "user" else "system", source=source),
        identities=JournalIdentities(
            state.coordinator.session_id, state.thread_id, submission_id
        ),
    )


# ------------------------------------------------------------------
# turn 内：挂起落账
# ------------------------------------------------------------------


async def commit_turn_suspended(
    *,
    state: AuditedSessionState,
    submission_id: str,
    turn_index: int,
    ordinal: int,
    record: SuspensionRecord,
    awaited_intents: Mapping[str, str],
    cancel: CancellationToken,
) -> ResponseItem:
    """落一次挂起：``turn_suspended`` 与 ``suspension`` 对话项同批提交；返回落账的对话项。

    Raises:
        SessionAuditFrozenError: 待答请求不属于「等人对已落账的调用作答」，或 Journal 写入不确定。
    """
    awaited: list[AwaitedRequestV1] = []
    for pending in record.pending:
        intent = awaited_intents.get(pending.related_call_id or "")
        if intent is None:
            raise state.coordinator.freeze(
                RuntimeError("audited suspension without a journaled tool intent")
            ) from None
        awaited.append(AwaitedRequestV1(
            request_id=pending.request_id,
            reason=str(pending.reason.value),  # type: ignore[arg-type]
            call_id=pending.related_call_id or "",
            intent_record_id=intent,
        ))
    identities = JournalIdentities(state.coordinator.session_id, state.thread_id, submission_id)
    operation = identities.context(identities.turn(turn_index), "suspension", ordinal)
    base = record.to_item()
    item = base.model_copy(update={"metadata": {
        **base.metadata,
        SUSPENDED_RECORD_KEY: record_id(operation, TURN_SUSPENDED_RECORD_TYPE),
        AWAITED_INTENTS_KEY: dict(awaited_intents),
    }})
    await commit_record_with_item(
        state=state, submission_id=submission_id, turn_index=turn_index,
        kind="suspension", ordinal=ordinal,
        record_type=TURN_SUSPENDED_RECORD_TYPE,
        payload=TurnSuspendedV1(
            turn_index=turn_index, suspension_id=record.record_id,
            item_id=item.id, awaited=tuple(awaited),
        ),
        item=item, cancel=cancel,
    )
    mark_awaiting(state, record.record_id)
    return item


# ------------------------------------------------------------------
# 提交时：Resume 的准入
# ------------------------------------------------------------------


def _suspension_item(engine: AgentEngine, suspension_id: str) -> ResponseItem | None:
    """history 里该挂起的对话项。"""
    for item in reversed(engine._history):  # noqa: SLF001
        if item.kind == "suspension" and item.payload.get("record_id") == suspension_id:
            return item
    return None


def _admission_rejection(engine: AgentEngine, op: Resume) -> str | None:
    """这次 ``Resume`` 不能被接受的原因；可以接受返回 None。"""
    if op.thread_id != engine._thread_id:  # noqa: SLF001
        return "resume_thread_not_root"
    record = engine._find_active_suspension()  # noqa: SLF001
    if record is None:
        return "no_active_suspension"
    if set(op.resolutions) != record.request_ids():
        return "resume_must_resolve_every_request"
    try:
        validate_json_value(dict(op.resolutions))
        SuspensionResolver().plan(record, dict(op.resolutions))
    except NonCanonicalValueError:
        return "resume_resolutions_not_canonical"
    except ResolveError as error:
        return str(error).split(":", 1)[0]
    item = _suspension_item(engine, record.record_id)
    if item is None or SUSPENDED_RECORD_KEY not in item.metadata:
        return "suspension_not_journaled"
    return None


async def _reject(state: AuditedSessionState, submission: Submission, reason: str) -> None:
    """durable 记录一次被拒的 ``Resume``；不保存答复原文。"""
    record = _factory(state, submission.id, source="user").build(
        operation_id=submission.id,
        record_type="submission_rejected",
        payload=SubmissionRejectedV1(
            op_kind="resume",
            stable_error=StableErrorV1(
                code=reason, class_name="AuditedResumeRejectedError",
                failure_class="input_validation", retryable=False,
            ),
            input_descriptor_hash=canonical_hash({"op_kind": "resume", "reason": reason}),
        ),
        submission_id=submission.id,
        thread_id=state.thread_id,
    )

    async def durable_reject() -> None:
        try:
            await state.coordinator.append(record)
        except BaseException as error:
            state.coordinator.freeze(error)
            raise

    await state.coordinator.reject_work(submission.id, durable_reject)


async def submit_audited_resume(engine: AgentEngine, submission: Submission) -> str:
    """审计模式下提交 ``Resume``：校验 → ``resume_accepted`` 落账 → 入队。

    Raises:
        AuditedResumeRejectedError: 答复不适用（已 durable 记录拒绝）。
        SessionAuditFrozenError / SessionFinishingError: Session 不再接受输入。
    """
    assert isinstance(submission.op, Resume)
    state = engine._audit_state  # noqa: SLF001
    assert state is not None
    state.coordinator.ensure_intake_open()
    async with engine._audited_admission_lock:  # noqa: SLF001
        reason = _admission_rejection(engine, submission.op)
        if reason is not None:
            await _reject(state, submission, reason)
            raise AuditedResumeRejectedError(submission.id, reason)
        record = engine._find_active_suspension()  # noqa: SLF001
        assert record is not None
        item = _suspension_item(engine, record.record_id)
        assert item is not None
        turn_index = engine._next_audited_turn_index  # noqa: SLF001
        accepted = _factory(state, submission.id, source="user").build(
            operation_id=submission.id,
            record_type=RESUME_ACCEPTED_RECORD_TYPE,
            payload=ResumeAcceptedV1(
                turn_index=turn_index,
                suspension_id=record.record_id,
                suspended_record_id=str(item.metadata[SUSPENDED_RECORD_KEY]),
                resolutions=dict(submission.op.resolutions),
            ),
            submission_id=submission.id,
            thread_id=state.thread_id,
        )

        async def durable_accept() -> None:
            await state.coordinator.append(accepted)

        work = await state.coordinator.admit_work(submission.id, durable_accept)
        engine._next_audited_turn_index = turn_index + 1  # noqa: SLF001
        _ACCEPTED.setdefault(engine, {})[submission.id] = _AcceptedResume(
            accepted_record_id=accepted.record_id, turn_index=turn_index,
            suspension_id=record.record_id, work=work,
        )
    try:
        await engine._submissions.put(submission)  # noqa: SLF001
    except BaseException:
        _ACCEPTED[engine].pop(submission.id, None)
        await work.complete()
        raise
    return submission.id


# ------------------------------------------------------------------
# 处置时：结算、结清、续跑
# ------------------------------------------------------------------


def _intents(item: ResponseItem) -> dict[str, str]:
    """挂起对话项记下的「调用 id → 意图 record id」。"""
    raw = item.metadata.get(AWAITED_INTENTS_KEY)
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _request_for(engine_history: list[ResponseItem], call_id: str) -> ToolCallRequest:
    """由 history 里的 function_call 还原一次调用。"""
    call = next(
        (
            item for item in reversed(engine_history)
            if item.kind == "function_call" and item.payload.get("call_id") == call_id
        ),
        None,
    )
    if call is None:
        raise RuntimeError(f"awaited_call_not_found: {call_id}")
    raw = call.payload.get("arguments") or "{}"
    arguments, error = parse_tool_arguments(raw)
    return ToolCallRequest(
        index=0, call_id=call_id, name=str(call.payload["name"]),
        arguments=arguments, arguments_raw=raw, parallel_safe=False,
        arguments_error=error,
    )


def awaited_convergence(
    state: AuditedSessionState,
    intents: Mapping[str, str],
    requests: list[ToolCallRequest],
    *,
    registry: object,
    cancel: CancellationToken,
    image_input_policy: Any = None,
) -> AwaitedToolConvergence:
    """为一批等过人的调用构造结算器：结果记在它们原来的调用名下。"""
    first = intents[requests[0].call_id]
    operation = first.removesuffix(_INTENT_SUFFIX)
    parts = operation.split(":")
    if not first.endswith(_INTENT_SUFFIX) or len(parts) != 6 or parts[0] != state.thread_id:
        raise state.coordinator.freeze(
            RuntimeError("awaited tool intent does not belong to this thread")
        ) from None
    kwargs: dict[str, Any] = {}
    if image_input_policy is not None:
        kwargs["image_input_policy"] = image_input_policy
    return AwaitedToolConvergence(
        state=state, submission_id=parts[1], turn_index=int(parts[3]), iteration=0,
        requests=requests, registry=registry, cancel=cancel,
        intent_ids={request.call_id: intents[request.call_id] for request in requests},
        **kwargs,
    )


async def _settle_answered(
    engine: AgentEngine,
    state: AuditedSessionState,
    record: SuspensionRecord,
    plan: Any,
    intents: Mapping[str, str],
    cancel: CancellationToken,
) -> dict[str, tuple[str, str]]:
    """结算被拒 / 直接作答的调用；返回 调用 id → (处置, outcome record id)。"""
    answers: list[tuple[str, str, ToolResult, ToolStatus]] = []
    for call_id, payload in plan.direct_outputs.items():
        answers.append((call_id, "answered", ToolResult.ok(
            json.dumps(payload, ensure_ascii=False)), ToolStatus.SUCCESS))
    for call_id, (text, is_error) in plan.provided_outputs.items():
        result = ToolResult.error(text, reason="provided") if is_error else ToolResult.ok(text)
        answers.append((
            call_id, "answered", result,
            ToolStatus.ERROR if is_error else ToolStatus.SUCCESS,
        ))
    for call_id, reason in plan.deny_outputs.items():
        text = engine._deny_output_text(record, call_id, reason)  # noqa: SLF001
        answers.append((
            call_id, "denied", ToolResult.error(text, reason="permission_denied"),
            ToolStatus.REJECTED,
        ))
    settled: dict[str, tuple[str, str]] = {}
    history = list(engine._history)  # noqa: SLF001
    already = {
        str(item.payload.get("call_id"))
        for item in history if item.kind == "function_call_output"
    }
    for call_id, disposition, result, status in answers:
        request = _request_for(history, call_id)
        convergence = awaited_convergence(
            state, intents, [request],
            registry=engine._tool_runtime._registry, cancel=cancel,  # noqa: SLF001
        )
        if call_id in already:
            # 上一次处置已结算过这次调用（处置到一半中断）：结果不可重写，沿用已有的
            settled[call_id] = (disposition, convergence.outcome_record_id(request))
            continue
        item, outcome_id = await convergence.settle(request, result, status)
        async with engine._lock:  # noqa: SLF001
            engine._history.append(item)  # noqa: SLF001
        settled[call_id] = (disposition, outcome_id)
    return settled


async def _commit_resolved(
    engine: AgentEngine,
    state: AuditedSessionState,
    submission_id: str,
    accepted: _AcceptedResume,
    record: SuspensionRecord,
    settled: Mapping[str, tuple[str, str]],
    cancel: CancellationToken,
) -> None:
    """落结清记录与结清标记，标记进 hot history。"""
    marker = system_injection(
        text=f"suspend_resolved:{record.record_id}",
        thread_id=state.thread_id, source="suspend_resolved",
    )
    resolved = tuple(
        ResolvedRequestV1(
            request_id=pending.request_id,
            call_id=pending.related_call_id or "",
            disposition=settled.get(pending.related_call_id or "", ("approved", ""))[0],  # type: ignore[arg-type]
            outcome_record_id=settled.get(pending.related_call_id or "", ("", None))[1] or None,
        )
        for pending in record.pending
    )
    await commit_record_with_item(
        state=state, submission_id=submission_id, turn_index=accepted.turn_index,
        kind="suspension", ordinal=0,
        record_type=SUSPENSION_RESOLVED_RECORD_TYPE,
        payload=SuspensionResolvedV1(
            suspension_id=record.record_id,
            resume_record_id=accepted.accepted_record_id,
            resolved=resolved, marker_item_id=marker.id,
        ),
        item=marker, cancel=cancel,
    )
    clear_awaiting(state, record.record_id)
    async with engine._lock:  # noqa: SLF001
        engine._history.append(marker)  # noqa: SLF001


async def _commit_applied(
    state: AuditedSessionState,
    submission_id: str,
    accepted: _AcceptedResume,
    *,
    result_status: str,
    rejection_reason: str | None = None,
) -> None:
    """落 ``resume_applied``：答复已生效（或确认无处可用），写在续跑之前。"""
    record = _factory(state, submission_id, source="engine").build(
        operation_id=submission_id,
        record_type=RESUME_APPLIED_RECORD_TYPE,
        payload=ResumeAppliedV1(
            accepted_record_id=accepted.accepted_record_id,
            result_status=result_status,  # type: ignore[arg-type]
            rejection_reason=rejection_reason,
        ),
        submission_id=submission_id,
        thread_id=state.thread_id,
        causation_id=accepted.accepted_record_id,
    )
    await state.coordinator.append(record)


async def run_audited_resume(
    engine: AgentEngine, sub: Submission, root_cancel: CancellationToken,
) -> None:
    """处置一次已接受的 ``Resume``：结算、结清、落 ``resume_applied``，然后续跑。"""
    assert isinstance(sub.op, Resume)
    state = engine._audit_state  # noqa: SLF001
    assert state is not None
    accepted = _ACCEPTED.get(engine, {}).pop(sub.id, None)
    if accepted is None:
        # 没有经过准入的 Resume 不处置（入队前必有 resume_accepted）
        raise state.coordinator.freeze(
            RuntimeError("audited resume reached the actor without admission")
        ) from None
    try:
        await _drive_resume(engine, state, sub, accepted, root_cancel)
    finally:
        await accepted.work.complete()


async def _drive_resume(
    engine: AgentEngine,
    state: AuditedSessionState,
    sub: Submission,
    accepted: _AcceptedResume,
    root_cancel: CancellationToken,
) -> None:
    """结算 → 结清 → ``resume_applied`` → 续跑。

    续跑的 turn 发出终态事件之后不再写任何记录：调用方看到终态即可安全释放 Session。
    """
    assert isinstance(sub.op, Resume)
    record = engine._find_active_suspension()  # noqa: SLF001
    if record is None or record.record_id != accepted.suspension_id:
        # 准入之后、处置之前挂起已被别的途径结清（取消）：答复无处可用
        await _commit_applied(
            state, sub.id, accepted,
            result_status="rejected", rejection_reason="no_active_suspension",
        )
        await engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(  # noqa: SLF001
            data={"reason": "no_active_suspension", "record_id": accepted.suspension_id,
                  "detail": {}})))
        return
    item = _suspension_item(engine, record.record_id)
    assert item is not None
    intents = _intents(item)
    plan = SuspensionResolver().plan(record, dict(sub.op.resolutions))
    turn_cancel = root_cancel.child(f"sub:{sub.id}")
    settled = await _settle_answered(engine, state, record, plan, intents, turn_cancel)
    policy = engine._permission_policy  # noqa: SLF001
    for call_id in plan.execute_tool_call_ids:
        # 批准留给续跑 turn 里的重跑消费
        if policy is not None:
            policy.preapprove(call_id)
    await _commit_resolved(engine, state, sub.id, accepted, record, settled, turn_cancel)
    await _commit_applied(
        state, sub.id, accepted, result_status="aborted" if plan.abort else "resumed",
    )
    await engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(  # noqa: SLF001
        data={"record_id": record.record_id, "request_ids": sorted(record.request_ids())})))
    if plan.abort:
        return
    seeds = tuple(plan.execute_tool_call_ids)
    # 续跑的 turn 登记为可取消的 target：CancelTurn 指向这次 Resume 即可取消它
    target_cancel = state.coordinator.register_target(sub.id)
    try:
        engine._pending[sub.id] = _PendingTurn(  # noqa: SLF001
            sub.id, target_cancel, accepted.turn_index,
        )
        await engine._build_and_run_runner(  # noqa: SLF001
            sub.id, target_cancel, list(engine._last_resolved or []),  # noqa: SLF001
            seed_pending_call_id=seeds[0] if seeds else None,
            extra_seed_call_ids=seeds[1:],
        )
        outcome = state.coordinator.target_outcome(sub.id, target_cancel)
        if outcome == "cancelled" and state.coordinator.target_cancel_requested(
            sub.id, target_cancel,
        ):
            await finalize_cancelled_target(
                state, submission_id=sub.id, turn_index=accepted.turn_index,
                target_token=target_cancel,
            )
    finally:
        engine._pending.pop(sub.id, None)  # noqa: SLF001
        state.coordinator.unregister_target(sub.id, target_cancel)


# ------------------------------------------------------------------
# 续跑的 turn 内：获批的调用重跑
# ------------------------------------------------------------------


async def rerun_awaited_calls(runner: Any, call_ids: list[str]) -> None:
    """在续跑的 turn 里重跑获批的调用，结果记在它们原来的调用名下并进 hot history。

    Raises:
        _BatchSuspend: 重跑又停下等人（下一道审批）。
    """
    state = runner.audit_state
    intents: dict[str, str] = {}
    for item in runner.history_buffer:
        if item.kind == "suspension":
            intents.update(_intents(item))
    history = list(runner.history_buffer)
    requests = []
    for index, call_id in enumerate(call_ids):
        request = _request_for(history, call_id)
        requests.append(ToolCallRequest(
            index=index, call_id=request.call_id, name=request.name,
            arguments=request.arguments, arguments_raw=request.arguments_raw,
            parallel_safe=False, arguments_error=request.arguments_error,
        ))
    convergence = awaited_convergence(
        state, intents, requests,
        registry=runner.tool_runtime._registry, cancel=runner.cancel,  # noqa: SLF001
        image_input_policy=runner.image_input_policy,
    )
    import asyncio

    outcomes = await dispatch_batch(
        requests, runtime=runner.tool_runtime,
        ctx_for=lambda cid: runner._build_tool_context(cid, 0),  # noqa: SLF001
        hooks=runner.hooks, emit=runner._emit,  # noqa: SLF001
        semaphore=asyncio.Semaphore(1),
        thread_id=runner.thread_id, submission_id=runner.submission_id,
        entry_skill_id=runner.entry_skill.id,
        visible_tools=runner.entry_skill.visible_tool_names(),
        registry=runner.tool_runtime._registry,  # noqa: SLF001
        result_cap_bytes=tool_result_cap(runner.budget, runner.compressors),
    )
    runner.history_buffer.extend(await convergence.converge(outcomes))
    if not convergence.awaited:
        return
    awaited: list[AwaitedCall] = list(convergence.awaited)
    runner._persist.awaited_intents = {  # noqa: SLF001
        call.pending.related_call_id: call.intent_record_id for call in awaited
    }
    from taifeng.loop import turn as _turn_mod

    raise _turn_mod._BatchSuspend(tuple(call.pending for call in awaited))  # noqa: SLF001


def awaited_call_ids(pending: tuple[PendingRequest, ...]) -> list[str]:
    """一组待答请求涉及的调用 id。"""
    return [item.related_call_id for item in pending if item.related_call_id]


__all__ = [
    "AWAITED_INTENTS_KEY",
    "SUSPENDED_RECORD_KEY",
    "AuditedResumeRejectedError",
    "awaited_call_ids",
    "awaited_convergence",
    "commit_turn_suspended",
    "rerun_awaited_calls",
    "run_audited_resume",
    "submit_audited_resume",
]
