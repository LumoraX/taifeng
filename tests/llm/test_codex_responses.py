"""Codex Responses 单次网络 session 与取消测试。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import InvalidResponseError
from taifeng.llm.providers.codex.responses import (
    CodexResponsesClient,
    CodexResponsesSession,
)
from taifeng.llm.types import ApiMessageItem, ApiRequest
from taifeng.loop.cancellation import CancellationToken


def _sse(*events: dict[str, object]) -> bytes:
    """编码 Codex SSE events。"""
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    )


def _events() -> tuple[dict[str, object], ...]:
    """构造代理实测形状的完整 text response。"""
    done = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "pong"}],
    }
    return (
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.in_progress", "response": {"id": "resp_1"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "pong",
        },
        {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "text": "pong",
        },
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "pong"},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "total_tokens": 6,
                },
            },
        },
    )


def _session(cancel: CancellationToken | None = None) -> CodexResponsesSession:
    """构造不主动发请求的 Codex session。"""
    return CodexResponsesSession(
        base_url="https://proxy.example/v1",
        api_key="sk-test",
        model="gpt-5.6-luna",
        cancel=cancel or CancellationToken(),
    )


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
) -> None:
    """把 session 内部 AsyncClient 指向 mock transport。"""
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


def test_codex_client_declares_independent_capabilities() -> None:
    """provider identity 不得伪装为 OpenAI。"""
    client = CodexResponsesClient(
        api_key="sk-test",
        model="gpt-5.6-luna",
        base_url="https://proxy.example/v1",
    )

    assert client.capabilities == ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="codex",
        protocol="responses",
        accepts_provider_state=True,
        tool_output_modalities=frozenset({"text", "image"}),
    )


@pytest.mark.asyncio
async def test_codex_posts_responses_and_emits_terminal_only_after_clean_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """done facts 只有通过 completed + clean EOF 才能成为 normalized output。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse(*_events()),
            headers={"x-request-id": "req_1"},
        )

    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    events = [
        event
        async for event in _session().stream(
            ApiRequest(
                model="gpt-5.6-luna",
                system_prompt=["system"],
                input_items=[ApiMessageItem(role="user", content="ping")],
            )
        )
    ]

    assert captured["url"] == "https://proxy.example/v1/responses"
    assert captured["body"]["instructions"] == "system"  # type: ignore[index]
    kinds = [event.kind for event in events]
    assert kinds.count("normalized_output") == 1
    assert kinds.count("completed") == 1
    assert kinds.index("normalized_output") < kinds.index("completed")
    normalized = next(
        event.data["items"] for event in events if event.kind == "normalized_output"
    )
    assert normalized[0]["text"] == "pong"
    completed = next(event for event in events if event.kind == "completed")
    assert completed.data["response_id"] == "resp_1"
    assert completed.data["request_id"] == "req_1"
    assert completed.data["usage"]["total_tokens"] == 6


@pytest.mark.asyncio
async def test_event_after_completed_rejects_without_normalized_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """completed 后的 JSON event 不能在已发布后才被发现。"""
    body = _sse(*_events(), {"type": "response.in_progress"})
    _patch_transport(
        monkeypatch,
        httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    observed = []

    with pytest.raises(InvalidResponseError, match="after response.completed"):
        async for event in _session().stream(
            ApiRequest(model="gpt-5.6-luna", input_items=[])
        ):
            observed.append(event)

    assert "normalized_output" not in [event.kind for event in observed]
    assert "completed" not in [event.kind for event in observed]


class _StalledBody(httpx.AsyncByteStream):
    """第一次 read 后永不自行结束的 body。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover
            yield b""


@pytest.mark.asyncio
async def test_codex_stalled_read_is_interrupted_by_cancel_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消必须抢占 HTTP read，不等待 provider timeout。"""
    body = _StalledBody()
    _patch_transport(
        monkeypatch,
        httpx.MockTransport(lambda _: httpx.Response(200, stream=body)),
    )
    cancel = CancellationToken(name="codex-stalled")

    async def consume() -> None:
        async for _ in _session(cancel).stream(
            ApiRequest(model="gpt-5.6-luna", input_items=[])
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(body.started.wait(), timeout=1)
    cancel.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


def test_strict_attempt_adapter_accepts_exact_codex_client() -> None:
    """仓库审查过的 exact one-attempt client 可进入 strict audit。"""
    inner = CodexResponsesClient(
        api_key="sk-test",
        model="gpt-5.6-luna",
        base_url="https://proxy.example/v1",
    )

    adapter = AttemptObservableClientAdapter(
        inner,
        provider="codex",
        default_model="gpt-5.6-luna",
    )

    assert adapter.capabilities == inner.capabilities


def test_strict_attempt_adapter_rejects_codex_subclass() -> None:
    """公开 marker 或继承不得自行取得 strict dispatch 资格。"""

    class ExternalCodexClient(CodexResponsesClient):
        """模拟未经仓库逐一审查的外部 subclass。"""

    inner = ExternalCodexClient(
        api_key="sk-test",
        model="gpt-5.6-luna",
        base_url="https://proxy.example/v1",
    )

    with pytest.raises(TypeError, match="reviewed one-attempt"):
        AttemptObservableClientAdapter(
            inner,
            provider="codex",
            default_model="gpt-5.6-luna",
        )
