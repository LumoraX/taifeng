"""客户端放弃请求时发 ``notifications/cancelled``（cancellation.py + stdio / HTTP 客户端 + 桥）。

规范（2025-06-18 §Cancellation / §Lifecycle「Timeouts」）：请求超时未得响应，发送方 SHOULD 发
``notifications/cancelled``（``requestId`` + ``reason``）并停止等待；``initialize`` MUST NOT 取消；
取消后迟到的响应 SHOULD 忽略。
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from taifeng.loop.cancellation import CancellationToken, CancelReason
from taifeng.mcp import McpHttpClient, McpStdioClient, bind_mcp_tools
from taifeng.mcp.bridge import McpToolError
from taifeng.mcp.cancellation import (
    DEFAULT_CANCEL_REASON,
    CancelNotifier,
    await_or_abandon,
    cancel_reason,
    cancelled_notification,
)
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.spec import ToolContext
from tests.conftest import GUARD_TIMEOUT_SECONDS, guard_ticks, wait_for_condition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


# ---------------------------------------------------------------- 纯函数 / 发送器


def test_notification_shape_and_reason_truncation() -> None:
    """报文带 requestId 与 reason；reason 超长截断（它会发给未必可信的 server）。"""
    msg = cancelled_notification(7, "x" * 500)
    assert msg["method"] == "notifications/cancelled"
    assert msg["params"]["requestId"] == 7
    assert len(msg["params"]["reason"]) == 200
    assert "id" not in msg  # 通知没有 id


def test_cancel_reason_uses_message_or_default() -> None:
    """取消消息即原因；无消息 / 非字符串 → 默认文案。"""
    assert cancel_reason(asyncio.CancelledError("client timeout after 3s")) == "client timeout after 3s"
    assert cancel_reason(asyncio.CancelledError()) == DEFAULT_CANCEL_REASON
    assert cancel_reason(asyncio.CancelledError(42)) == DEFAULT_CANCEL_REASON


async def test_notifier_skips_initialize_and_survives_send_failure() -> None:
    """initialize 不发取消（规范 MUST NOT）；发送失败只记日志，不外抛。"""
    sent: list[dict[str, Any]] = []

    async def _send(msg: dict[str, Any]) -> None:
        if msg["params"]["requestId"] == "boom":
            raise RuntimeError("pipe closed")
        sent.append(msg)

    notifier = CancelNotifier(_send)
    notifier.notify(1, "initialize", "timeout")
    notifier.notify("boom", "tools/call", "timeout")
    notifier.notify(2, "tools/call", "timeout")
    await wait_for_condition(lambda: len(sent) == 1)
    await notifier.aclose()
    assert [m["params"]["requestId"] for m in sent] == [2]


async def test_notifier_aclose_cancels_pending_sends() -> None:
    """关闭时尚未发出的通知被取消，aclose 不挂住。"""
    started = asyncio.Event()

    async def _stuck(msg: dict[str, Any]) -> None:
        started.set()
        await asyncio.Event().wait()

    notifier = CancelNotifier(_stuck)
    notifier.notify(1, "tools/call", "timeout")
    await asyncio.wait_for(started.wait(), GUARD_TIMEOUT_SECONDS)
    await asyncio.wait_for(notifier.aclose(), GUARD_TIMEOUT_SECONDS)


class _Recorder:
    """记录被打断时收到的取消原因。"""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.started = asyncio.Event()

    async def hang(self) -> str:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            self.reason = cancel_reason(exc)
            raise
        return "unreachable"


async def test_await_or_abandon_returns_result_in_time() -> None:
    """按时完成 → 原样返回；调用自身的异常原样上抛。"""

    async def _ok() -> str:
        return "done"

    async def _fail() -> str:
        raise McpToolError(-32602, "bad")

    assert await await_or_abandon(_ok(), timeout_seconds=5, cancel=CancellationToken()) == "done"
    with pytest.raises(McpToolError, match="bad"):
        await await_or_abandon(_fail(), timeout_seconds=5, cancel=CancellationToken())


async def test_await_or_abandon_timeout_interrupts_with_reason() -> None:
    """超时 → 在飞调用收到带「超时秒数」的取消，调用方得 TimeoutError。"""
    rec = _Recorder()
    with pytest.raises(TimeoutError, match="client timeout after 0.05s"):
        await await_or_abandon(rec.hang(), timeout_seconds=0.05, cancel=CancellationToken())
    assert rec.reason == "client timeout after 0.05s"


async def test_await_or_abandon_token_cancel_raises_cancelled() -> None:
    """token 取消 → 在飞调用收到含取消原因的取消，调用方得 CancelledError（交运行时收敛）。"""
    rec = _Recorder()
    token = CancellationToken()
    call = asyncio.create_task(
        await_or_abandon(rec.hang(), timeout_seconds=GUARD_TIMEOUT_SECONDS, cancel=token))
    await asyncio.wait_for(rec.started.wait(), GUARD_TIMEOUT_SECONDS)
    token.cancel(CancelReason.REQUESTED, "user stop")
    with pytest.raises(asyncio.CancelledError):
        await call
    assert rec.reason == "client cancelled the request (requested: user stop)"


async def test_await_or_abandon_outer_cancel_interrupts_call() -> None:
    """外部取消本函数（如运行时外层超时）→ 在飞调用同样被打断。"""
    rec = _Recorder()
    call = asyncio.create_task(await_or_abandon(
        rec.hang(), timeout_seconds=GUARD_TIMEOUT_SECONDS, cancel=CancellationToken()))
    await asyncio.wait_for(rec.started.wait(), GUARD_TIMEOUT_SECONDS)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await wait_for_condition(lambda: rec.reason is not None)
    assert rec.reason == "client cancelled the request (caller cancelled)"


# ---------------------------------------------------------------- stdio

# tools/call "hang" 不回响应（记一条 call-received 供测试确认请求已送达）；"late" 同样不回，
# 但收到对它的取消后仍补发一个迟到响应；"echo" 立即回；"received" 返回至今收到的全部通知
_CANCEL_SERVER = r"""
import json, sys

notes = []
late = set()

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        notes.append(msg)
        rid = (msg.get("params") or {}).get("requestId")
        if method == "notifications/cancelled" and rid in late:
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": []}})
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18", "serverInfo": {"name": "cancel-fake"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "hang", "inputSchema": {"type": "object"}}]}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name in ("hang", "late"):
            notes.append({"method": "call-received", "params": {"id": mid}})
        if name == "late":
            late.add(mid)
        elif name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": str(mid)}]}})
        elif name == "received":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": json.dumps(notes)}]}})
"""


def _cancel_server(tmp_path: Path) -> list[str]:
    path = tmp_path / "cancel_server.py"
    path.write_text(textwrap.dedent(_CANCEL_SERVER), encoding="utf-8")
    return [sys.executable, str(path)]


async def _notes(client: McpStdioClient, method: str) -> list[dict[str, Any]]:
    """server 至今收到的指定方法通知的 params（call-received 为 fake server 自记的送达标记）。"""
    result = await client.call_tool("received", {})
    notes = json.loads(result["content"][0]["text"])
    return [n["params"] for n in notes if n["method"] == method]


async def _wait_notes(client: McpStdioClient, method: str, count: int) -> list[dict[str, Any]]:
    """等 server 收到 count 条指定通知（取消通知以后台任务发出，需等真实状态）。"""
    notes: list[dict[str, Any]] = []
    async for _ in guard_ticks():
        notes = await _notes(client, method)
        if len(notes) >= count:
            return notes
    raise AssertionError(f"server 未在守卫期限内收到 {count} 条 {method}：{notes}")


async def test_stdio_timeout_sends_cancelled_with_request_id(tmp_path: Path) -> None:
    """stdio 客户端超时 → McpToolError，且 server 收到带同一 requestId 的取消通知。"""
    client = await McpStdioClient.spawn(_cancel_server(tmp_path), request_timeout_seconds=0.2)
    try:
        with pytest.raises(McpToolError, match="request timeout: tools/call"):
            await client.call_tool("hang", {})
        cancels = await _wait_notes(client, "notifications/cancelled", 1)
    finally:
        await client.close()
    # initialize=1，hang=2
    assert cancels == [{"requestId": 2, "reason": "client timeout after 0.2s"}]


async def test_stdio_completed_calls_send_no_cancellation(tmp_path: Path) -> None:
    """按时完成的请求（含 initialize）不发任何取消通知。"""
    client = await McpStdioClient.spawn(_cancel_server(tmp_path))
    try:
        await client.call_tool("echo", {})
        assert await _notes(client, "notifications/cancelled") == []
    finally:
        await client.close()


async def test_stdio_late_response_after_cancel_is_ignored(tmp_path: Path) -> None:
    """取消后 server 仍补发的迟到响应被忽略，连接照常可用。"""
    client = await McpStdioClient.spawn(_cancel_server(tmp_path), request_timeout_seconds=0.2)
    try:
        with pytest.raises(McpToolError):
            await client.call_tool("late", {})
        await _wait_notes(client, "notifications/cancelled", 1)
        echoed = await client.call_tool("echo", {})
    finally:
        await client.close()
    assert echoed["content"][0]["text"].isdigit()


async def test_stdio_bridge_token_cancel_notifies_server(tmp_path: Path) -> None:
    """桥：turn 取消（ctx.cancel）→ handler 以 CancelledError 结束，server 收到含原因的取消。"""
    client = await McpStdioClient.spawn(_cancel_server(tmp_path))
    registry = ToolRegistry()
    token = CancellationToken()
    try:
        await bind_mcp_tools(client, registry, watch=False, timeout_seconds=GUARD_TIMEOUT_SECONDS)
        ctx = ToolContext(call_id="c1", cancel=token, thread_id="t1")
        call = asyncio.create_task(registry.require("hang").handler({}, ctx))
        # 等请求真正送达再取消（未写出的请求按规范不发取消）
        await _wait_notes(client, "call-received", 1)
        token.cancel(CancelReason.SHUTDOWN)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, GUARD_TIMEOUT_SECONDS)
        cancels = await _wait_notes(client, "notifications/cancelled", 1)
    finally:
        await client.close()
    assert cancels[0]["reason"] == "client cancelled the request (shutdown)"


async def test_stdio_bridge_timeout_reports_timeout_reason(tmp_path: Path) -> None:
    """桥超时 → mcp_timeout 结果；server 收到的原因是桥的超时，而非笼统的取消。"""
    client = await McpStdioClient.spawn(_cancel_server(tmp_path), request_timeout_seconds=None)
    registry = ToolRegistry()
    try:
        await bind_mcp_tools(client, registry, watch=False, timeout_seconds=0.2)
        ctx = ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")
        result = await registry.require("hang").handler({}, ctx)
        cancels = await _wait_notes(client, "notifications/cancelled", 1)
    finally:
        await client.close()
    assert result.is_error and result.data["reason"] == "timeout"
    assert cancels[0]["reason"] == "client timeout after 0.2s"


# ---------------------------------------------------------------- HTTP


class _CancelHttpServer:
    """streamable HTTP fake：tools/call "hang" 的 SSE 流永不给响应；记录收到的通知。"""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.hanging: list[Any] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method in ("GET", "DELETE"):
            return httpx.Response(405)
        msg = json.loads(request.content)
        mid = msg.get("id")
        if mid is None:
            self.notes.append(msg)
            return httpx.Response(202)
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "http-cancel"}}})
        if msg["params"]["name"] == "echo":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid,
                                             "result": {"content": []}})
        self.hanging.append(mid)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=self._hang())

    @staticmethod
    async def _hang() -> AsyncIterator[bytes]:
        yield b": keep-alive\n\n"
        await asyncio.Event().wait()

    def cancels(self) -> list[dict[str, Any]]:
        return [n["params"] for n in self.notes if n["method"] == "notifications/cancelled"]


async def test_http_timeout_posts_cancelled() -> None:
    """HTTP 客户端超时 → McpToolError，另发一次 POST 送取消通知（带同一 requestId）。"""
    server = _CancelHttpServer()
    client = await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server),
        request_timeout_seconds=0.2)
    try:
        with pytest.raises(McpToolError, match="request timeout: tools/call"):
            await client.call_tool("hang", {})
        await wait_for_condition(lambda: bool(server.cancels()))
    finally:
        await client.close()
    assert server.cancels() == [{"requestId": 2, "reason": "client timeout after 0.2s"}]


async def test_http_caller_cancel_posts_cancelled_and_completion_does_not() -> None:
    """HTTP：按时完成不发取消；调用方取消（task.cancel(msg)）→ 取消消息即原因。"""
    server = _CancelHttpServer()
    client = await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server))
    try:
        await client.call_tool("echo", {})
        assert server.cancels() == []
        call = asyncio.create_task(client.call_tool("hang", {}))
        await wait_for_condition(lambda: bool(server.hanging))
        call.cancel("host gave up")
        with pytest.raises(asyncio.CancelledError):
            await call
        await wait_for_condition(lambda: bool(server.cancels()))
    finally:
        await client.close()
    assert server.cancels() == [{"requestId": 3, "reason": "host gave up"}]
