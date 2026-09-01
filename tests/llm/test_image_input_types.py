"""图片输入 provider-neutral DTO 契约。"""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from taifeng.llm import ImagePart as PublicImagePart
from taifeng.llm.client import model_capabilities
from taifeng.llm.image_input import redact_image_bodies
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


def test_sensitive_request_redaction_removes_image_and_encrypted_state() -> None:
    """durable request 快照不得保留图片正文或 provider 密文。"""
    request = {
        "input_items": [_image_part().model_dump(mode="json")],
        "provider_state": {
            "payload": {"encrypted_content": "encrypted-state"},
        },
    }

    redacted = redact_image_bodies(request)
    encoded = json.dumps(redacted)

    assert PNG_BASE64 not in encoded
    assert "encrypted-state" not in encoded
    assert "encrypted_content" not in encoded


def test_tool_output_modalities_defaults_to_text_only() -> None:
    """未声明的 client 一律 text-only —— 能力必须显式打开，不按模型名猜。"""
    from taifeng.llm.client import TEXT_ONLY_CAPABILITIES, ModelCapabilities

    assert TEXT_ONLY_CAPABILITIES.tool_output_modalities == frozenset({"text"})

    # user 消息能带图 ≠ tool 结果能带图：两种能力必须分开声明
    caps = ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="p",
        protocol="chat",
    )
    assert caps.tool_output_modalities == frozenset({"text"})


def test_responses_clients_declare_image_tool_output() -> None:
    """只有 Responses 协议原生接受 fco 带图，故只有这两个 client 声明。"""
    from taifeng.llm.providers.codex.responses import CodexResponsesClient
    from taifeng.llm.providers.openai.responses import OpenAIResponsesClient

    assert "image" in OpenAIResponsesClient.capabilities.tool_output_modalities
    assert "image" in CodexResponsesClient.capabilities.tool_output_modalities


def test_chat_client_does_not_declare_image_tool_output() -> None:
    """Chat 的 tool 消息 content 只能是字符串，不得声明 image tool 输出。"""
    from taifeng.llm.providers.openai.chat import OpenAIChatClient

    assert "image" in OpenAIChatClient.capabilities.input_modalities
    assert "image" not in OpenAIChatClient.capabilities.tool_output_modalities
