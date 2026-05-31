"""G3：typed 恢复配方 recommend_recovery。"""

from __future__ import annotations

import pytest

from taifeng.llm.errors import FailureClass
from taifeng.llm.recovery import RecoveryStep, recommend_recovery


def test_transient_classes_allow_one_auto_retry() -> None:
    """瞬时类错误建议退避重试 + 允许一次自动重试。"""
    for cls in ("provider_rate_limit", "provider_transport"):
        plan = recommend_recovery(cls)  # type: ignore[arg-type]
        assert RecoveryStep.BACKOFF_RETRY in plan.steps
        assert plan.auto_retry_once is True


def test_context_window_suggests_compact_then_retry() -> None:
    plan = recommend_recovery("context_window")
    assert plan.steps[0] == RecoveryStep.COMPACT
    assert plan.auto_retry_once is True
    assert plan.escalate is True


def test_auth_escalates_no_auto_retry() -> None:
    plan = recommend_recovery("provider_auth")
    assert RecoveryStep.CHECK_CREDENTIALS in plan.steps
    assert plan.auto_retry_once is False
    assert plan.escalate is True


def test_cancelled_is_noop() -> None:
    plan = recommend_recovery("cancelled")
    assert plan.steps == (RecoveryStep.NONE,)
    assert plan.escalate is False


def test_unknown_fallback_escalates() -> None:
    # 不在表中的值回退到 unknown 配方
    plan = recommend_recovery("totally-made-up")  # type: ignore[arg-type]
    assert RecoveryStep.ESCALATE in plan.steps


@pytest.mark.parametrize(
    "cls",
    [
        "context_window", "provider_auth", "provider_rate_limit",
        "provider_transport", "provider_internal", "invalid_request",
        "content_filter", "cancelled", "request_size", "runtime_io", "unknown",
    ],
)
def test_every_class_has_recipe_and_serializes(cls: FailureClass) -> None:
    plan = recommend_recovery(cls)
    assert plan.failure_class == cls
    assert plan.steps  # 非空
    d = plan.to_dict()
    assert d["failure_class"] == cls
    assert isinstance(d["steps"], list) and d["steps"]
    assert isinstance(d["auto_retry_once"], bool)
    assert isinstance(d["escalate"], bool)
