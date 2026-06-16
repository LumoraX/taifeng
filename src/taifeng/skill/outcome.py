"""Skill 执行战绩数据沉淀（认知回路 ⑦ 沉淀相位的地基）。

只采集、不决策：本模块产出「这一次 skill 执行干成没有」的结构化记录，供后续相位
（fitness 计算 / 提拔 / 逐出 / 隔离）消费。**严禁**把 selection_confidence（长相分）
喂入任何提拔逻辑——长相骗④评估、战绩防⑦沉淀，二者在数据层即分开存。

参照设计：docs/superpowers/specs/2026-06-16-skill-capability-acquisition-loop-design.md §5
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from taifeng.skill.definition import SkillSource

OutcomeStatus = Literal["success", "failure", "abandoned"]
OutcomeSignalSource = Literal["structural", "business"]
SelectionOrigin = Literal["whitelist", "discovered"]


@dataclass(frozen=True)
class OutcomeVerdict:
    """单次执行的战绩裁决。"""

    status: OutcomeStatus
    reason: str
    signal_source: OutcomeSignalSource


@dataclass(frozen=True)
class SkillExecutionContext:
    """喂给 OutcomeJudge 的判定上下文（v1 结构性判定只需这几个硬信号）。"""

    success: bool
    end_reason: str
    error: str | None = None


@dataclass(frozen=True)
class SkillExecutionRecord:
    """一次 skill 执行的战绩记录。刻意把「长相」与「战绩」分字段存（防假 skill 的根）。"""

    skill_id: str
    call_id: str
    parent_call_id: str | None
    depth: int
    source: SkillSource
    trust_tier: str | None
    selection_origin: SelectionOrigin
    selection_confidence: float | None
    outcome: OutcomeStatus
    outcome_signal_source: OutcomeSignalSource
    end_reason: str
    error_detail: str | None
    cost_tokens: int
    cost_duration_ms: int
    cost_iterations: int
    ts_unix: int

    def as_payload(self) -> dict[str, Any]:
        """转 JSON-safe dict，作为 ResponseItem.payload 落 JSONL / 事件 data。"""
        return {
            "skill_id": self.skill_id,
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "depth": self.depth,
            "source": self.source,
            "trust_tier": self.trust_tier,
            "selection_origin": self.selection_origin,
            "selection_confidence": self.selection_confidence,
            "outcome": self.outcome,
            "outcome_signal_source": self.outcome_signal_source,
            "end_reason": self.end_reason,
            "error_detail": self.error_detail,
            "cost_tokens": self.cost_tokens,
            "cost_duration_ms": self.cost_duration_ms,
            "cost_iterations": self.cost_iterations,
            "ts_unix": self.ts_unix,
        }


@runtime_checkable
class OutcomeJudge(Protocol):
    """判定单次 skill 执行的战绩。内核定协议 + 默认结构性实现；业务可注入更强实现。"""

    def judge(self, ctx: SkillExecutionContext) -> OutcomeVerdict:
        ...


_ABANDONED_END_REASONS: frozenset[str] = frozenset(
    {
        "cancelled",
        "max_iterations",
        "resource_limit_exceeded",
        "denial_circuit_open",
        "doom_loop_circuit_open",
    }
)


@dataclass(frozen=True)
class StructuralOutcomeJudge:
    """内核默认战绩判定：只用看得见的硬信号，业务无关、开箱即用。

    映射规则（确定性）：
      - success=True                → success
      - success=False 且 end_reason ∈ 放弃集 → abandoned
      - 其余非成功（含 error / 未知 end_reason）→ failure（不静默吞未知）

    注：suspended 不会到这里——挂起在 run_sub_skill 内提前返回，judge 不被调用。
    """

    def judge(self, ctx: SkillExecutionContext) -> OutcomeVerdict:
        if ctx.success:
            return OutcomeVerdict(
                status="success",
                reason=f"completed:{ctx.end_reason}",
                signal_source="structural",
            )
        if ctx.end_reason in _ABANDONED_END_REASONS:
            return OutcomeVerdict(
                status="abandoned",
                reason=f"abandoned:{ctx.end_reason}",
                signal_source="structural",
            )
        return OutcomeVerdict(
            status="failure",
            reason=f"failed:{ctx.end_reason}",
            signal_source="structural",
        )
