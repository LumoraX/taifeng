"""从 durable user attachment 到 provider-neutral prompt parts 的转换。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from taifeng.conversation.models import user_message
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import UnsupportedModalityError
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.types import ImagePart, TextPart
from taifeng.loop.prompt import history_to_api_messages


def _attachment() -> dict[str, object]:
    """构造一张尺寸为 1×1 的最小 PNG attachment。"""
    data = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    )
    return ImageAttachmentV1(
        media_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"),
        detail="high",
    ).model_dump()


IMAGE_POLICY = ImageInputPolicy(
    enabled=True,
    max_images=2,
    max_item_bytes=1024,
    max_total_bytes=1024,
    allowed_media_types=frozenset({"image/png"}),
)
IMAGE_CAPABILITIES = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}), provider="openai", protocol="chat"
)


def test_text_plus_image_uses_ordered_parts() -> None:
    """文本始终在首位，随后是 durable attachment 的顺序。"""
    messages = history_to_api_messages(
        [user_message("inspect", thread_id="t", attachments=[_attachment()])],
        image_input_policy=IMAGE_POLICY,
        model_capabilities=IMAGE_CAPABILITIES,
    )

    content = messages[0].content
    assert isinstance(content, list)
    assert isinstance(content[0], TextPart)
    assert content[0].text == "inspect"
    assert isinstance(content[1], ImagePart)
    assert content[1].detail == "high"


def test_image_only_user_message_has_no_empty_text_part() -> None:
    """纯图片请求不能给 provider 发送空文字部件。"""
    messages = history_to_api_messages(
        [user_message("", thread_id="t", attachments=[_attachment()])],
        image_input_policy=IMAGE_POLICY,
        model_capabilities=IMAGE_CAPABILITIES,
    )

    assert isinstance(messages[0].content, list)
    assert len(messages[0].content) == 1
    assert isinstance(messages[0].content[0], ImagePart)


def test_image_attachment_fails_closed_when_client_is_text_only() -> None:
    """未改造 provider 绝不能把图片悄悄丢弃后继续请求。"""
    with pytest.raises(UnsupportedModalityError):
        history_to_api_messages(
            [user_message("inspect", thread_id="t", attachments=[_attachment()])],
            image_input_policy=IMAGE_POLICY,
            model_capabilities=ModelCapabilities(
                input_modalities=frozenset({"text"}), provider="legacy", protocol="chat"
            ),
        )
