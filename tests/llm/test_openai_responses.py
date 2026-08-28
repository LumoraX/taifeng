"""OpenAI 官方 Responses 协议的图片 wire 与 terminal accumulator 测试。"""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import (
    InvalidHistoryError,
    InvalidResponseError,
    RequestTooLargeError,
)
from taifeng.llm.providers.openai._shared import MAX_REQUEST_BYTES_METADATA_KEY
from taifeng.llm.providers.openai.responses import (
    OpenAIResponsesClient,
    OpenAIResponsesSession,
    ResponsesAttemptAccumulator,
)
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
from taifeng.loop.cancellation import CancellationToken

_PNG = b"\x89PNG\r\n\x1a\nminimal"
_B64 = base64.b64encode(_PNG).decode("ascii")
_IMAGE = ImagePart(
    media_type="image/png",
    base64_data=_B64,
    size=len(_PNG),
    sha256=hashlib.sha256(_PNG).hexdigest(),
    detail="high",
)


def _session() -> OpenAIResponsesSession:
    """构造不发请求的 Responses session。"""
    return OpenAIResponsesSession(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        model="gpt-5.6",
        cancel=CancellationToken(),
    )


def test_responses_declares_stateful_image_capabilities() -> None:
    """Responses client 必须显式声明协议、图片与 provider-state 能力。"""
    client = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")

    assert client.capabilities == ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="openai",
        protocol="responses",
        accepts_provider_state=True,
    )


def test_audit_adapter_preserves_responses_capabilities() -> None:
    """strict audit 包装不得让 TurnRunner 退化成 text-only/unknown 协议。"""
    inner = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")

    adapter = AttemptObservableClientAdapter(
        inner, provider="openai", default_model="gpt-5.6"
    )

    assert adapter.capabilities == inner.capabilities


def test_responses_maps_ordered_items_images_tools_and_format() -> None:
    """Responses 使用 input_text/input_image、扁平 tool 与 text.format。"""
    state = ProviderStateEnvelope(
        provider="openai",
        protocol="responses",
        item_type="reasoning",
        payload={
            "id": "rs_old",
            "type": "reasoning",
            "encrypted_content": "ciphertext",
            "summary": [],
        },
    )
    request = ApiRequest(
        model="gpt-5.6",
        system_prompt=["你是检查员"],
        input_items=[
            ApiMessageItem(role="user", content=[TextPart(text="看图"), _IMAGE]),
            ApiProviderStateItem(
                sample_id="sample-old", output_index=0, state=state
            ),
            ApiFunctionCallItem(
                call_id="call-old",
                name="inspect",
                arguments='{"id":"A-17"}',
                sample_id="sample-old",
                output_index=1,
            ),
            ApiFunctionCallOutputItem(
                call_id="call-old", output='{"ok":true}', origin_sample_id="sample-old"
            ),
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
        reasoning_effort="medium",
    )

    payload = _session()._build_payload(request)

    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "previous_response_id" not in payload
    assert payload["input"][0]["role"] == "system"
    assert payload["input"][1]["content"] == [
        {"type": "input_text", "text": "看图"},
        {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{_B64}",
            "detail": "high",
        },
    ]
    assert payload["input"][2] == state.payload
    assert payload["input"][3]["type"] == "function_call"
    assert payload["input"][4]["type"] == "function_call_output"
    assert payload["tools"][0]["name"] == "inspect"
    assert "function" not in payload["tools"][0]
    assert "strict" not in payload["tools"][0]
    assert payload["text"]["format"]["name"] == "result"
    assert payload["reasoning"] == {"effort": "medium"}


def test_responses_guards_exact_final_utf8_json_bytes() -> None:
    """Responses 对包含 input_image 的最终 wire JSON 做精确字节门禁。"""
    request = ApiRequest(
        model="gpt-5.6",
        input_items=[ApiMessageItem(role="user", content=[TextPart(text="看图"), _IMAGE])],
    )
    payload = _session()._build_payload(request)
    exact = len(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )

    request.metadata[MAX_REQUEST_BYTES_METADATA_KEY] = exact
    assert _session()._build_payload(request) == payload

    request.metadata[MAX_REQUEST_BYTES_METADATA_KEY] = exact - 1
    with pytest.raises(RequestTooLargeError) as exc_info:
        _session()._build_payload(request)
    assert exc_info.value.estimated_bytes == exact
    assert exc_info.value.max_bytes == exact - 1


def test_responses_rejects_foreign_provider_state_before_network() -> None:
    """非 OpenAI Responses 状态不能静默发送或丢弃。"""
    request = ApiRequest(
        model="gpt-5.6",
        input_items=[
            ApiProviderStateItem(
                sample_id="sample-1",
                output_index=0,
                state=ProviderStateEnvelope(
                    provider="other",
                    protocol="responses",
                    item_type="reasoning",
                    payload={"id": "x"},
                ),
            )
        ],
    )

    with pytest.raises(InvalidHistoryError):
        _session()._build_payload(request)


def test_responses_terminal_output_indexes_must_be_increasing() -> None:
    """terminal list 的 provider output order 不能靠客户端排序掩盖。"""
    accumulator = ResponsesAttemptAccumulator()

    with pytest.raises(InvalidResponseError):
        accumulator.finalize(
            {
                "output": [
                    {
                        "type": "message",
                        "output_index": 1,
                        "content": [{"type": "output_text", "text": "later"}],
                    },
                    {
                        "type": "message",
                        "output_index": 0,
                        "content": [{"type": "output_text", "text": "earlier"}],
                    },
                ]
            }
        )


@pytest.mark.parametrize(("call_id", "name"), [("", "inspect"), ("call_1", "")])
def test_responses_terminal_function_call_requires_non_empty_identity(
    call_id: str,
    name: str,
) -> None:
    """terminal function call 必须先校验稳定身份，才能进入 durable history。"""
    accumulator = ResponsesAttemptAccumulator()

    with pytest.raises(InvalidResponseError):
        accumulator.finalize(
            {
                "output": [
                    {
                        "type": "function_call",
                        "output_index": 0,
                        "call_id": call_id,
                        "name": name,
                        "arguments": "{}",
                    }
                ]
            }
        )


def _sse(*events: dict[str, object]) -> bytes:
    """把 Responses event dict 编码为 SSE body。"""
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    )


def _completed_response(*, terminal_text: str = "库存 A-17") -> dict[str, object]:
    """构造包含 reasoning/message/function-call 的 terminal response。"""
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "model": "gpt-5.6-2026-08-01",
            "status": "completed",
            "output": [
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "检查图片"}],
                    "encrypted_content": "encrypted-state",
                    "status": "completed",
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": terminal_text}],
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "inspect",
                    "arguments": '{"id":"A-17"}',
                    "status": "completed",
                },
            ],
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "total_tokens": 28,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        },
    }


@pytest.mark.asyncio
async def test_responses_emits_one_normalized_output_before_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interleaved preview 最终收敛为唯一有序 normalized_output。"""
    body = _sse(
        {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.6"}},
        {"type": "response.output_item.added", "output_index": 2, "item": {"type": "function_call", "call_id": "call_1", "name": "inspect", "arguments": ""}},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "检查图片"},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "库存 A-17"},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"id":"A-17"}'},
        {"type": "response.function_call_arguments.done", "output_index": 2, "arguments": '{"id":"A-17"}'},
        _completed_response(),
    )
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    events = [
        event
        async for event in _session().stream(
            ApiRequest(
                model="gpt-5.6",
                input_items=[ApiMessageItem(role="user", content="inspect")],
            )
        )
    ]

    kinds = [event.kind for event in events]
    assert kinds.count("normalized_output") == 1
    assert kinds.index("normalized_output") < kinds.index("completed")
    assert kinds.count("tool_call_done") == 1
    normalized = next(event.data["items"] for event in events if event.kind == "normalized_output")
    assert [item["type"] for item in normalized] == ["reasoning", "message", "function_call"]
    assert [item["output_index"] for item in normalized] == [0, 1, 2]
    assert normalized[0]["state"]["payload"]["encrypted_content"] == "encrypted-state"
    assert normalized[1]["text"] == "库存 A-17"
    completed = next(event for event in events if event.kind == "completed")
    assert completed.data["usage"]["reasoning_tokens"] == 3


@pytest.mark.asyncio
async def test_responses_rejects_delta_terminal_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """preview delta 与 terminal bytes 不一致时不得 normalized/commit。"""
    body = _sse(
        {"type": "response.output_text.delta", "output_index": 1, "delta": "preview"},
        _completed_response(terminal_text="terminal"),
    )
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(InvalidResponseError):
        async for _ in _session().stream(
            ApiRequest(model="gpt-5.6", messages=[])
        ):
            pass
