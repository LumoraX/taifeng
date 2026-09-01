"""工具图片附件的 history → ApiMessage / ApiInputItem 投影。"""

from __future__ import annotations

import base64
import hashlib

from taifeng.conversation.models import (
    assistant_message,
    function_call,
    function_call_output,
)
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.types import ImagePart, TextPart
from taifeng.loop.prompt import history_to_api_messages

T = "th_1"
POLICY = ImageInputPolicy(enabled=True, max_images=4)

IMAGE_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}),
    provider="openai",
    protocol="responses",
    tool_output_modalities=frozenset({"text", "image"}),
)
TEXT_TOOL_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}),  # user 消息能带图
    provider="openai",
    protocol="chat",  # 但 tool 结果不能
)


def _png(width: int = 1, height: int = 1) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _payload(width: int = 1, height: int = 1) -> dict:
    data = _png(width, height)
    return ImageAttachmentV1(
        media_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"),
        detail="high",
    ).model_dump()


def _one_image_history() -> list:
    return [
        assistant_message("取一帧", thread_id=T, model="m"),
        function_call("c1", "observe_frame", "{}", thread_id=T),
        function_call_output(
            "c1", "frame 1023", thread_id=T, attachments=[_payload()]
        ),
    ]


def test_tool_output_without_attachments_stays_plain_string() -> None:
    """非图片工具的 tool 消息仍是裸字符串 —— 与既有逐位一致。"""
    messages = history_to_api_messages(
        [function_call_output("c1", "ok", thread_id=T)],
        image_input_policy=POLICY,
        model_capabilities=IMAGE_CAPS,
    )

    assert messages[0].content == "ok"


def test_tool_output_with_image_builds_parts_text_first() -> None:
    """带图时投影为 parts：文本在首项，图片按 attachment 顺序在后。"""
    messages = history_to_api_messages(
        _one_image_history(), image_input_policy=POLICY, model_capabilities=IMAGE_CAPS
    )
    tool_msg = messages[-1]

    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "c1"
    assert isinstance(tool_msg.content, list)
    assert isinstance(tool_msg.content[0], TextPart)
    assert tool_msg.content[0].text == "frame 1023"
    assert isinstance(tool_msg.content[1], ImagePart)
    assert tool_msg.content[1].detail == "high"


def test_image_order_follows_attachment_order() -> None:
    """多图按 attachment 顺序排列 —— 模型据序号指认「第几帧」。"""
    first, second = _payload(1, 1), _payload(2, 2)
    history = [
        function_call_output(
            "c1", "2 frames", thread_id=T, attachments=[first, second]
        )
    ]

    content = history_to_api_messages(
        history, image_input_policy=POLICY, model_capabilities=IMAGE_CAPS
    )[0].content

    assert [p.sha256 for p in content if isinstance(p, ImagePart)] == [
        first["sha256"],
        second["sha256"],
    ]


def test_empty_output_text_produces_no_empty_text_part() -> None:
    """只有图片时不生成空 TextPart —— 空项不占 API 数组槽位。"""
    history = [function_call_output("c1", "", thread_id=T, attachments=[_payload()])]

    content = history_to_api_messages(
        history, image_input_policy=POLICY, model_capabilities=IMAGE_CAPS
    )[0].content

    assert all(not isinstance(p, TextPart) for p in content)


def test_tool_output_degrades_with_inband_placeholder() -> None:
    """能力不足时降级为 in-band 文本占位符：模型看得见「这里本来有图」。"""
    content = history_to_api_messages(
        _one_image_history(),
        image_input_policy=POLICY,
        model_capabilities=TEXT_TOOL_CAPS,
    )[-1].content

    assert isinstance(content, str)
    assert "frame 1023" in content
    assert "1 image" in content


def test_degradation_is_not_silent_when_output_text_is_empty() -> None:
    """即便原文本为空，降级也必须留下可见痕迹，不能变成空串。"""
    history = [function_call_output("c1", "", thread_id=T, attachments=[_payload()])]

    content = history_to_api_messages(
        history, image_input_policy=POLICY, model_capabilities=TEXT_TOOL_CAPS
    )[0].content

    assert isinstance(content, str)
    assert content.strip() != ""


def test_image_tool_output_does_not_break_sample_merge() -> None:
    """带图的 fco 不得打断同轮合并 —— 并行工具仍归并为一条 assistant 消息。

    回归锁：图若走合成 user_message 就会在此处关窗、把一次采样劈成两条
    assistant（其中一条空 content 带 tool_calls），thinking 模型会 400。
    """
    history = [
        assistant_message("并发两工具", thread_id=T, model="m"),
        function_call("c1", "observe_frame", "{}", thread_id=T),
        function_call_output("c1", "A", thread_id=T, attachments=[_payload()]),
        function_call("c2", "observe_frame", "{}", thread_id=T),
        function_call_output("c2", "B", thread_id=T, attachments=[_payload(2, 2)]),
    ]

    messages = history_to_api_messages(
        history, image_input_policy=POLICY, model_capabilities=IMAGE_CAPS
    )

    assert [m.role for m in messages] == ["assistant", "tool", "tool"]
    assert len(messages[0].tool_calls or []) == 2
