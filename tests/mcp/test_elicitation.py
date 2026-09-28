"""MCP 客户端的 server → client 请求处理：elicitation 注入口、ping、未知方法、取消、关闭。

两种传输都测：stdio 用真实子进程 fake server，HTTP 用 MockTransport（SSE 流里夹带 server
请求，应答经另一次 POST 送回）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from taifeng.mcp import (
    ElicitationRequest,
    ElicitationResult,
    McpHttpClient,
    McpStdioClient,
)
from taifeng.mcp.elicitation import answer_elicitation
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

# ---------------------------------------------------------------- stdio fake server
#
# tools/call 名字即指令：
#   server_request {method, params, id?}  发一条 server 请求（缺 id 时复用本次 tools/call 的 id，
#                                         专测撞号），等到同 id 的应答后把应答 JSON 作为文本返回
#   elicit_pending {id}                   发 elicitation/create 后立即返回，不等应答
#   cancel {id}                           发 notifications/cancelled 撤回该请求
#   responses                             返回至今收到的全部应答（无 method 的消息）
_SERVER = r"""
import json, sys

responses = []
state = {}

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

def text_result(mid, text):
    send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})

def wait_response(rid):
    for line in sys.stdin:
        msg = json.loads(line)
        if "method" not in msg:
            responses.append(msg)
            if msg.get("id") == rid:
                return msg

for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method is None:
        responses.append(msg)
        continue
    if method == "initialize":
        state["capabilities"] = msg["params"]["capabilities"]
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18", "serverInfo": {"name": "elicit-fake"},
            "capabilities": {"tools": {}}}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments") or {}
        if name == "capabilities":
            text_result(mid, json.dumps(state["capabilities"]))
        elif name == "server_request":
            rid = args.get("id", mid)
            request = {"jsonrpc": "2.0", "id": rid, "method": args["method"]}
            if "params" in args:
                request["params"] = args["params"]
            send(request)
            text_result(mid, json.dumps(wait_response(rid)))
        elif name == "elicit_pending":
            send({"jsonrpc": "2.0", "id": args["id"], "method": "elicitation/create",
                  "params": {"message": "等一下", "requestedSchema": {"type": "object"}}})
            text_result(mid, "sent")
        elif name == "cancel":
            send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                  "params": {"requestId": args["id"], "reason": "server gave up"}})
            text_result(mid, "cancelled")
        elif name == "responses":
            text_result(mid, json.dumps(responses))
"""


def _server(tmp_path: Path) -> list[str]:
    path = tmp_path / "elicit_server.py"
    path.write_text(textwrap.dedent(_SERVER), encoding="utf-8")
    return [sys.executable, str(path)]


async def _server_request(client: McpStdioClient, **args: Any) -> dict[str, Any]:
    """让 fake server 发一条请求，返回它收到的客户端应答。"""
    result = await client.call_tool("server_request", args)
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


def _elicit_params(schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"message": "你的名字？", "requestedSchema": schema or SCHEMA}


# ---------------------------------------------------------------- ElicitationResult 契约


def test_result_rejects_invalid_shapes() -> None:
    """规范外动作 / decline 带 content / 非原始类型取值 → 构造期 ValueError。"""
    with pytest.raises(ValueError, match="action"):
        ElicitationResult(action="reject")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only allowed with action='accept'"):
        ElicitationResult(action="decline", content={"a": "b"})
    with pytest.raises(ValueError, match="primitive"):
        ElicitationResult(action="accept", content={"a": [1]})  # type: ignore[dict-item]


def test_result_wire_shape() -> None:
    """accept 带 content；decline / cancel 省略 content。"""
    assert ElicitationResult("accept", {"n": 1}).to_wire() == {
        "action": "accept", "content": {"n": 1}}
    assert ElicitationResult("decline").to_wire() == {"action": "decline"}
    assert ElicitationResult("cancel").to_wire() == {"action": "cancel"}


async def test_answer_validates_params_and_content_against_schema() -> None:
    """params 非法 → -32602；accept 的 content 违反 requestedSchema → -32603（不外发坏数据）。"""

    async def _bad_content(request: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult("accept", {"name": 42})

    bad_params = await answer_elicitation(
        7, {"message": "m", "requestedSchema": {"type": "array"}}, _bad_content, {})
    assert bad_params["error"]["code"] == -32602
    violated = await answer_elicitation(7, _elicit_params(), _bad_content, {})
    assert violated["error"]["code"] == -32603
    assert "$.name: expected string" in violated["error"]["message"]


# ---------------------------------------------------------------- stdio


async def test_stdio_declares_capability_only_when_handler_injected(tmp_path: Path) -> None:
    """注入 handler → initialize 声明 elicitation；未注入 → 不声明。"""

    async def _handler(request: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult("cancel")

    for handler, expected in ((_handler, {"elicitation": {}}), (None, {})):
        client = await McpStdioClient.spawn(_server(tmp_path), elicitation_handler=handler)
        try:
            result = await client.call_tool("capabilities", {})
            assert json.loads(result["content"][0]["text"]) == expected
        finally:
            await client.close()


async def test_stdio_elicitation_accept_and_decline(tmp_path: Path) -> None:
    """handler 收到 message / schema / serverInfo；accept 与 decline 按规范回 result。"""
    seen: list[ElicitationRequest] = []
    answers = iter([ElicitationResult("accept", {"name": "octocat"}),
                    ElicitationResult("decline")])

    async def _handler(request: ElicitationRequest) -> ElicitationResult:
        seen.append(request)
        return next(answers)

    client = await McpStdioClient.spawn(_server(tmp_path), elicitation_handler=_handler)
    try:
        accepted = await _server_request(
            client, method="elicitation/create", params=_elicit_params(), id="e1")
        declined = await _server_request(
            client, method="elicitation/create", params=_elicit_params(), id="e2")
    finally:
        await client.close()
    assert accepted == {"jsonrpc": "2.0", "id": "e1",
                        "result": {"action": "accept", "content": {"name": "octocat"}}}
    assert declined["result"] == {"action": "decline"}
    assert seen[0].message == "你的名字？"
    assert seen[0].requested_schema == SCHEMA
    assert seen[0].server_info["name"] == "elicit-fake"


async def test_stdio_server_request_id_colliding_with_client_id(tmp_path: Path) -> None:
    """server 请求复用本端 tools/call 的 id → 仍按请求路由，不被误当成响应。"""

    async def _handler(request: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult("accept", {"name": "x"})

    client = await McpStdioClient.spawn(_server(tmp_path), elicitation_handler=_handler)
    try:
        reply = await _server_request(  # 不传 id：fake server 复用本次 tools/call 的 id
            client, method="elicitation/create", params=_elicit_params())
    finally:
        await client.close()
    assert isinstance(reply["id"], int)
    assert reply["result"]["action"] == "accept"


async def test_stdio_without_handler_answers_method_not_found(tmp_path: Path) -> None:
    """未注入 handler 而 server 仍发 elicitation → -32601，不静默忽略。"""
    client = await McpStdioClient.spawn(_server(tmp_path))
    try:
        reply = await _server_request(
            client, method="elicitation/create", params=_elicit_params(), id="e1")
    finally:
        await client.close()
    assert reply["error"]["code"] == -32601
    assert "elicitation/create" in reply["error"]["message"]


async def test_stdio_handler_exception_becomes_error_and_client_survives(tmp_path: Path) -> None:
    """handler 抛异常 → -32603（只含异常类型，不外泄消息）；连接照常可用。"""

    async def _boom(request: ElicitationRequest) -> ElicitationResult:
        raise RuntimeError("宿主内部细节")

    client = await McpStdioClient.spawn(_server(tmp_path), elicitation_handler=_boom)
    try:
        reply = await _server_request(
            client, method="elicitation/create", params=_elicit_params(), id="e1")
        assert reply["error"]["code"] == -32603
        assert "RuntimeError" in reply["error"]["message"]
        assert "宿主内部细节" not in reply["error"]["message"]
        pong = await _server_request(client, method="ping", id="p1")
        assert pong == {"jsonrpc": "2.0", "id": "p1", "result": {}}
    finally:
        await client.close()


async def test_stdio_unknown_server_request_is_method_not_found(tmp_path: Path) -> None:
    """未实现的 server 请求（如 sampling）→ -32601。"""
    client = await McpStdioClient.spawn(_server(tmp_path))
    try:
        reply = await _server_request(client, method="sampling/createMessage", id="s1")
    finally:
        await client.close()
    assert reply["error"]["code"] == -32601


async def test_stdio_server_cancellation_cancels_handler_without_reply(tmp_path: Path) -> None:
    """server 发 notifications/cancelled → 在等的 handler 被取消，且不回任何响应。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _waiting(request: ElicitationRequest) -> ElicitationResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    client = await McpStdioClient.spawn(_server(tmp_path), elicitation_handler=_waiting)
    try:
        await client.call_tool("elicit_pending", {"id": "e9"})
        await asyncio.wait_for(started.wait(), GUARD_TIMEOUT_SECONDS)
        await client.call_tool("cancel", {"id": "e9"})
        await asyncio.wait_for(cancelled.wait(), GUARD_TIMEOUT_SECONDS)
        received = json.loads((await client.call_tool("responses", {}))["content"][0]["text"])
    finally:
        await client.close()
    assert all(r.get("id") != "e9" for r in received)


async def test_stdio_close_interrupts_waiting_handler(tmp_path: Path) -> None:
    """客户端 close() 打断仍在等用户的 handler（不会挂住关闭）。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _waiting(request: ElicitationRequest) -> ElicitationResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    client = await McpStdioClient.spawn(_server(tmp_path), elicitation_handler=_waiting)
    await client.call_tool("elicit_pending", {"id": "e1"})
    await asyncio.wait_for(started.wait(), GUARD_TIMEOUT_SECONDS)
    await asyncio.wait_for(client.close(), GUARD_TIMEOUT_SECONDS)
    assert cancelled.is_set()


# ---------------------------------------------------------------- HTTP


class _ElicitHttpServer:
    """streamable HTTP fake：tools/call 的 SSE 流里先发 server 请求，收到应答 POST 后再给结果。"""

    def __init__(self) -> None:
        self.capabilities: dict[str, Any] | None = None
        self.responses: dict[Any, dict[str, Any]] = {}
        self._arrived: dict[Any, asyncio.Event] = {}
        self.cancel_now = asyncio.Event()

    def _event(self, rid: Any) -> asyncio.Event:
        return self._arrived.setdefault(rid, asyncio.Event())

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        if "method" not in msg:  # 客户端对 server 请求的应答
            self.responses[msg.get("id")] = msg
            self._event(msg.get("id")).set()
            return httpx.Response(202)
        mid = msg.get("id")
        if mid is None:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            self.capabilities = msg["params"]["capabilities"]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "http-elicit"}}})
        args = msg["params"].get("arguments") or {}
        stream = (self._ask(mid, args) if msg["params"]["name"] == "ask"
                  else self._ask_then_cancel(mid))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    @staticmethod
    def _sse(msg: dict[str, Any]) -> bytes:
        return f"event: message\ndata: {json.dumps(msg)}\n\n".encode()

    async def _ask(self, mid: Any, args: dict[str, Any]) -> AsyncIterator[bytes]:
        """发 server 请求 → 等客户端 POST 回应答 → 把应答作为 tools/call 结果文本。"""
        rid = args.get("id", mid)
        yield self._sse({"jsonrpc": "2.0", "id": rid, "method": args["method"],
                         "params": args.get("params", {})})
        await asyncio.wait_for(self._event(rid).wait(), GUARD_TIMEOUT_SECONDS)
        yield self._sse({"jsonrpc": "2.0", "id": mid, "result": {"content": [
            {"type": "text", "text": json.dumps(self.responses[rid])}]}})

    async def _ask_then_cancel(self, mid: Any) -> AsyncIterator[bytes]:
        """发 elicitation → 等测试放行 → 发 cancelled 撤回 → 给结果。"""
        yield self._sse({"jsonrpc": "2.0", "id": "srv-c", "method": "elicitation/create",
                         "params": _elicit_params()})
        await asyncio.wait_for(self.cancel_now.wait(), GUARD_TIMEOUT_SECONDS)
        yield self._sse({"jsonrpc": "2.0", "method": "notifications/cancelled",
                         "params": {"requestId": "srv-c"}})
        yield self._sse({"jsonrpc": "2.0", "id": mid, "result": {"content": []}})


async def _http_client(server: _ElicitHttpServer, handler: Any) -> McpHttpClient:
    return await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server),
        elicitation_handler=handler)


async def _http_ask(client: McpHttpClient, **args: Any) -> dict[str, Any]:
    result = await client.call_tool("ask", args)
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


async def test_http_elicitation_accept_via_sse_and_post_back() -> None:
    """SSE 流内的 elicitation/create → handler → 应答 POST 回端点 → server 继续给出结果。"""

    async def _handler(request: ElicitationRequest) -> ElicitationResult:
        assert request.server_info["name"] == "http-elicit"
        return ElicitationResult("accept", {"name": "octocat"})

    server = _ElicitHttpServer()
    client = await _http_client(server, _handler)
    try:
        assert server.capabilities == {"elicitation": {}}
        reply = await _http_ask(
            client, method="elicitation/create", params=_elicit_params(), id="srv-1")
    finally:
        await client.close()
    assert reply["result"] == {"action": "accept", "content": {"name": "octocat"}}


async def test_http_without_handler_and_handler_error() -> None:
    """未注入 → 不声明能力且回 -32601；handler 抛错 → -32603；ping 照常应答。"""
    server = _ElicitHttpServer()
    client = await _http_client(server, None)
    try:
        assert server.capabilities == {}
        reply = await _http_ask(
            client, method="elicitation/create", params=_elicit_params(), id="srv-1")
        assert reply["error"]["code"] == -32601
    finally:
        await client.close()

    async def _boom(request: ElicitationRequest) -> ElicitationResult:
        raise ValueError("secret")

    server = _ElicitHttpServer()
    client = await _http_client(server, _boom)
    try:
        reply = await _http_ask(
            client, method="elicitation/create", params=_elicit_params(), id="srv-2")
        assert reply["error"]["code"] == -32603
        assert "secret" not in reply["error"]["message"]
        pong = await _http_ask(client, method="ping", id="srv-3")
        assert pong["result"] == {}
    finally:
        await client.close()


async def test_http_server_cancellation_cancels_handler() -> None:
    """同一 SSE 流里的 notifications/cancelled → handler 被取消，不 POST 应答。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _waiting(request: ElicitationRequest) -> ElicitationResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    server = _ElicitHttpServer()
    client = await _http_client(server, _waiting)
    try:
        call = asyncio.create_task(client.call_tool("ask_then_cancel", {}))
        await asyncio.wait_for(started.wait(), GUARD_TIMEOUT_SECONDS)
        server.cancel_now.set()
        await asyncio.wait_for(call, GUARD_TIMEOUT_SECONDS)
        await wait_for_condition(cancelled.is_set)
    finally:
        await client.close()
    assert "srv-c" not in server.responses
