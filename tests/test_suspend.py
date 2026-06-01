"""通用挂起 / resume 原语测试。"""
from __future__ import annotations

import dataclasses
import json

import pytest

from taifeng.conversation.models import user_message
from taifeng.llm.errors import LLMError
from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.suspend.signal import SuspendSignal


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


def test_pending_request_default_dicts_are_independent():
    # 默认 payload_schema / detail 必须是各实例独立的对象(field default_factory),
    # 不能共享同一 dict,否则一处 mutate 会污染其他实例
    a = PendingRequest(request_id="a", reason=SuspendReason.FORM)
    b = PendingRequest(request_id="b", reason=SuspendReason.FORM)
    assert a.payload_schema is not b.payload_schema
    assert a.detail is not b.detail


def test_suspend_reason_json_serializes_to_string():
    # StrEnum 跨层序列化契约:json.dumps 直接得到字符串值(若有人改回普通 Enum 会回归)
    assert json.dumps({"r": SuspendReason.FORM}) == '{"r": "form"}'
    assert str(SuspendReason.PERMISSION) == "permission"


def test_suspend_signal_carries_pending():
    """SuspendSignal 携带 PendingRequest,且是 Exception 子类但非 LLMError 子类。"""
    req = PendingRequest(request_id="r1", reason=SuspendReason.FORM)
    sig = SuspendSignal(req)
    assert sig.pending is req
    # 是 Exception 子类(控制流),但不是 LLMError 家族
    assert isinstance(sig, Exception)
    assert not isinstance(sig, LLMError)


def test_suspension_item_constructor():
    from taifeng.conversation.models import suspension_item

    item = suspension_item(
        record_id="sr_1",
        submission_id="sub_1",
        turn_index=2,
        pending=[{"request_id": "r1", "reason": "permission", "payload_schema": {},
                  "related_call_id": "call_a", "detail": {}}],
        created_at=1000,
        thread_id="th_1",
    )
    assert item.kind == "suspension"
    assert item.thread_id == "th_1"
    assert item.payload["record_id"] == "sr_1"
    assert item.payload["turn_index"] == 2
    assert item.payload["pending"][0]["request_id"] == "r1"
    assert item.payload["resolved"] is False
    assert item.payload["created_at"] == 1000


def test_record_roundtrip_via_item():
    """SuspensionRecord → to_item() → from_item() 必须完整还原,含 SuspendReason 枚举类型。"""
    rec = SuspensionRecord(
        record_id="sr_1",
        thread_id="th_1",
        submission_id="sub_1",
        turn_index=1,
        pending=(
            PendingRequest(request_id="r1", reason=SuspendReason.PERMISSION,
                           related_call_id="call_a", detail={"scope": "tool_use"}),
            PendingRequest(request_id="r2", reason=SuspendReason.FORM,
                           related_call_id="call_b"),
        ),
        created_at=1234,
    )
    item = rec.to_item()
    assert item.kind == "suspension"
    back = SuspensionRecord.from_item(item)
    assert back == rec
    # 枚举还原正确(不是裸字符串)
    assert back.pending[0].reason is SuspendReason.PERMISSION
    assert back.pending[1].related_call_id == "call_b"


def test_record_request_ids():
    """request_ids() 返回全部 pending 的 request_id 集合。"""
    rec = SuspensionRecord(
        record_id="sr", thread_id="t", submission_id="s", turn_index=1,
        pending=(PendingRequest(request_id="a", reason=SuspendReason.DATA),
                 PendingRequest(request_id="b", reason=SuspendReason.DATA)),
        created_at=1,
    )
    assert rec.request_ids() == {"a", "b"}


def test_record_from_item_rejects_wrong_kind():
    """from_item 传入非 suspension item 必须抛 ValueError。"""
    bad = user_message("hi", thread_id="t")
    with pytest.raises(ValueError):
        SuspensionRecord.from_item(bad)
