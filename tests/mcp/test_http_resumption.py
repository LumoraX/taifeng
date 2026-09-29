"""streamable HTTP 断流续传与推送流重连（http_client.py + sse.py）。

规范（2025-06-18 §Transports「Resumability and Redelivery」）：server 可给 SSE 事件带 id；流断后
客户端 SHOULD 以 GET + ``Last-Event-ID`` 请求续传，server 可在同一条流上重放其后的消息。
server 不支持续传（从未给 id / GET 405）或续传次数用尽 → 显式失败，不静默等到超时。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from taifeng.mcp import McpHttpClient
from taifeng.mcp.bridge import McpToolError
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_SSE = {"content-type": "text/event-stream"}


def _event(msg: dict[str, Any], event_id: str | None = None) -> bytes:
    """一个 SSE 事件（可带 id）。"""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}data: {json.dumps(msg)}\n\n".encode()


def _result(mid: Any, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}


class _ResumeServer:
    """POST tools/call 的 SSE 流先推一条带 id 的通知再断开；GET 按脚本续传。

    Attributes:
        post_ids: POST 流里是否给事件带 id（False = server 不支持续传）。
        resume: 续传 GET 的响应工厂 ``(last_event_id, call_id) -> Response``。
        resume_ids: 每次续传 GET 带来的 ``Last-Event-ID``。
        posted: 收到的无 id POST（客户端应答 / 通知）。
    """

    def __init__(self, resume: Callable[[str | None, Any], httpx.Response], *,
                 post_ids: bool = True, clean_eof: bool = False) -> None:
        self.resume = resume
        self.post_ids = post_ids
        self.clean_eof = clean_eof
        self.resume_ids: list[str | None] = []
        self.posted: list[dict[str, Any]] = []
        self.call_id: Any = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200)
        if request.method == "GET":
            self.resume_ids.append(request.headers.get("last-event-id"))
            return self.resume(request.headers.get("last-event-id"), self.call_id)
        msg = json.loads(request.content)
        mid = msg.get("id")
        if mid is None or "method" not in msg:  # 通知 / 客户端对 server 请求的应答
            self.posted.append(msg)
            return httpx.Response(202)
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "resume"}}})
        self.call_id = mid
        return httpx.Response(200, headers=_SSE, content=self._broken_stream())

    async def _broken_stream(self) -> AsyncIterator[bytes]:
        note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}
        yield _event(note, "e1" if self.post_ids else None)
        if not self.clean_eof:
            raise httpx.ReadError("connection reset by peer")


async def _connect(server: _ResumeServer, **kwargs: Any) -> McpHttpClient:
    return await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server),
        listen_notifications=False, stream_resume_delay_seconds=0, **kwargs)


def _replay(mid: Any, *extra: bytes) -> httpx.Response:
    """续传流：先重放 extra 事件，再给出本次调用的响应。"""
    return httpx.Response(200, headers=_SSE, content=b"".join(
        [*extra, _event(_result(mid, "resumed"), "e3")]))


# ---------------------------------------------------------------- POST 流续传


@pytest.mark.parametrize("clean_eof", [False, True])
async def test_broken_post_stream_resumes_with_last_event_id(clean_eof: bool) -> None:
    """POST 流在响应前断开（或提前 EOF）→ GET 带 Last-Event-ID 续传拿到响应；
    续传流里的 server 请求照常路由应答。"""
    ping = _event({"jsonrpc": "2.0", "id": "srv-ping", "method": "ping"}, "e2")
    server = _ResumeServer(lambda _last, mid: _replay(mid, ping), clean_eof=clean_eof)
    client = await _connect(server)
    try:
        result = await client.call_tool("work", {})
        await wait_for_condition(lambda: any(p.get("id") == "srv-ping" for p in server.posted))
    finally:
        await client.close()
    assert result["content"][0]["text"] == "resumed"
    assert server.resume_ids == ["e1"]
    pong = next(p for p in server.posted if p.get("id") == "srv-ping")
    assert pong["result"] == {}


async def test_resume_retries_with_latest_event_id_until_cap() -> None:
    """续传流再次断开 → 用最新的事件 id 继续续传；用尽 max_stream_resumptions 次显式失败。"""
    attempt = 0

    def _flaky(_last: str | None, _mid: Any) -> httpx.Response:
        nonlocal attempt
        attempt += 1

        async def _body() -> AsyncIterator[bytes]:
            yield _event({"jsonrpc": "2.0", "method": "notifications/progress"}, f"r{attempt}")
            raise httpx.ReadError("reset again")

        return httpx.Response(200, headers=_SSE, content=_body())

    server = _ResumeServer(_flaky)
    client = await _connect(server, max_stream_resumptions=2)
    try:
        with pytest.raises(McpToolError, match="gave up after 2 resumption attempt"):
            await client.call_tool("work", {})
    finally:
        await client.close()
    assert server.resume_ids == ["e1", "r1"]


async def test_no_event_ids_fails_fast_without_resuming() -> None:
    """server 从未给事件 id（不支持续传）→ 立即显式失败，不发续传 GET。"""
    server = _ResumeServer(lambda _last, mid: _replay(mid), post_ids=False)
    client = await _connect(server)
    try:
        with pytest.raises(McpToolError, match="sent no event id to resume from"):
            await client.call_tool("work", {})
    finally:
        await client.close()
    assert server.resume_ids == []


async def test_resume_rejected_with_405_fails_explicitly() -> None:
    """续传 GET 回 405 → server 不支持续传，显式失败（不重试）。"""
    server = _ResumeServer(lambda _last, _mid: httpx.Response(405))
    client = await _connect(server)
    try:
        with pytest.raises(McpToolError, match="does not support resumption"):
            await client.call_tool("work", {})
    finally:
        await client.close()
    assert server.resume_ids == ["e1"]


async def test_zero_resumptions_disables_resume() -> None:
    """max_stream_resumptions=0 → 不续传，断流即显式失败。"""
    server = _ResumeServer(lambda _last, mid: _replay(mid))
    client = await _connect(server, max_stream_resumptions=0)
    try:
        with pytest.raises(McpToolError, match="gave up after 0 resumption"):
            await client.call_tool("work", {})
    finally:
        await client.close()
    assert server.resume_ids == []


def test_resumption_knobs_are_validated() -> None:
    """续传旋钮为负是配置错误，构造期拒绝。"""
    with pytest.raises(ValueError, match="max_stream_resumptions"):
        McpHttpClient("https://mcp.example/mcp", max_stream_resumptions=-1)
    with pytest.raises(ValueError, match="stream_resume_delay_seconds"):
        McpHttpClient("https://mcp.example/mcp", stream_resume_delay_seconds=-0.1)


# ---------------------------------------------------------------- GET 推送流重连


class _PushServer:
    """GET 推送流按脚本逐次回应；记录每次 GET 的 Last-Event-ID。"""

    def __init__(self, script: list[Callable[[], httpx.Response]]) -> None:
        self.script = script
        self.last_ids: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200)
        if request.method == "GET":
            self.last_ids.append(request.headers.get("last-event-id"))
            index = len(self.last_ids) - 1
            return self.script[index]() if index < len(self.script) else httpx.Response(405)
        msg = json.loads(request.content)
        if msg.get("id") is None:
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "serverInfo": {"name": "push"}}})


def _list_changed(event_id: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(200, headers=_SSE, content=_event(
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}, event_id))


async def _run_listener(server: _PushServer, **kwargs: Any) -> McpHttpClient:
    """连上并等推送流监听任务自行结束（放弃重连 / 405）。"""
    client = await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server),
        stream_resume_delay_seconds=0, **kwargs)
    assert client._listener_task is not None
    await asyncio.wait_for(asyncio.shield(client._listener_task), GUARD_TIMEOUT_SECONDS)
    return client


async def test_push_stream_reconnects_with_last_event_id() -> None:
    """server 关掉推送流 → 带 Last-Event-ID 重连，继续收通知；405 后停止。"""
    server = _PushServer([_list_changed("n1"), _list_changed("n2")])
    client = await _run_listener(server)
    await client.close()
    assert server.last_ids == [None, "n1", "n2"]


async def test_push_stream_gives_up_after_fruitless_reconnects() -> None:
    """重连连续无事件 → 达到 max_stream_resumptions 次即放弃（不无限重连）。"""
    empty = lambda: httpx.Response(200, headers=_SSE, content=b"")  # noqa: E731
    server = _PushServer([_list_changed("n1")] + [empty] * 10)
    client = await _run_listener(server, max_stream_resumptions=2)
    await client.close()
    # 首连有事件 → 之后 2 次重连都没有事件 → 放弃
    assert server.last_ids == [None, "n1", "n1"]


async def test_push_stream_non_sse_response_stops_without_retry() -> None:
    """推送流回 200 但不是 SSE（违反规范）→ 告警并停止，不重连。"""
    server = _PushServer([lambda: httpx.Response(200, json={"not": "sse"})])
    client = await _run_listener(server)
    await client.close()
    assert server.last_ids == [None]
