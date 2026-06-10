"""failure_policy 模块单测 —— 两个内置 policy 的判定矩阵 + 协议形态。

对应 openspec change ``failure-suspension-policy`` 任务 1.1;
turn 层接入(2.1/2.2)的行为测试在本文件追加(任务推进时补)。
"""
from __future__ import annotations

import pytest

from taifeng.loop.failure_policy import (
    DEFAULT_FAILURE_POLICY,
    ConservativeFailurePolicy,
    FailureContext,
    FailureDisposition,
    FailureDispositionPolicy,
    SuspendByDefaultPolicy,
)


def _llm_ctx(
    *, failure_class: str, retryable: bool, is_root: bool = True
) -> FailureContext:
    """构造 llm_error 来源的裁决上下文(测试便捷工厂)。"""
    return FailureContext(
        origin="llm_error",
        failure_class=failure_class,
        end_reason=None,
        error_kind="LLMError",
        retryable=retryable,
        is_root=is_root,
        iteration=1,
    )


def _guard_ctx(end_reason: str) -> FailureContext:
    """构造 guard_trip 来源的裁决上下文(测试便捷工厂)。"""
    return FailureContext(
        origin="guard_trip",
        failure_class=None,
        end_reason=end_reason,
        error_kind=None,
        retryable=False,
        is_root=True,
        iteration=3,
    )


# ---------- ConservativeFailurePolicy:复刻 _should_suspend_on_error 判据 ----------

@pytest.mark.parametrize(
    ("failure_class", "retryable", "expected"),
    [
        # retryable 为真(限流 / 瞬时网络 / 5xx)→ SUSPEND
        ("provider_rate_limit", True, FailureDisposition.SUSPEND),
        ("provider_transport", True, FailureDisposition.SUSPEND),
        ("provider_internal", True, FailureDisposition.SUSPEND),
        # 等外部介入类(鉴权 / 配额 / 余额)即使 retryable=False 也 SUSPEND
        ("provider_auth", False, FailureDisposition.SUSPEND),
        ("provider_quota", False, FailureDisposition.SUSPEND),
        ("provider_balance", False, FailureDisposition.SUSPEND),
        # 确定性失败 → TERMINAL(resume 救不回)
        ("content_filter", False, FailureDisposition.TERMINAL),
        ("invalid_request", False, FailureDisposition.TERMINAL),
        ("context_window", False, FailureDisposition.TERMINAL),
        ("unknown", False, FailureDisposition.TERMINAL),
    ],
)
def test_conservative_llm_error_matrix(
    failure_class: str, retryable: bool, expected: FailureDisposition
) -> None:
    """保守 policy 对 llm_error 的判定矩阵与原硬编码判据一致。"""
    policy = ConservativeFailurePolicy()
    assert policy.decide(
        _llm_ctx(failure_class=failure_class, retryable=retryable)
    ) is expected


@pytest.mark.parametrize(
    "end_reason",
    ["max_iterations", "resource_limit_exceeded", "denial_circuit_open"],
)
def test_conservative_guard_trip_always_terminal(end_reason: str) -> None:
    """保守 policy 下全部护栏触顶维持终态(历史行为)。"""
    policy = ConservativeFailurePolicy()
    assert policy.decide(_guard_ctx(end_reason)) is FailureDisposition.TERMINAL


# ---------- SuspendByDefaultPolicy:一切失败皆挂起 ----------

@pytest.mark.parametrize(
    "ctx",
    [
        _llm_ctx(failure_class="content_filter", retryable=False),
        _llm_ctx(failure_class="provider_rate_limit", retryable=True),
        _llm_ctx(failure_class="invalid_request", retryable=False, is_root=False),
        _guard_ctx("max_iterations"),
        _guard_ctx("resource_limit_exceeded"),
        _guard_ctx("denial_circuit_open"),
    ],
)
def test_suspend_by_default_always_suspends(ctx: FailureContext) -> None:
    """SuspendByDefault 对任意来源 / 分类的失败一律 SUSPEND。"""
    assert SuspendByDefaultPolicy().decide(ctx) is FailureDisposition.SUSPEND


# ---------- 协议与默认单例 ----------

def test_builtin_policies_satisfy_protocol() -> None:
    """两个内置实现满足 FailureDispositionPolicy 协议(结构化子类型)。"""
    conservative: FailureDispositionPolicy = ConservativeFailurePolicy()
    suspend_all: FailureDispositionPolicy = SuspendByDefaultPolicy()
    ctx = _guard_ctx("max_iterations")
    assert conservative.decide(ctx) is FailureDisposition.TERMINAL
    assert suspend_all.decide(ctx) is FailureDisposition.SUSPEND


def test_default_policy_is_conservative() -> None:
    """模块级默认单例是保守 policy(None 注入时的零变化保证)。"""
    assert isinstance(DEFAULT_FAILURE_POLICY, ConservativeFailurePolicy)


def test_failure_context_is_frozen() -> None:
    """FailureContext 不可变(frozen dataclass,防 policy 内偷改上下文)。"""
    ctx = _guard_ctx("max_iterations")
    with pytest.raises(AttributeError):
        ctx.end_reason = "other"  # type: ignore[misc]
