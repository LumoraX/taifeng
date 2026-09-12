"""ConsoleSink 渲染 —— 关键事件必须有专用渲染（R3 可观测完整性，非 `evt` 兜底）。

真实回归 R3 审计发现 ``user_input_injected`` / ``post_turn_hook_fired`` 落 `evt` 兜底
（只 dump 原始 data dict），信息不自描述。本测试钉死这两类各有专用 tag + 字段渲染。
"""

from __future__ import annotations

from taifeng.loop.event import EventMsg, PostTurnHookFired, UserInputInjected
from taifeng.telemetry.console import _fmt_event


def _line(msg: object) -> str:
    ev = EventMsg(submission_id="sub-123456789012", msg=msg)  # type: ignore[arg-type]
    return _fmt_event(ev, color=False)


def test_user_input_injected_has_dedicated_render() -> None:
    """user_input_injected：专用 tag + delivered 状态 + 文本预览，不落兜底。"""
    line = _line(UserInputInjected(data={
        "submission_id": "s", "delivered": True, "text_preview": "改成方案B",
    }))
    assert " evt " not in line, f"不应落 evt 兜底：{line}"
    assert "user_input_injected {" not in line, "不应是 raw data dump"
    assert "delivered=True" in line
    assert "改成方案B" in line


def test_post_turn_hook_fired_has_dedicated_render() -> None:
    """post_turn_hook_fired：专用 tag + end_reason + hook 数，不落兜底。"""
    line = _line(PostTurnHookFired(data={
        "end_reason": "completed", "iteration": 2, "hook_count": 1,
    }))
    assert " evt " not in line, f"不应落 evt 兜底：{line}"
    assert "post_turn_hook_fired {" not in line, "不应是 raw data dump"
    assert "completed" in line
    assert "hooks=1" in line


def test_provider_retry_network_has_dedicated_render() -> None:
    """provider_retry（网络退避重试）：专用 tag + reason/attempt/backoff/class，不落兜底。"""
    from taifeng.loop.event import ProviderRetry

    line = _line(ProviderRetry(data={
        "reason": "transient_network", "iteration": 0, "attempt": 2, "max_attempts": 3,
        "delay_seconds": 1.25, "failure_class": "provider_transport",
        "error_kind": "TransientNetworkError", "transport_phase": "connect",
        "retry_after_seconds": None,
    }))
    assert " evt " not in line, f"不应落 evt 兜底：{line}"
    assert "provider_retry {" not in line, "不应是 raw data dump"
    assert "transient_network attempt 2/3" in line
    assert "backoff=1.25s" in line
    assert "class=provider_transport" in line


def test_provider_retry_overflow_shape_still_renders() -> None:
    """provider_retry（overflow 自愈，无 attempt 字段）：沿用 reason + iter 简式，不崩。"""
    from taifeng.loop.event import ProviderRetry

    line = _line(ProviderRetry(data={"reason": "context_overflow", "iteration": 1}))
    assert " evt " not in line
    assert "context_overflow iter=1" in line
