"""图片输入 provider-neutral DTO 契约。"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from taifeng.llm import ImagePart as PublicImagePart
from taifeng.llm.client import model_capabilities
from taifeng.llm.types import (
    ApiMessage,
    ApiMessageItem,
    ApiRequest,
    ImagePart,
    TextPart,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n"
PNG_BASE64 = "iVBORw0KGgo="
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()


def _image_part() -> ImagePart:
    """构造一张最小 canonical PNG part。"""
    return ImagePart(
        media_type="image/png",
        base64_data=PNG_BASE64,
        size=len(PNG_BYTES),
        sha256=PNG_SHA256,
    )


def test_text_only_api_message_keeps_string_content() -> None:
    """既有纯文本请求保持 str，不改 cache prefix 的形态。"""
    message = ApiMessage(role="user", content="inspect this")
    assert message.content == "inspect this"
    assert isinstance(message.content, str)


def test_items_only_request_derives_ordered_compatibility_message() -> None:
    """input_items 是规范源，兼容 messages 从它无损派生。"""
    image = _image_part()
    request = ApiRequest(
        model="test-model",
        input_items=[
            ApiMessageItem(
                role="user",
                content=[TextPart(text="inspect"), image],
            )
        ],
    )

    assert request.input_items[0].type == "message"
    assert request.messages == [ApiMessage(role="user", content=[TextPart(text="inspect"), image])]


def test_image_only_item_has_no_empty_text_part() -> None:
    """纯图片消息只包含 image part，不能伪造空 text part。"""
    image = _image_part()
    request = ApiRequest(
        model="test-model",
        input_items=[ApiMessageItem(role="user", content=[image])],
    )

    assert request.messages[0].content == [image]


def test_conflicting_messages_and_input_items_are_rejected() -> None:
    """双 view 不一致不能由 adapter 自行猜测。"""
    with pytest.raises(ValidationError, match="input_items"):
        ApiRequest(
            model="test-model",
            messages=[ApiMessage(role="user", content="one")],
            input_items=[ApiMessageItem(role="user", content="two")],
        )


def test_legacy_client_defaults_to_text_only_capabilities() -> None:
    """未迁移的 custom client 不会意外获得图片输入能力。"""

    class LegacyClient:
        """仅模拟老接口；故意没有 capabilities 属性。"""

    capabilities = model_capabilities(LegacyClient())

    assert capabilities.input_modalities == frozenset({"text"})
    assert capabilities.provider == "unknown"
    assert capabilities.protocol == "unknown"
    assert capabilities.accepts_provider_state is False


def test_image_part_is_available_from_public_llm_api() -> None:
    """业务侧无需依赖内部 types 模块即可声明图片输入。"""
    assert PublicImagePart is ImagePart
