"""挂起与恢复的 durable 记录（session-journal，ADR 0097）。

turn 在工具调用处停下等人（审批、填表、给数据），之后由 ``Resume`` 带着答复继续：

```text
turn_suspended + conversation_item(suspension)            turn 停下，列出在等什么
resume_accepted                                           答复被接受（先于任何处置）
tool_outcome_committed + conversation_item(function_call_output)
                                                          被拒绝 / 直接作答的调用各自结算
suspension_resolved + conversation_item(system_injection) 这次挂起结清
resume_applied                                            答复已经生效
…续跑的 turn（获批的调用在其中重跑并结算）…
session_detached                                          Session 在等人的状态下被释放（不是终结）
```
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from taifeng.conversation.journal.models import NonEmptyStr  # noqa: TC001  # Pydantic 运行期需要
from taifeng.conversation.journal.records import CanonicalMapping, PayloadModel

TURN_SUSPENDED_RECORD_TYPE = "turn_suspended"
RESUME_ACCEPTED_RECORD_TYPE = "resume_accepted"
SUSPENSION_RESOLVED_RECORD_TYPE = "suspension_resolved"
RESUME_APPLIED_RECORD_TYPE = "resume_applied"
SESSION_DETACHED_RECORD_TYPE = "session_detached"

AwaitedReasonV1 = Literal["permission", "form", "data"]
"""审计模式下允许的等待原因：都是「等人对一次工具调用作答」。"""

DispositionV1 = Literal["approved", "denied", "answered"]
"""一个待答请求的处置：获批（调用将重跑）、被拒、直接作答（答复即调用结果）。"""


class AwaitedRequestV1(PayloadModel):
    """一个在等人作答的请求。

    Attributes:
        request_id: 请求 id；``Resume`` 的答复以它为键。
        reason: 等待原因。
        call_id: 所属工具调用。
        intent_record_id: 该调用已落账的意图；调用在答复之前保持未结算。
    """

    request_id: NonEmptyStr
    reason: AwaitedReasonV1
    call_id: NonEmptyStr
    intent_record_id: NonEmptyStr


class TurnSuspendedV1(PayloadModel):
    """turn 在工具调用处停下等人。

    Attributes:
        turn_index: 所属 turn。
        suspension_id: 挂起记录的 id（``Resume`` 据此核对）。
        item_id: 同批提交的 ``suspension`` 对话项的 id。
        awaited: 在等的请求，按工具调用的发起顺序。
    """

    turn_index: int = Field(ge=0)
    suspension_id: NonEmptyStr
    item_id: NonEmptyStr
    awaited: tuple[AwaitedRequestV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_distinct_requests(self) -> TurnSuspendedV1:
        """请求 id 与调用 id 都不得重复。"""
        requests = [item.request_id for item in self.awaited]
        calls = [item.call_id for item in self.awaited]
        if len(set(requests)) != len(requests) or len(set(calls)) != len(calls):
            raise ValueError("awaited requests must have distinct request ids and call ids")
        return self


class ResumeAcceptedV1(PayloadModel):
    """一次 ``Resume`` 被接受。

    Attributes:
        turn_index: 续跑的 turn 使用的序号。
        suspension_id: 答复针对的挂起。
        suspended_record_id: 那次挂起的 ``turn_suspended`` record。
        resolutions: 答复原文，请求 id → 答复。
    """

    turn_index: int = Field(ge=0)
    suspension_id: NonEmptyStr
    suspended_record_id: NonEmptyStr
    resolutions: CanonicalMapping


class ResolvedRequestV1(PayloadModel):
    """一个请求的处置。"""

    request_id: NonEmptyStr
    call_id: NonEmptyStr
    disposition: DispositionV1
    outcome_record_id: NonEmptyStr | None = None
    """被拒 / 直接作答时该调用的 ``tool_outcome_committed``；获批的调用稍后在续跑里结算，为 None。"""

    @model_validator(mode="after")
    def _require_outcome_unless_approved(self) -> ResolvedRequestV1:
        """获批的调用此刻没有结果，其余必须已经结算。"""
        if (self.disposition == "approved") != (self.outcome_record_id is None):
            raise ValueError("only approved requests may be unsettled at resolution time")
        return self


class SuspensionResolvedV1(PayloadModel):
    """一次挂起结清。

    Attributes:
        suspension_id: 被结清的挂起。
        resume_record_id: 促成结清的 ``resume_accepted`` record。
        resolved: 各请求的处置，顺序与 ``turn_suspended.awaited`` 一致。
        marker_item_id: 同批提交的结清标记对话项的 id。
    """

    suspension_id: NonEmptyStr
    resume_record_id: NonEmptyStr
    resolved: tuple[ResolvedRequestV1, ...] = Field(min_length=1)
    marker_item_id: NonEmptyStr


class ResumeAppliedV1(PayloadModel):
    """一次 ``Resume`` 的答复已经生效（或确认无处可用）。

    写在续跑的 turn **开始之前**：它回答的是「答复有没有落到挂起上」，续跑的 turn
    怎么结束与普通 turn 一样由它自己的记录说明。

    Attributes:
        accepted_record_id: 对应的 ``resume_accepted`` record。
        result_status: ``resumed`` = 挂起已结清、随后续跑；``aborted`` = 挂起已结清、
            按答复中止不再续跑；``rejected`` = 答复不适用，状态未变。
        rejection_reason: 被拒原因；仅 ``rejected`` 时有值。
    """

    accepted_record_id: NonEmptyStr
    result_status: Literal["resumed", "aborted", "rejected"]
    rejection_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _reason_matches_status(self) -> ResumeAppliedV1:
        """被拒必须说明原因，其余状态不带原因。"""
        if (self.result_status == "rejected") != (self.rejection_reason is not None):
            raise ValueError("rejection_reason is required exactly when rejected")
        return self


class SessionDetachedV1(PayloadModel):
    """Session 在等人作答的状态下被释放：写者离开，Session 没有终结，之后可以接管继续。

    Attributes:
        reason: 释放原因。
        suspension_ids: 释放时仍在等待的挂起。
    """

    reason: NonEmptyStr
    suspension_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)


__all__ = [
    "RESUME_ACCEPTED_RECORD_TYPE",
    "RESUME_APPLIED_RECORD_TYPE",
    "SESSION_DETACHED_RECORD_TYPE",
    "SUSPENSION_RESOLVED_RECORD_TYPE",
    "TURN_SUSPENDED_RECORD_TYPE",
    "AwaitedReasonV1",
    "AwaitedRequestV1",
    "DispositionV1",
    "ResolvedRequestV1",
    "ResumeAcceptedV1",
    "ResumeAppliedV1",
    "SessionDetachedV1",
    "SuspensionResolvedV1",
    "TurnSuspendedV1",
]
