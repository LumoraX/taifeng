"""G3：基于 failure_class 的 typed 恢复配方（advisory）。

参照 claw-code crates/runtime/src/recovery_recipes.rs —— 把"某类失败该怎么办"
固化成结构化数据：步骤序列 + 是否允许一次自动重试 + 是否需升级人工。

定位（R1 业务零侵入）：本模块**只产出建议**，不自动执行。引擎把 RecoveryPlan
透出到 ``TurnFailed`` 事件，是否真的自动重试 / 升级由业务侧编排层决定。这与
``suggested_action`` 同philosophy（人读 vs 机读两个层次）。

配方可由业务**声明**（ADR 0084）：``RecoveryRecipeBook`` 在内核配方表之上按失败分类覆盖，
声明在构造期校验；经失败处置 policy 注入（``loop/failure_policy.RecipeDeclaringPolicy``）。
业务特有的恢复动作（换端点、转人工复核等）写进 ``RecoveryPlan.custom_steps``——内核不解释
它们，只原样透出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import get_args

from taifeng.llm.errors import FailureClass


class RecoveryStep(str, Enum):
    """单个恢复动作（机读枚举）。"""

    RETRY = "retry"                    # 原样立即重试
    BACKOFF_RETRY = "backoff_retry"    # 指数退避后重试
    COMPACT = "compact"                # 先压缩上下文再重试
    REDUCE_REQUEST = "reduce_request"  # 精简请求（消息 / 附件）
    CHECK_CREDENTIALS = "check_credentials"  # 检查 API key / 凭据
    ADJUST_INPUT = "adjust_input"      # 调整输入（内容被拦 / 参数非法）
    ESCALATE = "escalate"              # 升级到人工
    NONE = "none"                      # 无需处理


@dataclass(frozen=True)
class RecoveryPlan:
    """某个 failure_class 的结构化恢复建议。"""

    failure_class: FailureClass
    steps: tuple[RecoveryStep, ...]
    auto_retry_once: bool
    """是否建议「自动重试恰好一次」后再升级（claw-code 范式）。"""
    escalate: bool
    """自动手段耗尽后是否需要人工介入。"""
    custom_steps: tuple[str, ...] = ()
    """业务声明的恢复动作（内核不解释，原样透出）；排在 ``steps`` 之后执行由业务约定。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为事件 / telemetry 友好的 dict；没有 ``custom_steps`` 时不带该键。"""
        data: dict[str, object] = {
            "failure_class": self.failure_class,
            "steps": [s.value for s in self.steps],
            "auto_retry_once": self.auto_retry_once,
            "escalate": self.escalate,
        }
        if self.custom_steps:
            data["custom_steps"] = list(self.custom_steps)
        return data


# 各 failure_class → 恢复配方。原则（对齐 claw-code）：瞬时错误允许一次自动
# 退避重试；不可恢复错误直接给出人工动作 + 升级；取消无需处理。
_RECIPES: dict[FailureClass, RecoveryPlan] = {
    "context_window": RecoveryPlan(
        "context_window", (RecoveryStep.COMPACT, RecoveryStep.BACKOFF_RETRY),
        auto_retry_once=True, escalate=True,
    ),
    "provider_auth": RecoveryPlan(
        "provider_auth", (RecoveryStep.CHECK_CREDENTIALS, RecoveryStep.ESCALATE),
        auto_retry_once=False, escalate=True,
    ),
    "provider_rate_limit": RecoveryPlan(
        "provider_rate_limit", (RecoveryStep.BACKOFF_RETRY,),
        auto_retry_once=True, escalate=False,
    ),
    "provider_transport": RecoveryPlan(
        "provider_transport", (RecoveryStep.BACKOFF_RETRY,),
        auto_retry_once=True, escalate=False,
    ),
    "provider_internal": RecoveryPlan(
        "provider_internal", (RecoveryStep.BACKOFF_RETRY, RecoveryStep.ESCALATE),
        auto_retry_once=True, escalate=True,
    ),
    "invalid_request": RecoveryPlan(
        "invalid_request", (RecoveryStep.ADJUST_INPUT, RecoveryStep.ESCALATE),
        auto_retry_once=False, escalate=True,
    ),
    "content_filter": RecoveryPlan(
        "content_filter", (RecoveryStep.ADJUST_INPUT,),
        auto_retry_once=False, escalate=True,
    ),
    # 网关未如实上报终止原因：标签不具判别力，真实原因多为瞬时（如畸形 tool call），
    # 故与 provider_internal 同形——先退避重试，仍失败再上报人工。
    "provider_unreliable_finish": RecoveryPlan(
        "provider_unreliable_finish", (RecoveryStep.BACKOFF_RETRY, RecoveryStep.ESCALATE),
        auto_retry_once=True, escalate=True,
    ),
    "cancelled": RecoveryPlan(
        "cancelled", (RecoveryStep.NONE,),
        auto_retry_once=False, escalate=False,
    ),
    "request_size": RecoveryPlan(
        "request_size", (RecoveryStep.REDUCE_REQUEST, RecoveryStep.COMPACT),
        auto_retry_once=False, escalate=True,
    ),
    "runtime_io": RecoveryPlan(
        "runtime_io", (RecoveryStep.ESCALATE,),
        auto_retry_once=False, escalate=True,
    ),
    "unknown": RecoveryPlan(
        "unknown", (RecoveryStep.ESCALATE,),
        auto_retry_once=False, escalate=True,
    ),
}


def recommend_recovery(failure_class: FailureClass) -> RecoveryPlan:
    """返回某个 failure_class 的结构化恢复配方（缺失回退到 unknown）。"""
    return _RECIPES.get(failure_class, _RECIPES["unknown"])


KNOWN_FAILURE_CLASSES: frozenset[str] = frozenset(get_args(FailureClass))
"""内核定义的全部失败分类；声明的配方只能针对它们。"""

_KERNEL_STEP_NAMES: frozenset[str] = frozenset(step.value for step in RecoveryStep)
# 这些动作意味着「再发一次请求」
_RETRYING_STEPS = frozenset({RecoveryStep.RETRY, RecoveryStep.BACKOFF_RETRY})


def _validate_declared(plan: RecoveryPlan) -> None:
    """校验一条声明的配方；不合法抛 ``ValueError``。"""
    if plan.failure_class not in KNOWN_FAILURE_CLASSES:
        raise ValueError(f"unknown failure_class in recovery recipe: {plan.failure_class!r}")
    if not plan.steps:
        raise ValueError(f"recovery recipe for {plan.failure_class!r} declares no steps")
    for step in plan.custom_steps:
        if not step or not step.strip():
            raise ValueError(f"blank custom step in recipe for {plan.failure_class!r}")
        if step in _KERNEL_STEP_NAMES:
            raise ValueError(
                f"custom step {step!r} shadows a kernel step; put it in steps instead"
            )
    if plan.failure_class == "cancelled" and (
        plan.auto_retry_once or _RETRYING_STEPS & set(plan.steps)
    ):
        # 取消是调用方的意图，不是失败：声明「取消后重试」等于违背取消
        raise ValueError("recovery recipe for 'cancelled' must not retry")


@dataclass(frozen=True)
class RecoveryRecipeBook:
    """一份恢复配方表：内核配方 + 业务按失败分类声明的覆盖。

    不可变；``declare`` 返回新表。未声明的分类沿用内核配方。
    """

    _declared: dict[str, RecoveryPlan] = field(default_factory=dict)

    @classmethod
    def default(cls) -> RecoveryRecipeBook:
        """只含内核配方的表。"""
        return cls()

    @property
    def declared_classes(self) -> frozenset[str]:
        """业务声明过配方的失败分类。"""
        return frozenset(self._declared)

    def declare(self, *plans: RecoveryPlan) -> RecoveryRecipeBook:
        """在本表之上声明若干配方，返回新表。

        Raises:
            ValueError: 配方不合法（未知分类 / 无步骤 / 自定义步骤为空或与内核步骤重名 /
                对 cancelled 声明重试），或同一分类在本次调用里声明了多次。
        """
        declared = dict(self._declared)
        seen: set[str] = set()
        for plan in plans:
            _validate_declared(plan)
            if plan.failure_class in seen:
                raise ValueError(
                    f"failure_class {plan.failure_class!r} declared more than once"
                )
            seen.add(plan.failure_class)
            declared[plan.failure_class] = plan
        return RecoveryRecipeBook(declared)

    def declared(self, failure_class: str) -> RecoveryPlan | None:
        """该分类上业务声明的配方；未声明返回 None。"""
        return self._declared.get(failure_class)

    def recommend(self, failure_class: FailureClass) -> RecoveryPlan:
        """该分类的配方：声明优先，否则内核配方；未知分类按 ``unknown`` 的配方。"""
        plan = self._declared.get(failure_class)
        if plan is not None:
            return plan
        if failure_class not in KNOWN_FAILURE_CLASSES:
            return self._declared.get("unknown", _RECIPES["unknown"])
        return _RECIPES[failure_class]
