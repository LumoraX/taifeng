"""G3：基于 failure_class 的 typed 恢复配方（advisory）。

参照 claw-code crates/runtime/src/recovery_recipes.rs —— 把"某类失败该怎么办"
固化成结构化数据：步骤序列 + 是否允许一次自动重试 + 是否需升级人工。

定位（R1 业务零侵入）：本模块**只产出建议**，不自动执行。引擎把 RecoveryPlan
透出到 ``TurnFailed`` 事件，是否真的自动重试 / 升级由业务侧编排层决定。这与
``suggested_action`` 同philosophy（人读 vs 机读两个层次）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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

    def to_dict(self) -> dict[str, object]:
        """序列化为事件 / telemetry 友好的 dict。"""
        return {
            "failure_class": self.failure_class,
            "steps": [s.value for s in self.steps],
            "auto_retry_once": self.auto_retry_once,
            "escalate": self.escalate,
        }


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
