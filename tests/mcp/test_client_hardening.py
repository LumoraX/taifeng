"""MCP 传输边界：长帧、握手取消、清理错误与远端形状。"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import httpx
import pytest

from taifeng.mcp import McpHttpClient, McpStdioClient
from taifeng.mcp.bridge import McpToolError
from taifeng.mcp.http_client import _decode_message
from tests.mcp.test_mcp import fake_server as fake_server

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("executor", [False, True])
async def test_stdio_accepts_frames_larger_than_default_pipe_limit(
    fake_server: Path, executor: bool,
) -> None:
    """直接启动与注入本地执行器均可读超过 64 KiB 的合法帧。"""
    from taifeng.tool.command_executor import LocalCommandExecutor

    source = fake_server.read_text().replace(
        'if name == "echo":',
        'if name == "large":\n                reply(msg_id, {"content": [{"type": "text", "text": "x" * 100000}]})\n            elif name == "echo":',
    )
    fake_server.write_text(source)
    client = await McpStdioClient.spawn(
        [sys.executable, str(fake_server)],
        executor=LocalCommandExecutor() if executor else None,
    )
    try:
        result = await client.call_tool("large", {})
        assert len(result["content"][0]["text"]) == 100000
    finally:
        await client.close()


async def test_cancelled_stdio_handshake_closes_process(
    fake_server: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消 initialize 不留下仍活着的子进程。"""
    entered = asyncio.Event()
    clients: list[McpStdioClient] = []

    async def paused_initialize(client: McpStdioClient) -> None:
        """在进程启动之后停住握手。"""
        clients.append(client)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(McpStdioClient, "_initialize", paused_initialize)
    task = asyncio.create_task(McpStdioClient.spawn([sys.executable, str(fake_server)]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients[0]._proc.returncode is not None
    finally:
        if clients:
            await clients[0].close()


@pytest.mark.parametrize("body", [b'{"error":"secret","id":1}', b'{"result":{},"id":1}\xff'])
async def test_invalid_http_response_is_mcp_error(body: bytes) -> None:
    """远端坏编码或坏 error 形状不会泄漏底层异常类型。"""
    client = McpHttpClient("https://mcp.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body),
    ))
    try:
        with pytest.raises(McpToolError):
            await client.call_tool("tool", {})
    finally:
        await client.close()


async def test_non_object_tool_result_is_rejected() -> None:
    """非对象工具结果不能被替换成假空成功。"""
    client = McpHttpClient("https://mcp.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []}),
    ))
    try:
        with pytest.raises(McpToolError):
            await client.call_tool("tool", {})
    finally:
        await client.close()


async def test_http_pool_closes_when_router_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """前一个资源清理失败也必须关闭 HTTP pool。"""
    client = McpHttpClient("https://mcp.test")

    async def fail() -> None:
        """制造路由器清理异常。"""
        raise RuntimeError("first cleanup error")

    monkeypatch.setattr(client._router, "aclose", fail)
    try:
        with pytest.raises(RuntimeError, match="first cleanup error"):
            await client.close()
        assert client._http.is_closed
    finally:
        await client._http.aclose()


def test_bad_frame_logs_only_metadata(caplog: pytest.LogCaptureFixture) -> None:
    """坏帧不会把远端正文写入日志。"""
    with caplog.at_level("WARNING"):
        assert _decode_message("secret-frame-invalid-json") is None
    assert "secret-frame" not in caplog.text
    assert "length" in caplog.text


def test_server_method_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """远端通知名不能作为日志正文。"""
    from taifeng.mcp.server_messages import ServerMessageRouter

    async def send(message: dict) -> None:
        """本例不需要发送应答。"""
        return None

    router = ServerMessageRouter(send=send, server_info=lambda: {})
    with caplog.at_level("DEBUG"):
        router.route({"method": "secret-remote-method"})
        router.route({"method": {"secret-remote-method": True}})
    assert "secret-remote-method" not in caplog.text


async def test_handshake_error_survives_cleanup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端清理失败不能把原始握手错误变成另一个异常。"""
    clients: list[McpHttpClient] = []
    original_close = McpHttpClient.close

    async def initialize(client: McpHttpClient) -> None:
        """制造握手错误并留下待清理的客户端引用。"""
        clients.append(client)
        raise ValueError("handshake original")

    async def close(client: McpHttpClient) -> None:
        """资源实际关闭后再报告另一异常。"""
        await original_close(client)
        raise RuntimeError("cleanup secondary")

    monkeypatch.setattr(McpHttpClient, "_initialize", initialize)
    monkeypatch.setattr(McpHttpClient, "close", close)
    with pytest.raises(ValueError, match="handshake original"):
        await McpHttpClient.connect("https://mcp.test", listen_notifications=False)
    assert clients[0]._http.is_closed


async def test_bad_utf8_sse_is_not_silent_replacement() -> None:
    """SSE 的坏 UTF-8 不能被替换字符伪装成成功。"""
    client = McpHttpClient("https://mcp.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=b'data: {"id":1,"result":{"text":"\xff"}}\n\n'),
    ))
    try:
        with pytest.raises(McpToolError, match="UTF-8"):
            await client.call_tool("tool", {})
    finally:
        await client.close()


async def test_overbound_stdio_disconnects_and_fails_followup(fake_server: Path) -> None:
    """超限读取令在途及后续请求立即失败，客户端仍能收掉进程。"""
    fake_server.write_text(fake_server.read_text().replace(
        'if name == "echo":',
        'if name == "large":\n                print("x" * (16 * 1024 * 1024 + 1), flush=True)\n            elif name == "echo":',
    ))
    client = await McpStdioClient.spawn([sys.executable, str(fake_server)])
    try:
        with pytest.raises(McpToolError, match="closed"):
            await asyncio.wait_for(client.call_tool("large", {}), 3)
        with pytest.raises(McpToolError, match="closed"):
            await asyncio.wait_for(client.call_tool("echo", {}), 0.2)
    finally:
        await client.close()
    assert client._proc.returncode is not None


async def test_cancel_delivery_failure_logs_no_remote_body(caplog: pytest.LogCaptureFixture) -> None:
    """取消通知失败的 traceback 也不能把 HTTP 正文写入日志。"""
    client = McpHttpClient("https://mcp.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(400, content=b"secret-remote-body"),
    ))
    try:
        with caplog.at_level("DEBUG"):
            client._cancels.notify("secret-id", "secret-method", "secret-reason")
            await asyncio.gather(*list(client._cancels._tasks))
        assert "secret-" not in caplog.text
    finally:
        await client.close()


async def test_non_object_stdio_result_is_rejected(fake_server: Path) -> None:
    """stdio 也必须拒绝非对象工具结果。"""
    fake_server.write_text(fake_server.read_text().replace(
        'if name == "echo":',
        'if name == "invalid":\n                reply(msg_id, [])\n            elif name == "echo":',
    ))
    client = await McpStdioClient.spawn([sys.executable, str(fake_server)])
    try:
        with pytest.raises(McpToolError):
            await client.call_tool("invalid", {})
    finally:
        await client.close()


async def test_bridge_logs_metadata_only(caplog: pytest.LogCaptureFixture,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """桥接注册和重同步失败日志不保留远端名字或正文。"""
    from taifeng.mcp import bind_mcp_tools
    from taifeng.tool import ToolRegistry

    client = McpHttpClient("https://mcp.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": 1, "result": {
            "tools": [{"name": "secret-tool", "annotations": "secret-annotations"}],
        }}),
    ))
    client._server_info = {"name": "secret-server"}

    async def fail() -> list[dict]:
        """模拟远端带正文的失败。"""
        raise McpToolError(-32000, "secret-remote-body")

    try:
        with caplog.at_level("DEBUG"):
            binding = await bind_mcp_tools(client, ToolRegistry(), trust_annotations=True, watch=False)
            monkeypatch.setattr(client, "list_tools", fail)
            await binding._on_list_changed()
        assert "secret-" not in caplog.text
    finally:
        await client.close()
