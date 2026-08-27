"""Sim 图片输入的结构 conformance 与脱敏侦察测试。"""

from __future__ import annotations

import hashlib

import pytest

import taifeng
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.llm.types import ApiMessage, ApiRequest, ImagePart, TextPart
from taifeng.loop.cancellation import CancellationToken

_FIRST_BASE64 = "Zmlyc3QtaW1hZ2UtYnl0ZXM="
_SECOND_BASE64 = "c2Vjb25kLWltYWdlLWJ5dGVz"


def _image(base64_data: str, *, detail: str) -> ImagePart:
    """构造无需真实解码的 provider-neutral Sim 输入部件。"""
    decoded = {
        _FIRST_BASE64: b"first-image-bytes",
        _SECOND_BASE64: b"second-image-bytes",
    }[base64_data]
    return ImagePart(
        media_type="image/png",
        base64_data=base64_data,
        size=len(decoded),
        sha256=hashlib.sha256(decoded).hexdigest(),
        detail=detail,
    )


@pytest.mark.asyncio
async def test_sim_records_redacted_image_descriptors_in_request_order() -> None:
    """Sim 只记录图片结构与摘要，不保存或解释图片正文。"""
    first = _image(_FIRST_BASE64, detail="low")
    second = _image(_SECOND_BASE64, detail="high")
    request = ApiRequest(
        model="sim-image",
        messages=[
            ApiMessage(role="user", content=[TextPart(text="compare"), first]),
            ApiMessage(role="user", content=[second]),
        ],
    )
    client = SimClient(
        turns=[SimTurn(text="structure accepted")],
        capabilities=ModelCapabilities(
            input_modalities=frozenset({"text", "image"}),
            provider="sim",
            protocol="sim",
        ),
    )

    async with client.session(cancel=CancellationToken()) as session:
        _ = [event async for event in session.stream(request)]

    recorded = client.ledger.single_request()
    descriptors = recorded.image_inputs()
    assert [descriptor.order for descriptor in descriptors] == [0, 1]
    assert [descriptor.message_index for descriptor in descriptors] == [0, 1]
    assert [descriptor.part_index for descriptor in descriptors] == [1, 0]
    assert [descriptor.media_type for descriptor in descriptors] == ["image/png", "image/png"]
    assert [descriptor.detail for descriptor in descriptors] == ["low", "high"]
    assert [descriptor.sha256 for descriptor in descriptors] == [first.sha256, second.sha256]
    assert len(descriptors) == 2

    blob = recorded.blob()
    assert "compare" in blob
    assert first.sha256 in blob
    assert second.sha256 in blob
    assert _FIRST_BASE64 not in blob
    assert _SECOND_BASE64 not in blob


def test_public_api_exports_image_input_and_openai_protocol_clients() -> None:
    """业务可从 taifeng 根包显式选择图片策略与两套 OpenAI 协议。"""
    from taifeng.llm.providers import OpenAICompatClient

    assert taifeng.ImageAttachmentV1.__name__ == "ImageAttachmentV1"
    assert taifeng.ImageInputPolicy.__name__ == "ImageInputPolicy"
    assert taifeng.TextPart is TextPart
    assert taifeng.ImagePart is ImagePart
    assert taifeng.OpenAIChatClient.__name__ == "OpenAIChatClient"
    assert taifeng.OpenAIResponsesClient.__name__ == "OpenAIResponsesClient"
    assert OpenAICompatClient.__module__ == "taifeng.llm.providers.openai_compat"
