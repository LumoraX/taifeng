"""通用挂起 / resume 原语测试。"""
from __future__ import annotations

import dataclasses

import pytest

from taifeng.suspend.reason import PendingRequest, SuspendReason


def test_suspend_reason_values():
    # 四类挂起原因,值用于 JSON 序列化稳定性
    assert SuspendReason.PERMISSION.value == "permission"
    assert SuspendReason.FORM.value == "form"
    assert SuspendReason.DATA.value == "data"
    assert SuspendReason.SYSTEM_RETRY.value == "system_retry"


def test_pending_request_frozen_and_fields():
    req = PendingRequest(
        request_id="req_1",
        reason=SuspendReason.PERMISSION,
        payload_schema={"type": "object"},
        related_call_id="call_abc",
        detail={"scope": "tool_use", "target": "shell_exec"},
    )
    assert req.request_id == "req_1"
    assert req.reason is SuspendReason.PERMISSION
    assert req.related_call_id == "call_abc"
    # frozen:不可变
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.request_id = "x"  # type: ignore[misc]
