"""独立 Codex Responses provider 的请求 wire 测试。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from taifeng.llm.errors import InvalidHistoryError, RequestTooLargeError
from taifeng.llm.providers.codex.wire import build_codex_payload
from taifeng.llm.providers.openai._shared import MAX_REQUEST_BYTES_METADATA_KEY
from taifeng.llm.types import (
    ApiFunctionCallItem,
    ApiFunctionCallOutputItem,
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    ProviderStateEnvelope,
    ResponseFormatSpec,
    TextPart,
    ToolSpecRef,
)

_PNG = b"\x89PNG\r\n\x1a\nminimal"
_B64 = base64.b64encode(_PNG).decode("ascii")
_IMAGE = ImagePart(
    media_type="image/png",
    base64_data=_B64,
    size=len(_PNG),
    sha256=hashlib.sha256(_PNG).hexdigest(),
    detail="high",
)


def _state(provider: str = "codex") -> ApiProviderStateItem:
    """构造 provider-state replay item。"""
    return ApiProviderStateItem(
        sample_id="sample-1",
        output_index=0,
        state=ProviderStateEnvelope(
            provider=provider,
            protocol="responses",
            item_type="reasoning",
            payload={
                "id": "rs_1",
                "type": "reasoning",
                "encrypted_content": "ciphertext",
                "summary": [],
            },
        ),
    )


def test_codex_uses_top_level_instructions_and_ordered_list_input() -> None:
    """system guidance 不得成为代理拒绝的 role=system item。"""
    request = ApiRequest(
        model="gpt-5.6-luna",
        system_prompt=["", " first ", "second"],
        input_items=[
            ApiMessageItem(
                role="user",
                content=[TextPart(text="inspect"), _IMAGE],
            ),
            _state(),
            ApiFunctionCallItem(
                call_id="call-1",
                name="inspect",
                arguments='{"id":"A-17"}',
                sample_id="sample-1",
                output_index=1,
            ),
            ApiFunctionCallOutputItem(
                call_id="call-1",
                output='{"ok":true}',
                origin_sample_id="sample-1",
            ),
        ],
    )

    payload = build_codex_payload(request, default_model="fallback")

    assert payload["instructions"] == " first \n\nsecond"
    assert isinstance(payload["input"], list)
    assert all(item.get("role") != "system" for item in payload["input"])
    assert payload["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{_B64}",
        "detail": "high",
    }
    assert payload["input"][1] == _state().state.payload
    assert payload["input"][2]["type"] == "function_call"
    assert payload["input"][3]["type"] == "function_call_output"


def test_codex_folds_runtime_system_items_into_top_level_instructions() -> None:
    """budget/memory/compaction 等动态 system item 走 instructions，不落 input。"""
    request = ApiRequest(
        model="gpt-5.6-luna",
        system_prompt=["base"],
        input_items=[
            ApiMessageItem(role="user", content="first"),
            ApiMessageItem(role="system", content="runtime budget hint"),
            ApiMessageItem(role="assistant", content="ack"),
        ],
    )

    payload = build_codex_payload(request, default_model="fallback")

    assert payload["instructions"] == "base\n\nruntime budget hint"
    assert [item["role"] for item in payload["input"]] == ["user", "assistant"]


def test_codex_omits_empty_instructions_and_maps_tools_and_format() -> None:
    """可选字段必须使用 Codex/Responses 精确形状。"""
    request = ApiRequest(
        model="gpt-5.6-luna",
        system_prompt=[""],
        input_items=[ApiMessageItem(role="user", content="ping")],
        tools=[
            ToolSpecRef(
                name="inspect",
                description="检查库存",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        parallel_tool_calls=False,
        response_format=ResponseFormatSpec(
            name="result",
            json_schema={"type": "object", "properties": {}},
            strict=True,
        ),
        reasoning_effort="medium",
        max_output_tokens=123,
        temperature=0.2,
    )

    payload = build_codex_payload(request, default_model="fallback")

    assert "instructions" not in payload
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "inspect",
            "description": "检查库存",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    assert payload["parallel_tool_calls"] is False
    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "result",
        "schema": {"type": "object", "properties": {}},
        "strict": True,
    }
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["max_output_tokens"] == 123
    assert payload["temperature"] == 0.2


def test_codex_rejects_openai_state_and_unapproved_state_fields() -> None:
    """Codex state 必须 exact-match identity 和 payload 白名单。"""
    foreign = ApiRequest(model="m", input_items=[_state("openai")])
    with pytest.raises(InvalidHistoryError, match="foreign provider state"):
        build_codex_payload(foreign, default_model="m")

    bad = _state().model_copy(
        update={
            "state": _state().state.model_copy(
                update={"payload": {**_state().state.payload, "secret": "x"}}
            )
        }
    )
    with pytest.raises(InvalidHistoryError, match="invalid Codex reasoning"):
        build_codex_payload(
            ApiRequest(model="m", input_items=[bad]),
            default_model="m",
        )


def test_codex_rejects_non_user_image_and_exact_oversize_wire() -> None:
    """非法 role 与最终 JSON bytes 都必须在网络前拒绝。"""
    with pytest.raises(InvalidHistoryError, match="user messages"):
        build_codex_payload(
            ApiRequest(
                model="m",
                input_items=[ApiMessageItem(role="assistant", content=[_IMAGE])],
            ),
            default_model="m",
        )

    request = ApiRequest(
        model="m",
        input_items=[ApiMessageItem(role="user", content=[_IMAGE])],
        metadata={MAX_REQUEST_BYTES_METADATA_KEY: 1},
    )
    with pytest.raises(RequestTooLargeError):
        build_codex_payload(request, default_model="m")
