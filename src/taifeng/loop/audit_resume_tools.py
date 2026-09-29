"""审计 Session resume 时收敛「结果未知」的工具调用（ADR 0070）。

把 tool-crash-reconciliation（ADR 0045）的按副作用分流接进 strict audit 冷恢复。待收敛的调用有两类：
悬空 ``tool_intent_committed``（崩溃在执行途中，模型看不到任何结果），以及已 durable 为
``unknown`` 的 ``tool_outcome_committed``（取消 / 超时判不清，模型已看到一条错误结果，Session
当时即冻结）。只处理 root thread 的调用——子 thread 的调用必然伴随未结算的 skill 派发，仍整体
fail closed。

| 条件（按序判定） | 结论 | 随 batch 补写给模型的 output |
| --- | --- | --- |
| 有 ``reconcile``，回查 ``completed``（仅悬空 intent） | reconcile / completed | 真实结果 |
| 有 ``reconcile``，回查 ``not_executed`` | reconcile / not_executed | 「未执行，可安全重发」（已有结果时不补） |
| 无 ``reconcile``，悬空 intent，落账声明与当前注册声明都属 pure / idempotent | effect_kind / retry_safe | 「可安全重发」 |
| 其余，``AuditConfig.tool_outcome_resolver`` 给出裁决 | operator / provided 或 aborted | 人给的结果 / 放弃文案（已有结果时只允许 aborted） |
| 其余 | 不写任何记录 | resume 拒绝并列出这些 record |

已有 durable 结果的调用不能补第二条 ``function_call_output``（会破坏配对，且不改写历史），因此
回查 ``completed`` 或人 ``provide`` 都无法纠正模型已看到的错误结果——这两种情况仍交人，人只能选择
接受未知（``abort``）。

「交人」的表达：审计会话不能挂起（capability gate 拒绝 HITL / suspension），所以沿用 ADR 0053
的既有语义——resume 以 ``audit_resume_recovery_required`` 拒绝并列出需裁决的 record；人的裁决经
resolver 在下次 resume 接管时提交，并以 ``operator`` actor durable 落账。恢复从不执行工具，也不支持
``retry`` 裁决：审计模式的 effect 只能发生在有 durable intent 的 turn 内。

回查与 resolver 只在持有写者锁之后调用：锁证明原 writer 已退出，回查不会与仍在运行的原调用赛跑。

参照：ADR 0025 恢复语义（仅幂等或 reconciler 证明后才重试 / 补写）；差异：结论写成新的 Journal
记录而非 transcript 回填，审计链可复核依据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    ConversationItemV1,
    JournalIdentities,
    JournalRecordFactory,
    ToolIntentCommittedV1,
    ToolOutcomeCommittedV1,
    conversation_item_record,
)
from taifeng.conversation.journal.recovery_records import (
    TOOL_RECOVERY_RECORD_TYPE,
    ToolRecoveryCommittedV1,
)
from taifeng.conversation.models import function_call_output
from taifeng.loop.tool_recovery import (
    NOT_EXECUTED_TEXT,
    OPERATOR_ABORTED_TEXT,
    RETRY_SAFE_EFFECTS,
    RecoveredCall,
    run_reconcile,
    safe_to_retry_text,
    stable_recovery_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord
    from taifeng.conversation.journal.recovery_records import (
        ReconcileStatus,
        RecoveryBasis,
        RecoveryVerdict,
    )
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.tool_recovery import Disposition
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.spec import ReconcileVerdict, ToolSpec

_SYSTEM_ACTOR = ActorRef(kind="system", source="recovery")
# 恢复补写的 function_call_output 与 live outcome 的会话项（ordinal 0）共用 tool operation，
# 取 ordinal 1 使两者 record id 永不相撞
_RECOVERY_ITEM_ORDINAL = 1


@dataclass(frozen=True, slots=True)
class AuditToolOutcomeRequest:
    """交人裁决的一次结果未知调用（resolver 的输入）。

    Attributes:
        record_id: 待裁决的 record（悬空 intent，或已 durable 为 unknown 的 outcome）。
        has_recorded_output: True 表示模型已看到该调用的一条结果——只能 ``abort``。
        reconcile_status: 回查函数的原始结论；没有回查函数时为 None。
    """

    session_id: str
    thread_id: str
    record_id: str
    intent_record_id: str
    call_id: str
    name: str
    arguments_raw: str
    effect_kind: str
    reconciliation: str
    has_recorded_output: bool
    reconcile_status: ReconcileStatus | None


@dataclass(frozen=True, slots=True)
class AuditToolOutcomeResolution:
    """人对一次结果未知调用的裁决。

    Attributes:
        action: ``provide`` = 给出真实结果（``output`` / ``is_error`` 原样补写给模型）；
            ``abort`` = 放弃查明、接受未知并继续。
        operator_id: 裁决人身份，落账为 actor ``principal_id``（审计追溯必需）。
    """

    action: Literal["provide", "abort"]
    operator_id: str
    output: str = ""
    is_error: bool = False

    def __post_init__(self) -> None:
        """构造期拒绝非法裁决（显式报错，不静默改判）。

        Raises:
            ValueError: action 非 provide / abort（含 ``retry``：恢复不执行工具）、operator_id
                为空、output / is_error 类型不对。
        """
        if self.action not in ("provide", "abort"):
            raise ValueError(
                f"audit tool resolution action must be 'provide' or 'abort', got {self.action!r}"
                " (retry is unsupported: audited effects run only inside a turn)"
            )
        if type(self.operator_id) is not str or not self.operator_id:
            raise ValueError("audit tool resolution requires a non-empty operator_id")
        if type(self.output) is not str or type(self.is_error) is not bool:
            raise ValueError("audit tool resolution output must be str and is_error bool")


type AuditToolOutcomeResolver = Callable[
    [AuditToolOutcomeRequest], Awaitable[AuditToolOutcomeResolution | None]
]
"""resume 时向人征求裁决的回调；返回 None = 尚无裁决（resume 仍拒绝）。异常原样上抛。"""


class AuditToolResolutionError(ValueError):
    """resolver 给出的裁决不适用于该调用（如对已有结果的调用 ``provide``）。"""

    def __init__(self, record_id: str, reason: str) -> None:
        """记录违约的 record 与原因。"""
        super().__init__(f"{reason}: {record_id}")
        self.record_id = record_id


@dataclass(frozen=True, slots=True)
class UnresolvedToolCall:
    """一个待收敛的 root thread 工具调用。"""

    intent: JournalEnvelope
    intent_payload: ToolIntentCommittedV1
    outcome_record_id: str | None
    """已 durable 为 unknown 的 outcome；悬空 intent 为 None。"""
    origin_sample_id: str | None
    """Responses 路径该调用所属采样；补写的 output 须带上以保持配对。"""

    @property
    def record_id(self) -> str:
        """需要结算的 record：有 unknown outcome 时是它，否则是 intent。"""
        return self.outcome_record_id or self.intent.record_id

    def arguments(self) -> dict[str, Any]:
        """intent 落账的生效参数，解冻为普通容器交给业务回查函数。"""
        thawed = json.loads(json.dumps(self.intent_payload.effective_arguments))
        assert isinstance(thawed, dict)
        return thawed


@dataclass(frozen=True, slots=True)
class AuditToolRecoveryPlan:
    """一次 resume 的工具收敛计划。

    Attributes:
        records: 待原子追加的 recovery（+ output 会话项）记录，按 Journal seq 顺序。
        recovered: 已收敛调用的处置结论（随 ``thread_resumed`` 透出）。
        pending: 仍需人裁决的 record id；非空时整批不写。
    """

    records: tuple[JournalRecord, ...]
    recovered: tuple[RecoveredCall, ...]
    pending: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Decision:
    """一个调用的收敛结论。"""

    basis: RecoveryBasis
    verdict: RecoveryVerdict
    output: str | None
    is_error: bool | None
    reconcile_status: ReconcileStatus | None
    operator_id: str | None = None

    @property
    def disposition(self) -> Disposition:
        """映射到 ``thread_resumed.recovered_tool_calls`` 的处置取值。"""
        if self.basis == "operator":
            return "operator_resolved"
        return "reconciled" if self.verdict == "completed" else "safe_to_retry"


def split_unsettled(
    envelopes: Sequence[JournalEnvelope],
    pending: Sequence[str],
    root_thread_id: str,
) -> tuple[tuple[UnresolvedToolCall, ...], tuple[str, ...]]:
    """把未结算 record 分成可按工具恢复收敛的调用与其余（仍一律 fail closed）。

    Raises:
        pydantic.ValidationError: 工具 intent / outcome payload 形状违约（Journal 不可信）。
    """
    by_id = {envelope.record_id: envelope for envelope in envelopes}
    samples = _function_call_samples(envelopes, root_thread_id)
    calls: list[UnresolvedToolCall] = []
    others: list[str] = []
    for record_id in pending:
        call = _tool_call_for(by_id[record_id], by_id, samples, root_thread_id)
        if call is None:
            others.append(record_id)
        else:
            calls.append(call)
    return tuple(calls), tuple(others)


def _tool_call_for(
    envelope: JournalEnvelope,
    by_id: Mapping[str, JournalEnvelope],
    samples: Mapping[str, str],
    root_thread_id: str,
) -> UnresolvedToolCall | None:
    """未结算 record 若是 root thread 的工具 intent / unknown outcome，还原成待收敛调用。"""
    if envelope.record_type == "tool_intent_committed":
        intent: JournalEnvelope | None = envelope
        outcome_record_id: str | None = None
    elif envelope.record_type == "tool_outcome_committed":
        outcome = ToolOutcomeCommittedV1.model_validate(envelope.payload)
        intent = by_id.get(outcome.intent_record_id)
        outcome_record_id = envelope.record_id
    else:
        return None
    # 引用缺失 / 非 root thread / 缺 operation lineage 的调用无法安全落账结论，留给人
    if (
        intent is None
        or intent.record_type != "tool_intent_committed"
        or intent.thread_id != root_thread_id
        or intent.operation_id is None
    ):
        return None
    payload = ToolIntentCommittedV1.model_validate(intent.payload)
    return UnresolvedToolCall(
        intent=intent,
        intent_payload=payload,
        outcome_record_id=outcome_record_id,
        origin_sample_id=samples.get(payload.call_id),
    )


def _function_call_samples(
    envelopes: Sequence[JournalEnvelope], root_thread_id: str,
) -> dict[str, str]:
    """root thread 已提交 function_call 会话项的 call_id → Responses 采样 id。"""
    samples: dict[str, str] = {}
    for envelope in envelopes:
        if (
            envelope.record_type != "conversation_item"
            or envelope.thread_id != root_thread_id
            or envelope.payload.get("item_kind") != "function_call"
        ):
            continue
        item = ConversationItemV1.model_validate(envelope.payload)
        sample_id = item.metadata.get("llm_sample_id")
        call_id = item.payload.get("call_id")
        if isinstance(sample_id, str) and sample_id and isinstance(call_id, str):
            samples[call_id] = sample_id
    return samples


def _retry_safe(call: UnresolvedToolCall, spec: ToolSpec | None) -> bool:
    """悬空调用可仅凭副作用声明判「可安全重发」：落账声明与当前注册声明都须属安全集合。"""
    return (
        call.outcome_record_id is None
        and spec is not None
        and call.intent_payload.effect_kind in RETRY_SAFE_EFFECTS
        and spec.effect_kind in RETRY_SAFE_EFFECTS
    )


def needs_operator_without_lock(
    call: UnresolvedToolCall,
    registry: ToolRegistry,
    resolver: AuditToolOutcomeResolver | None,
) -> bool:
    """不持锁、不回查即可断定只能交人：无回查函数、不可安全重发、且未配置 resolver。

    供只读预检使用：注定被拒的 resume 不写接管记录。
    """
    if resolver is not None:
        return False
    spec = registry.get(call.intent_payload.name)
    if spec is not None and spec.reconcile is not None:
        return False
    return not _retry_safe(call, spec)


async def plan_audited_tool_recovery(
    calls: Sequence[UnresolvedToolCall],
    *,
    registry: ToolRegistry,
    resolver: AuditToolOutcomeResolver | None,
    session_id: str,
    recovery_operation_id: str,
) -> AuditToolRecoveryPlan:
    """逐个调用按分流规则给出结论并构造待落账记录（调用方须已持有写者锁）。

    全部调用都得出结论才有意义落账；任一仍需人裁决时调用方整批不写。

    Raises:
        AuditToolResolutionError: resolver 返回的裁决不适用于该调用。
        Exception: resolver 自身抛出的异常原样上抛。
    """
    records: list[JournalRecord] = []
    recovered: list[RecoveredCall] = []
    pending: list[str] = []
    for call in calls:
        spec = registry.get(call.intent_payload.name)
        decision = await _decide(call, spec, resolver, session_id=session_id)
        if decision is None:
            pending.append(call.record_id)
            continue
        records.extend(_recovery_records(
            call, decision, session_id=session_id, recovery_operation_id=recovery_operation_id,
        ))
        recovered.append(RecoveredCall(
            call.intent_payload.call_id, call.intent_payload.name, decision.disposition,
        ))
    return AuditToolRecoveryPlan(tuple(records), tuple(recovered), tuple(pending))


async def _decide(
    call: UnresolvedToolCall,
    spec: ToolSpec | None,
    resolver: AuditToolOutcomeResolver | None,
    *,
    session_id: str,
) -> _Decision | None:
    """按 ADR 0045 顺序分流：回查 → 副作用声明 → 人；都给不出结论返回 None。"""
    status: ReconcileStatus | None = None
    if spec is not None and spec.reconcile is not None:
        verdict = await run_reconcile(spec, call.arguments(), call.intent_payload.call_id)
        status = "failed" if verdict is None else verdict.status
        automatic = _reconciled_decision(call, verdict)
        if automatic is not None:
            return automatic
    elif _retry_safe(call, spec):
        effect = call.intent_payload.effect_kind
        return _Decision("effect_kind", "retry_safe", safe_to_retry_text(effect), True, None)
    if resolver is None:
        return None
    resolution = await resolver(_operator_request(call, session_id, status))
    if resolution is None:
        return None
    return _operator_decision(call, resolution, status)


def _reconciled_decision(
    call: UnresolvedToolCall, verdict: ReconcileVerdict | None,
) -> _Decision | None:
    """把回查结论映射为自动收敛；查不清或无法向模型纠正时返回 None（交人）。"""
    if verdict is None:
        return None
    dangling = call.outcome_record_id is None
    if verdict.status == "not_executed":
        # 已有结果的调用：模型看到的错误结果与「未执行」一致，只落结论不补写
        text = NOT_EXECUTED_TEXT if dangling else None
        return _Decision("reconcile", "not_executed", text, True if dangling else None,
                         "not_executed")
    if verdict.status == "completed" and dangling:
        return _Decision("reconcile", "completed", verdict.output, verdict.is_error, "completed")
    return None


def _operator_request(
    call: UnresolvedToolCall, session_id: str, status: ReconcileStatus | None,
) -> AuditToolOutcomeRequest:
    """构造交给 resolver 的裁决请求。"""
    payload = call.intent_payload
    assert call.intent.thread_id is not None
    return AuditToolOutcomeRequest(
        session_id=session_id,
        thread_id=call.intent.thread_id,
        record_id=call.record_id,
        intent_record_id=call.intent.record_id,
        call_id=payload.call_id,
        name=payload.name,
        arguments_raw=payload.arguments_raw,
        effect_kind=payload.effect_kind,
        reconciliation=payload.reconciliation,
        has_recorded_output=call.outcome_record_id is not None,
        reconcile_status=status,
    )


def _operator_decision(
    call: UnresolvedToolCall, resolution: object, status: ReconcileStatus | None,
) -> _Decision:
    """校验 resolver 的裁决并映射为结论。

    Raises:
        AuditToolResolutionError: 返回值类型不对，或对已有结果的调用 ``provide``。
    """
    if type(resolution) is not AuditToolOutcomeResolution:
        raise AuditToolResolutionError(
            call.record_id, "resolver must return AuditToolOutcomeResolution or None"
        )
    dangling = call.outcome_record_id is None
    if resolution.action == "provide":
        if not dangling:
            raise AuditToolResolutionError(
                call.record_id, "provide cannot replace an output the model already saw"
            )
        return _Decision("operator", "provided", resolution.output, resolution.is_error,
                         status, resolution.operator_id)
    text = OPERATOR_ABORTED_TEXT if dangling else None
    return _Decision("operator", "aborted", text, True if dangling else None,
                     status, resolution.operator_id)


def _recovery_records(
    call: UnresolvedToolCall,
    decision: _Decision,
    *,
    session_id: str,
    recovery_operation_id: str,
) -> tuple[JournalRecord, ...]:
    """构造 ``tool_recovery_committed``（+ 需要时的 output 会话项），挂在原 tool operation 下。"""
    intent = call.intent
    operation_id = intent.operation_id
    assert operation_id is not None
    # tool operation 形如 {thread}:{submission}:turn:{i}:tool:{call_id}（record_id 会再校验）
    thread_part, submission_part = operation_id.split(":")[:2]
    actor = (
        _SYSTEM_ACTOR if decision.operator_id is None
        else ActorRef(kind="operator", source="recovery", principal_id=decision.operator_id)
    )
    factory = JournalRecordFactory(
        session_id=session_id,
        actor=actor,
        identities=JournalIdentities(session_id, thread_part, submission_part),
    )
    payload = call.intent_payload
    recovery = factory.build(
        operation_id=operation_id,
        record_type=TOOL_RECOVERY_RECORD_TYPE,
        payload=ToolRecoveryCommittedV1(
            intent_record_id=intent.record_id,
            outcome_record_id=call.outcome_record_id,
            call_id=payload.call_id,
            name=payload.name,
            effect_kind=payload.effect_kind,
            basis=decision.basis,
            verdict=decision.verdict,
            reconcile_status=decision.reconcile_status,
            output=decision.output,
            is_error=decision.is_error,
            recovery_operation_id=recovery_operation_id,
        ),
        submission_id=intent.submission_id,
        thread_id=intent.thread_id,
        turn_id=intent.turn_id,
        causation_id=call.record_id,
        correlation_id=recovery_operation_id,
    )
    if decision.output is None:
        return (recovery,)
    item = _recovery_output_item(call, decision.output, bool(decision.is_error))
    conversation = conversation_item_record(
        factory,
        operation_id=operation_id,
        item=item,
        source_record_id=recovery.record_id,
        ordinal=_RECOVERY_ITEM_ORDINAL,
        submission_id=intent.submission_id,
        turn_id=intent.turn_id,
    )
    return recovery, conversation


def _recovery_output_item(call: UnresolvedToolCall, output: str, is_error: bool) -> ResponseItem:
    """补写给模型的 function_call_output（确定性 id；Responses 带采样归属）。"""
    thread_id = call.intent.thread_id
    assert thread_id is not None
    call_id = call.intent_payload.call_id
    metadata: dict[str, object] = {"recovered": True}
    if call.origin_sample_id is not None:
        metadata["origin_llm_sample_id"] = call.origin_sample_id
    return function_call_output(
        call_id=call_id, output=output, thread_id=thread_id, is_error=is_error,
    ).model_copy(update={
        "id": stable_recovery_id("item_recovery", thread_id, call_id),
        "metadata": metadata,
    })


__all__ = [
    "AuditToolOutcomeRequest",
    "AuditToolOutcomeResolution",
    "AuditToolOutcomeResolver",
    "AuditToolRecoveryPlan",
    "AuditToolResolutionError",
    "UnresolvedToolCall",
    "needs_operator_without_lock",
    "plan_audited_tool_recovery",
    "split_unsettled",
]
