"""function_call_output 图片附件 payload 的持久化契约。"""

from __future__ import annotations

from taifeng.conversation.models import function_call_output

_IMAGE_A = {"kind": "image", "sha256": "a" * 64}
_IMAGE_B = {"kind": "image", "sha256": "b" * 64}


def test_without_attachments_keeps_payload_shape_byte_for_byte() -> None:
    """不带附件时 payload 必须逐键与既有一致 —— 冷恢复与审计比对依赖它。"""
    item = function_call_output("c1", "ok", thread_id="t1")

    assert item.payload == {"call_id": "c1", "output": "ok", "is_error": False}


def test_empty_attachments_omits_the_key() -> None:
    """空列表等同于不带 —— 不写空键，避免老新数据形状分叉。"""
    item = function_call_output("c1", "ok", thread_id="t1", attachments=[])

    assert "attachments" not in item.payload


def test_attachments_are_persisted_in_order() -> None:
    """带附件时才出现该键，且顺序即传入序（渲染据此排 ImagePart）。"""
    item = function_call_output(
        "c1", "2 frames", thread_id="t1", attachments=[_IMAGE_A, _IMAGE_B]
    )

    assert item.payload["attachments"] == [_IMAGE_A, _IMAGE_B]
    assert item.payload["output"] == "2 frames"
    assert item.payload["call_id"] == "c1"


def test_error_result_can_still_carry_no_attachments() -> None:
    """错误结果的形状不受新字段影响。"""
    item = function_call_output("c1", "boom", thread_id="t1", is_error=True)

    assert item.payload == {"call_id": "c1", "output": "boom", "is_error": True}


def test_kind_stays_function_call_output() -> None:
    """图片进的是 fco 内部，不新增 ItemKind —— turn 边界消费者不受污染。"""
    item = function_call_output(
        "c1", "frame", thread_id="t1", attachments=[_IMAGE_A]
    )

    assert item.kind == "function_call_output"
