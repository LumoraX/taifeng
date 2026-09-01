"""工具图片附件的预算计量与压缩视图投影。"""

from __future__ import annotations

import base64
import hashlib

from taifeng.context.budget import estimate_item_tokens
from taifeng.context.compaction_view import CompactionView
from taifeng.conversation.models import function_call, function_call_output
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy

T = "t1"
POLICY = ImageInputPolicy(enabled=True, max_images=4)


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _payload() -> dict:
    data = _png()
    return ImageAttachmentV1(
        media_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"),
    ).model_dump()


def test_tool_output_images_count_into_token_budget() -> None:
    """fco 里的图片必须计入 token 估算 —— 否则内核自己的资源账是假的。"""
    with_image = function_call_output(
        "c1", "frame", thread_id=T, attachments=[_payload()]
    )
    without = function_call_output("c1", "frame", thread_id=T)

    assert estimate_item_tokens(
        with_image, image_input_policy=POLICY, model="m"
    ) > estimate_item_tokens(without, image_input_policy=POLICY, model="m")


def test_tool_output_images_count_even_without_policy() -> None:
    """未注入策略时也不得按零处理 —— 走保守上界，绝不低估。"""
    with_image = function_call_output(
        "c1", "frame", thread_id=T, attachments=[_payload()]
    )
    without = function_call_output("c1", "frame", thread_id=T)

    assert estimate_item_tokens(with_image, model="m") > estimate_item_tokens(
        without, model="m"
    )


def test_compaction_view_never_contains_base64() -> None:
    """压缩摘要 prompt 绝不能含 base64 正文 —— 会撑爆且泄漏。"""
    payload = _payload()
    history = [
        function_call("c1", "observe_frame", "{}", thread_id=T),
        function_call_output("c1", "frame 1023", thread_id=T, attachments=[payload]),
    ]

    rendered = CompactionView.from_items(history).format_for_summary()

    assert payload["content"] not in rendered
    assert "frame 1023" in rendered


def test_compaction_view_leaves_image_count_trace() -> None:
    """图被淘汰后，计数痕迹是模型唯一能看到的「这里曾有图」证据。"""
    history = [
        function_call("c1", "observe_frame", "{}", thread_id=T),
        function_call_output(
            "c1", "frame 1023", thread_id=T, attachments=[_payload(), _payload()]
        ),
    ]

    rendered = CompactionView.from_items(history).format_for_summary()

    assert "2" in rendered
    assert "图片" in rendered


def test_compaction_view_unchanged_without_attachments() -> None:
    """无附件的 fco 投影不受影响 —— 既有摘要形态零变化。"""
    history = [
        function_call("c1", "noop", "{}", thread_id=T),
        function_call_output("c1", "ok", thread_id=T),
    ]

    rendered = CompactionView.from_items(history).format_for_summary()

    assert "图片" not in rendered
