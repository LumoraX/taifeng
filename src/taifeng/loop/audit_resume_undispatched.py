"""审计 Session resume 时收敛「从未登记意图」的工具调用（ADR 0075）。

崩溃窗口：模型回复（``llm_response_committed`` + ``function_call`` 会话项）已 durable，进程在
整批 ``tool_intent_committed`` 落账前死亡。此时 Journal 里没有任何未结算 effect，resume 能通过，
但 history 末尾留着没有结果的 ``function_call``——下一次请求带着不成对的调用发出去。

与 ``audit_resume_tools`` 处理的「结果未知」不同，这类调用的结局是**确定的**：strict audit 下
意图先于任何派发落账（``audit_tool.commit_intents``），没有意图即没有执行。所以不需要回查、
不看副作用声明、也不需要人裁决，恢复时直接落结论并补一条「未执行」结果。

结论写成新记录 ``tool_call_undispatched``（+ ``function_call_output`` 会话项），挂在该调用本应
使用的 tool operation 下；不改写历史。恢复从不执行工具——需要重发由模型在续跑的 turn 里自己
重调，新调用有自己的 intent / outcome。

唯一无法自动收敛的情形：call id 不能构成合法的 operation identity（空或含 ``:``）。这类调用
在 live 路径同样无法登记意图，恢复时交人（resume 拒绝并列出该 ``function_call`` record）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    ConversationItemV1,
    JournalIdentities,
    JournalRecordFactory,
    conversation_item_record,
)
from taifeng.conversation.journal.recovery_records import (
    TOOL_CALL_UNDISPATCHED_RECORD_TYPE,
    ToolCallUndispatchedV1,
)
from taifeng.conversation.models import function_call_output
from taifeng.loop.tool_recovery import (
    NOT_DISPATCHED_TEXT,
    RecoveredCall,
    stable_recovery_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord
    from taifeng.conversation.models import ResponseItem

_SYSTEM_ACTOR = ActorRef(kind="system", source="recovery")
# 与 audit_resume_tools 的恢复会话项同取 ordinal 1：ordinal 0 留给 live outcome 的会话项
_RECOVERY_ITEM_ORDINAL = 1


@dataclass(frozen=True, slots=True)
class UndispatchedCall:
    """一个已随模型回复落账、但从未登记意图的工具调用。"""

    function_call: JournalEnvelope
    call_id: str
    name: str
    arguments_raw: str
    origin_sample_id: str | None
    """Responses 路径该调用所属采样；补写的 output 须带上以保持配对。"""

    @property
    def record_id(self) -> str:
        """该调用的 ``function_call`` 会话项 record id。"""
        return self.function_call.record_id

    def tool_operation_id(self) -> str | None:
        """该调用本应使用的 tool operation id；无法构成合法 identity 时返回 None。"""
        envelope = self.function_call
        if (
            envelope.turn_id is None
            or envelope.thread_id is None
            or envelope.submission_id is None
        ):
            return None
        try:
            identities = JournalIdentities(
                envelope.session_id, envelope.thread_id, envelope.submission_id
            )
            return identities.tool(envelope.turn_id, self.call_id)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class UndispatchedRecoveryPlan:
    """一次 resume 对从未登记意图的调用的收敛计划。

    Attributes:
        records: 待原子追加的结论（+ output 会话项）记录，按模型发出调用的顺序。
        recovered: 已收敛调用的处置结论（随 ``thread_resumed`` 透出）。
        pending: 无法自动收敛、需人处置的 ``function_call`` record id。
    """

    records: tuple[JournalRecord, ...]
    recovered: tuple[RecoveredCall, ...]
    pending: tuple[str, ...]


def undispatched_call(envelope: JournalEnvelope) -> UndispatchedCall:
    """把 ``function_call`` 会话项还原成待收敛调用。

    Raises:
        pydantic.ValidationError / ValueError: 会话项 payload 形状违约（Journal 不可信）。
    """
    item = ConversationItemV1.model_validate(envelope.payload)
    call_id = item.payload.get("call_id")
    name = item.payload.get("name")
    arguments = item.payload.get("arguments")
    if not isinstance(call_id, str) or not isinstance(name, str) or not name:
        raise ValueError(f"function_call item missing call_id / name: {envelope.record_id}")
    if not isinstance(arguments, str):
        raise ValueError(f"function_call item arguments must be text: {envelope.record_id}")
    sample_id = item.metadata.get("llm_sample_id")
    return UndispatchedCall(
        function_call=envelope,
        call_id=call_id,
        name=name,
        arguments_raw=arguments,
        origin_sample_id=sample_id if isinstance(sample_id, str) and sample_id else None,
    )


def needs_operator(call: UndispatchedCall) -> bool:
    """该调用是否无法自动收敛（供只读预检使用）。"""
    return call.tool_operation_id() is None


def plan_undispatched_recovery(
    calls: Sequence[UndispatchedCall],
    *,
    session_id: str,
    recovery_operation_id: str,
) -> UndispatchedRecoveryPlan:
    """为每个从未登记意图的调用构造结论记录；纯计算，不做 IO。"""
    records: list[JournalRecord] = []
    recovered: list[RecoveredCall] = []
    pending: list[str] = []
    for call in calls:
        operation_id = call.tool_operation_id()
        if operation_id is None:
            pending.append(call.record_id)
            continue
        records.extend(_undispatched_records(
            call,
            operation_id,
            session_id=session_id,
            recovery_operation_id=recovery_operation_id,
        ))
        recovered.append(RecoveredCall(call.call_id, call.name, "not_dispatched"))
    return UndispatchedRecoveryPlan(tuple(records), tuple(recovered), tuple(pending))


def _undispatched_records(
    call: UndispatchedCall,
    operation_id: str,
    *,
    session_id: str,
    recovery_operation_id: str,
) -> tuple[JournalRecord, JournalRecord]:
    """构造 ``tool_call_undispatched`` 与补写的 output 会话项。"""
    envelope = call.function_call
    assert envelope.thread_id is not None
    assert envelope.submission_id is not None
    factory = JournalRecordFactory(
        session_id=session_id,
        actor=_SYSTEM_ACTOR,
        identities=JournalIdentities(session_id, envelope.thread_id, envelope.submission_id),
    )
    verdict = factory.build(
        operation_id=operation_id,
        record_type=TOOL_CALL_UNDISPATCHED_RECORD_TYPE,
        payload=ToolCallUndispatchedV1(
            function_call_record_id=envelope.record_id,
            call_id=call.call_id,
            name=call.name,
            arguments_raw=call.arguments_raw,
            output=NOT_DISPATCHED_TEXT,
            recovery_operation_id=recovery_operation_id,
        ),
        submission_id=envelope.submission_id,
        thread_id=envelope.thread_id,
        turn_id=envelope.turn_id,
        causation_id=envelope.record_id,
        correlation_id=recovery_operation_id,
    )
    conversation = conversation_item_record(
        factory,
        operation_id=operation_id,
        item=_output_item(call),
        source_record_id=verdict.record_id,
        ordinal=_RECOVERY_ITEM_ORDINAL,
        submission_id=envelope.submission_id,
        turn_id=envelope.turn_id,
    )
    return verdict, conversation


def _output_item(call: UndispatchedCall) -> ResponseItem:
    """补写给模型的 function_call_output（确定性 id；Responses 带采样归属）。"""
    thread_id = call.function_call.thread_id
    assert thread_id is not None
    metadata: dict[str, object] = {"recovered": True}
    if call.origin_sample_id is not None:
        metadata["origin_llm_sample_id"] = call.origin_sample_id
    return function_call_output(
        call_id=call.call_id, output=NOT_DISPATCHED_TEXT, thread_id=thread_id, is_error=True,
    ).model_copy(update={
        "id": stable_recovery_id(
            "item_recovery", thread_id, call.call_id, call.record_id
        ),
        "metadata": metadata,
    })


__all__ = [
    "UndispatchedCall",
    "UndispatchedRecoveryPlan",
    "needs_operator",
    "plan_undispatched_recovery",
    "undispatched_call",
]
