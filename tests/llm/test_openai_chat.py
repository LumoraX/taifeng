"""OpenAI 官方 Chat 协议的图片输入与兼容边界测试。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import (
    InvalidHistoryError,
    UnsupportedCombinationError,
    UnsupportedModalityError,
)
from taifeng.llm.providers.openai.chat import OpenAIChatClient, OpenAIChatSession
from taifeng.llm.providers.openai_compat import OpenAICompatSession
from taifeng.llm.types import (
    ApiMessage,
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    ProviderStateEnvelope,
    ResponseFormatSpec,
    TextPart,
    ToolSpecRef,
)
from taifeng.loop.cancellation import CancellationToken

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_PNG_BASE64 = base64.b64encode(_PNG_BYTES).decode("ascii")
_IMAGE = ImagePart(
    media_type="image/png",
    base64_data=_PNG_BASE64,
    size=len(_PNG_BYTES),
    sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
)


def _chat_session(*, model: str = "gpt-5.6") -> OpenAIChatSession:
    """构造不发起网络请求的官方 Chat session。"""
    return OpenAIChatSession(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        model=model,
        cancel=CancellationToken(),
    )


def _compat_session() -> OpenAICompatSession:
    """构造兼容协议 session，用于防御性拒绝断言。"""
    return OpenAICompatSession(
        base_url="https://gateway.example/v1",
        api_key="key",
        model="compat-model",
        cancel=CancellationToken(),
    )


def test_openai_chat_declares_official_image_capabilities() -> None:
    """官方 Chat client 必须显式声明协议与图片能力。"""
    client = OpenAIChatClient(api_key="sk-test", model="gpt-5.6")

    assert client.capabilities == ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="openai",
        protocol="chat",
    )


def test_chat_maps_image_parts_to_data_urls_and_disables_store() -> None:
    """图片只在 wire 边界转成 Chat image_url Data URL。"""
    request = ApiRequest(
        model="gpt-5.6",
        messages=[
            ApiMessage(
                role="user",
                content=[TextPart(text="看图"), _IMAGE],
            )
        ],
    )

    payload = _chat_session()._build_payload(request)

    assert payload["store"] is False
    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "看图"},
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{_PNG_BASE64}",
                "detail": "auto",
            },
        },
    ]


def test_chat_retains_system_tool_and_structured_output_semantics() -> None:
    """专用 client 继续使用 Chat 的 system/tool/response_format 形状。"""
    request = ApiRequest(
        model="gpt-5.6",
        system_prompt=["你是检查员"],
        messages=[
            ApiMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": '{"ok":true}'},
                    }
                ],
            ),
            ApiMessage(role="tool", content='{"accepted":true}', tool_call_id="call-1"),
        ],
        tools=[
            ToolSpecRef(
                name="inspect",
                description="检查库存",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        response_format=ResponseFormatSpec(
            name="result",
            json_schema={"type": "object", "properties": {}},
        ),
    )

    payload = _chat_session()._build_payload(request)

    assert payload["messages"][0] == {"role": "system", "content": "你是检查员"}
    assert payload["messages"][1]["tool_calls"][0]["id"] == "call-1"
    assert payload["messages"][2]["tool_call_id"] == "call-1"
    assert payload["tools"][0]["function"]["name"] == "inspect"
    assert payload["response_format"]["json_schema"]["name"] == "result"


def test_gpt_5_6_chat_tools_reject_non_none_reasoning_before_network() -> None:
    """GPT-5.6 Chat 的 tool 组合只接受未设置或 none reasoning。"""
    request = ApiRequest(
        model="gpt-5.6",
        messages=[ApiMessage(role="user", content="检查")],
        tools=[ToolSpecRef(name="inspect", description="检查", input_schema={})],
        reasoning_effort="medium",
    )

    with pytest.raises(UnsupportedCombinationError):
        _chat_session()._build_payload(request)


def test_openai_compat_rejects_images_before_serialization() -> None:
    """兼容客户端保持 text-only，不得把 Pydantic part 原样交给 httpx。"""
    request = ApiRequest(
        model="compat-model",
        messages=[ApiMessage(role="user", content=[_IMAGE])],
    )

    with pytest.raises(UnsupportedModalityError):
        _compat_session()._build_payload(request)


def test_openai_compat_rejects_provider_state_before_serialization() -> None:
    """兼容客户端不得静默丢弃 Responses provider state。"""
    request = ApiRequest(
        model="compat-model",
        input_items=[
            ApiProviderStateItem(
                sample_id="sample-1",
                output_index=0,
                state=ProviderStateEnvelope(
                    provider="openai",
                    protocol="responses",
                    item_type="reasoning",
                    payload={"id": "rs_1", "encrypted_content": "ciphertext"},
                ),
            )
        ],
    )

    with pytest.raises(InvalidHistoryError):
        _compat_session()._build_payload(request)
