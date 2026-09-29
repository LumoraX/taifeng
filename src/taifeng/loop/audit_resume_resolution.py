"""审计 resume 向人征求工具调用裁决的公开契约（ADR 0070）。

审计会话不能挂起，「交人」表达为 resume 拒绝并列出 record；人的裁决经
``AuditConfig.tool_outcome_resolver`` 在下次 resume 接管时提交。本模块只放这组公开 DTO 与回调类型，
分流与落账逻辑在 ``audit_resume_tools``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.conversation.journal.recovery_records import ReconcileStatus


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


__all__ = [
    "AuditToolOutcomeRequest",
    "AuditToolOutcomeResolution",
    "AuditToolOutcomeResolver",
    "AuditToolResolutionError",
]
