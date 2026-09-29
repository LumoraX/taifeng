"""审计恢复写入的版本化 record payload（ADR 0070）。

strict audit Session 冷恢复时，「结果未知」的工具调用（悬空 ``tool_intent_committed``，或已
durable 为 ``unknown`` 的 ``tool_outcome_committed``）按副作用分流收敛。收敛结论作为一条**新的**
``tool_recovery_committed`` 记录追加进 Journal——不改写任何历史记录，hash chain 与 durable 语义
由 core 照常保证；resume 扫描以它为该 intent 的结算依据。

独立成模块而不并入 ``records.py``：后者已超 800 行红线，且本记录只由恢复路径产生。
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from taifeng.conversation.journal.models import NonEmptyStr  # noqa: TC001  # Pydantic 运行期需要
from taifeng.conversation.journal.records import PayloadModel

TOOL_RECOVERY_RECORD_TYPE = "tool_recovery_committed"
"""恢复收敛结论的 record_type。"""

type RecoveryBasis = Literal["reconcile", "effect_kind", "operator"]
"""结论依据：工具回查 / 副作用声明 / 人裁决。"""

type RecoveryVerdict = Literal["completed", "not_executed", "retry_safe", "provided", "aborted"]
"""结论本身；与依据的合法组合见 ``_ALLOWED_VERDICTS``。"""

type ReconcileStatus = Literal["completed", "not_executed", "unknown", "failed"]
"""回查函数的原始结论；``failed`` = 抛异常 / 超时 / 返回值违约。"""

# 依据 → 允许的结论：回查只能给出「已完成 / 未执行」，副作用声明只能给出「可安全重发」，
# 人裁决只能「给出真实结果 / 接受未知并继续」。其余组合都是写入方违约。
_ALLOWED_VERDICTS: dict[str, frozenset[str]] = {
    "reconcile": frozenset({"completed", "not_executed"}),
    "effect_kind": frozenset({"retry_safe"}),
    "operator": frozenset({"provided", "aborted"}),
}

# 改判已 durable 的 unknown outcome 时，模型早已看到那条结果（function_call_output 不可重写），
# 只有不需要纠正模型认知的结论才成立：确认未执行（已有的错误结果与事实一致）或人接受未知。
_VERDICTS_WITHOUT_NEW_OUTPUT = frozenset({"not_executed", "aborted"})


class ToolRecoveryCommittedV1(PayloadModel):
    """一个结果未知的工具调用在恢复时的 durable 收敛结论。

    Attributes:
        intent_record_id: 被结算的 ``tool_intent_committed``。
        outcome_record_id: 改判已 durable 为 ``unknown`` 的 outcome 时指向它；悬空 intent 为 None。
        call_id / name: 调用标识（与 intent 一致）。
        effect_kind: intent 落账时声明的副作用分类（判定依据之一，审计可复核）。
        basis / verdict: 结论依据与结论（合法组合受校验）。
        reconcile_status: 回查函数的原始结论；未提供回查函数时为 None。
        output / is_error: 随同 batch 补写给模型的 ``function_call_output`` 内容；改判已有 outcome
            时不补写，二者皆为 None。
        recovery_operation_id: 本次 resume 接管的 operation id（与 ``writer_takeover`` 同源）。
    """

    intent_record_id: NonEmptyStr
    outcome_record_id: NonEmptyStr | None = None
    call_id: NonEmptyStr
    name: NonEmptyStr
    effect_kind: NonEmptyStr
    basis: RecoveryBasis
    verdict: RecoveryVerdict
    reconcile_status: ReconcileStatus | None = None
    output: str | None = None
    is_error: bool | None = None
    recovery_operation_id: NonEmptyStr

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        """拒绝依据 / 结论错配，以及「是否补写结果」与是否改判已有 outcome 不一致。"""
        if self.verdict not in _ALLOWED_VERDICTS[self.basis]:
            raise ValueError(f"verdict {self.verdict!r} is not allowed for basis {self.basis!r}")
        if self.basis == "reconcile" and self.reconcile_status != self.verdict:
            raise ValueError("reconcile verdict must equal reconcile_status")
        has_output = self.output is not None
        if has_output != (self.is_error is not None):
            raise ValueError("output and is_error must be both present or both absent")
        if self.outcome_record_id is None and not has_output:
            raise ValueError("settling a dangling intent requires the output given to the model")
        if self.outcome_record_id is not None:
            if has_output:
                raise ValueError("an already recorded outcome cannot get a second output")
            if self.verdict not in _VERDICTS_WITHOUT_NEW_OUTPUT:
                raise ValueError(
                    f"verdict {self.verdict!r} cannot settle an already recorded outcome"
                )
        return self


__all__ = [
    "TOOL_RECOVERY_RECORD_TYPE",
    "ReconcileStatus",
    "RecoveryBasis",
    "RecoveryVerdict",
    "ToolRecoveryCommittedV1",
]
