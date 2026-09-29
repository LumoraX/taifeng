"""FilePart 在各 provider wire 上的形状与能力门控（llm-file-input 契约 § Provider 映射）。"""

from __future__ import annotations

import json

import pytest

from taifeng.llm.client import TEXT_ONLY_CAPABILITIES, ModelCapabilities, model_capabilities
from taifeng.llm.errors import InvalidHistoryError, UnsupportedModalityError
from taifeng.llm.file_input import FileAttachmentV1
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.providers.anthropic_provider import AnthropicClient, AnthropicSession
from taifeng.llm.providers.codex import CodexResponsesClient
from taifeng.llm.providers.codex.wire import build_codex_payload
from taifeng.llm.providers.gemini_provider import GeminiClient, GeminiSession
from taifeng.llm.providers.litellm_provider import LiteLLMClient, _to_litellm_messages
from taifeng.llm.providers.openai import OpenAIChatClient, OpenAIResponsesClient
from taifeng.llm.providers.openai._shared import tool_output_content
from taifeng.llm.providers.openai.chat import OpenAIChatSession
from taifeng.llm.providers.openai.responses import OpenAIResponsesSession
from taifeng.llm.providers.openai_compat import OpenAICompatClient, OpenAICompatSession
from taifeng.llm.types import (
    ApiFunctionCallItem,
    ApiFunctionCallOutputItem,
    ApiMessage,
    ApiMessageItem,
    ApiRequest,
    FilePart,
    ImagePart,
    TextPart,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.skill.eligibility import derive_modality_tags
from tests.pdf_fixtures import minimal_pdf

_PDF = minimal_pdf("wire")
_ATTACHMENT = FileAttachmentV1.from_bytes(_PDF, filename="note.pdf")
_FILE = _ATTACHMENT.to_part()
_UNNAMED = FileAttachmentV1.from_bytes(_PDF).to_part()
_DATA_URL = f"data:application/pdf;base64,{_ATTACHMENT.content}"


def _user_request(*parts: TextPart | FilePart | ImagePart) -> ApiRequest:
    """构造只有一条多模态 user 消息的 messages 视图请求。"""
    return ApiRequest(model="m", messages=[ApiMessage(role="user", content=list(parts))])


def _items_request(*parts: TextPart | FilePart) -> ApiRequest:
    """构造 Responses / Codex 使用的 ordered input_items 请求。"""
    return ApiRequest(model="m", input_items=[ApiMessageItem(role="user", content=list(parts))])


def _chat_session() -> OpenAIChatSession:
    """不发请求的官方 Chat session。"""
    return OpenAIChatSession(
        base_url="https://api.openai.com/v1", api_key="sk", model="gpt-5.6",
        cancel=CancellationToken(),
    )


def _responses_session() -> OpenAIResponsesSession:
    """不发请求的官方 Responses session。"""
    return OpenAIResponsesSession(
        base_url="https://api.openai.com/v1", api_key="sk", model="gpt-5.6",
        cancel=CancellationToken(),
    )


def _anthropic_session() -> AnthropicSession:
    """只组 payload 的 Anthropic session（关尾部缓存断点，只看块映射）。"""
    return AnthropicSession(
        api_key="sk", model="claude-x", base_url="https://api.anthropic.com",
        cancel=CancellationToken(), cache_tail=False,
    )


def _gemini_session() -> GeminiSession:
    """只组 payload 的 Gemini session。"""
    return GeminiSession(
        api_key="sk", model="gemini-test", base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )


# ---------------------------------------------------------------- OpenAI Chat


def test_chat_maps_file_part_to_file_data_url() -> None:
    payload = _chat_session()._build_payload(_user_request(TextPart(text="读"), _FILE))

    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "读"},
        {"type": "file", "file": {"file_data": _DATA_URL, "filename": "note.pdf"}},
    ]


def test_chat_uses_deterministic_default_filename() -> None:
    first = _chat_session()._build_payload(_user_request(_UNNAMED))
    second = _chat_session()._build_payload(_user_request(_UNNAMED))

    name = first["messages"][-1]["content"][0]["file"]["filename"]
    assert name == f"attachment-{_UNNAMED.sha256[:12]}.pdf"
    assert first == second


def test_chat_rejects_file_outside_user_message() -> None:
    request = ApiRequest(
        model="m", messages=[ApiMessage(role="assistant", content=[_FILE])]
    )
    with pytest.raises(InvalidHistoryError, match="user messages"):
        _chat_session()._build_payload(request)


# ------------------------------------------------------- Responses / Codex


def test_responses_maps_file_part_to_input_file() -> None:
    payload = _responses_session()._build_payload(_items_request(TextPart(text="读"), _FILE))

    assert payload["input"][-1]["content"] == [
        {"type": "input_text", "text": "读"},
        {"type": "input_file", "file_data": _DATA_URL, "filename": "note.pdf"},
    ]


def test_codex_maps_file_part_to_input_file() -> None:
    payload = build_codex_payload(_items_request(_FILE), default_model="m")

    assert payload["input"][-1]["content"] == [
        {"type": "input_file", "file_data": _DATA_URL, "filename": "note.pdf"}
    ]


@pytest.mark.parametrize("builder", ["responses", "codex"])
def test_responses_family_rejects_file_in_assistant_message(builder: str) -> None:
    request = ApiRequest(
        model="m",
        input_items=[ApiMessageItem(role="assistant", content=[_FILE], sample_id="s", output_index=0)],
    )
    with pytest.raises(InvalidHistoryError, match="only valid in user messages"):
        if builder == "responses":
            _responses_session()._build_payload(request)
        else:
            build_codex_payload(request, default_model="m")


def test_tool_output_rejects_file_part() -> None:
    """工具附件契约只有图片：function_call_output 里出现文件必须显式拒绝。"""
    with pytest.raises(InvalidHistoryError):
        tool_output_content([TextPart(text="x"), _FILE])
    request = ApiRequest(
        model="m",
        input_items=[
            ApiMessageItem(role="user", content="go"),
            ApiFunctionCallItem(call_id="c", name="t", arguments="{}", sample_id="s", output_index=0),
            ApiFunctionCallOutputItem(call_id="c", output=[_FILE], origin_sample_id="s"),
        ],
    )
    with pytest.raises(InvalidHistoryError):
        build_codex_payload(request, default_model="m")


# ------------------------------------------------------------- Anthropic


def test_anthropic_maps_file_part_to_document_block() -> None:
    payload = _anthropic_session()._build_payload(_user_request(TextPart(text="读"), _FILE))

    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "读"},
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": _ATTACHMENT.content,
            },
            "title": "note.pdf",
        },
    ]


def test_anthropic_document_omits_title_without_filename() -> None:
    payload = _anthropic_session()._build_payload(_user_request(_UNNAMED))

    assert "title" not in payload["messages"][-1]["content"][0]


def test_anthropic_still_rejects_images_and_non_user_files() -> None:
    image = ImagePart(
        media_type="image/png", base64_data="iVBORw0KGgo=", size=8, sha256="0" * 64
    )
    with pytest.raises(UnsupportedModalityError, match="image input"):
        _anthropic_session()._build_payload(_user_request(image))
    request = ApiRequest(model="m", messages=[ApiMessage(role="assistant", content=[_FILE])])
    with pytest.raises(InvalidHistoryError):
        _anthropic_session()._build_payload(request)


# ---------------------------------------------------------------- Gemini


def test_gemini_maps_file_part_to_inline_data() -> None:
    payload = _gemini_session()._build_payload(_user_request(TextPart(text="读"), _FILE))

    assert payload["contents"][-1]["parts"] == [
        {"text": "读"},
        {"inlineData": {"mimeType": "application/pdf", "data": _ATTACHMENT.content}},
    ]


def test_gemini_rejects_file_outside_user_message() -> None:
    request = ApiRequest(model="m", messages=[ApiMessage(role="assistant", content=[_FILE])])
    with pytest.raises(InvalidHistoryError):
        _gemini_session()._build_payload(request)


# ------------------------------------------------ text-only 门控（显式拒绝）


def test_openai_compat_rejects_file_before_serialization() -> None:
    session = OpenAICompatSession(
        base_url="https://compat.example/v1", api_key="sk", model="m", cancel=CancellationToken()
    )
    with pytest.raises(UnsupportedModalityError, match="file input"):
        session._build_payload(_user_request(TextPart(text="读"), _FILE))


def test_litellm_rejects_file_and_serializes_text_parts() -> None:
    with pytest.raises(UnsupportedModalityError, match="file input"):
        _to_litellm_messages(_user_request(_FILE))
    messages = _to_litellm_messages(_user_request(TextPart(text="甲"), TextPart(text="乙")))
    # 仅文本的 part 列表按 OpenAI content part 形状序列化，不把 pydantic 对象交给 LiteLLM
    assert messages[-1]["content"] == [
        {"type": "text", "text": "甲"},
        {"type": "text", "text": "乙"},
    ]
    json.dumps(messages)


# ------------------------------------------------------------ 能力声明


@pytest.mark.parametrize(
    ("client", "declares_file"),
    [
        (OpenAIChatClient(api_key="sk"), True),
        (OpenAIResponsesClient(api_key="sk"), True),
        (CodexResponsesClient(api_key="sk", base_url="https://proxy.example/v1"), True),
        (AnthropicClient(api_key="sk"), True),
        (GeminiClient(api_key="sk"), True),
        (OpenAICompatClient(base_url="https://compat.example/v1", api_key="sk", model="m"), False),
        (LiteLLMClient(model="openai/gpt-5.6"), False),
        (SimClient(turns=[SimTurn(text="x")]), False),
    ],
)
def test_file_capability_is_declared_only_by_dedicated_clients(
    client: object, declares_file: bool
) -> None:
    assert ("file" in model_capabilities(client).input_modalities) is declares_file


def test_file_capability_derives_input_file_tag() -> None:
    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text", "file"}), provider="x", protocol="y"
    )

    assert "input_file" in derive_modality_tags(capabilities)
    assert "input_file" not in derive_modality_tags(TEXT_ONLY_CAPABILITIES)


# ------------------------------------------------------------------ Sim


async def test_sim_records_file_descriptor_without_body() -> None:
    client = SimClient(
        turns=[SimTurn(text="ok")],
        capabilities=ModelCapabilities(
            input_modalities=frozenset({"text", "file"}), provider="sim", protocol="sim"
        ),
    )
    async with client.session(cancel=CancellationToken()) as session:
        _ = [event async for event in session.stream(_user_request(TextPart(text="读"), _FILE))]

    recorded = client.ledger.single_request()
    (descriptor,) = recorded.file_inputs()
    assert (descriptor.media_type, descriptor.filename, descriptor.size) == (
        "application/pdf", "note.pdf", len(_PDF),
    )
    assert descriptor.sha256 == _ATTACHMENT.sha256
    assert _ATTACHMENT.content not in recorded.blob()
    assert "<file media_type=application/pdf filename=note.pdf" in recorded.blob()
