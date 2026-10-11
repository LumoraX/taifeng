"""MCP 2025 会话失效后的有限恢复与执行歧义。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from taifeng.mcp import McpHttpClient
from taifeng.mcp.bridge import McpToolError


class ExpiringServer:
    """在旧会话拒绝请求的新会话服务端。"""

    def __init__(self, *, resume: bool = False, always_expired: bool = False) -> None:
        """配置拒绝位置并记录请求。"""
        self.resume = resume
        self.always_expired = always_expired
        self.initializations = 0
        self.calls = 0
        self.initial_headers: list[httpx.Headers] = []
        self.get_headers: list[httpx.Headers] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """模拟握手、工具列表、直接拒绝与丢失响应。"""
        if request.method == "DELETE":
            return httpx.Response(200)
        if request.method == "GET":
            self.get_headers.append(request.headers)
            return httpx.Response(404)
        message = json.loads(request.content)
        method = message.get("method")
        if method == "initialize":
            self.initializations += 1
            self.initial_headers.append(request.headers)
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "test"}}
            return self.reply(message, result, session=f"s{self.initializations}")
        if "id" not in message:
            return httpx.Response(202)
        old_session = request.headers.get("Mcp-Session-Id") == "s1"
        if method == "tools/call":
            self.calls += 1
            if self.resume and old_session:
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=b'id: old-cursor\ndata: {"method":"notifications/ping"}\n\n')
            if old_session or self.always_expired:
                await asyncio.sleep(0)
                return httpx.Response(404)
            return self.reply(message, {"content": [{"type": "text", "text": "done"}]})
        return self.reply(message, {"tools": [{"name": f"tool{self.initializations}"}]})

    @staticmethod
    def reply(message: dict, result: dict, *, session: str | None = None) -> httpx.Response:
        """返回匹配请求 id 的合法结果。"""
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result},
                              headers={"Mcp-Session-Id": session} if session else {})


async def connect(server: ExpiringServer, **kwargs: object) -> McpHttpClient:
    """使用内存传输，省去网络与重试间隔。"""
    return await McpHttpClient.connect(
        "https://mcp.test", transport=httpx.MockTransport(server),
        listen_notifications=False, stream_resume_delay_seconds=0, **kwargs,
    )


async def test_post_rejection_reinitializes_and_retries_once() -> None:
    """明确拒绝的工具调用只在新会话重试一次。"""
    server = ExpiringServer()
    client = await connect(server)
    try:
        result = await client.call_tool("tool", {})
        assert result["content"][0]["text"] == "done"
        assert server.calls == 2
        assert server.initializations == 2
        assert all("Mcp-Session-Id" not in h for h in server.initial_headers)
        assert all("MCP-Protocol-Version" not in h for h in server.initial_headers)
    finally:
        await client.close()


async def test_concurrent_expiry_shares_one_reinitialization() -> None:
    """同时失效的请求共用一次握手。"""
    server = ExpiringServer()
    client = await connect(server)
    try:
        await asyncio.gather(*(client.call_tool("tool", {}) for _ in range(5)))
        assert server.initializations == 2
        assert server.calls == 10
    finally:
        await client.close()


async def test_second_rejection_does_not_loop() -> None:
    """新会话仍然 404 时返回错误，不无限握手。"""
    server = ExpiringServer(always_expired=True)
    client = await connect(server)
    try:
        with pytest.raises(McpToolError):
            await client.call_tool("tool", {})
        assert server.initializations == 2
        assert server.calls == 2
    finally:
        await client.close()


async def test_resume_expiry_never_replays_tool_call() -> None:
    """丢失结果的工具可能已执行；恢复会话也不能再次执行。"""
    server = ExpiringServer(resume=True)
    client = await connect(server)
    try:
        with pytest.raises(McpToolError, match="not replayed"):
            await client.call_tool("tool", {})
        assert server.calls == 1
        assert server.initializations == 2
    finally:
        await client.close()


async def test_changed_tools_notify_existing_listener() -> None:
    """新会话工具集变化通过已有通知机制报告。"""
    server = ExpiringServer()
    client = await connect(server)
    changed = asyncio.Event()

    async def notify() -> None:
        """记录工具集变化。"""
        changed.set()

    try:
        await client.list_tools()
        client.add_tools_changed_listener(notify)
        await client.call_tool("tool", {})
        await asyncio.wait_for(changed.wait(), 1)
    finally:
        await client.close()


async def test_push_expiry_resets_old_cursor() -> None:
    """推送流恢复后不能把旧会话 cursor 发到新会话。"""
    class PushServer(ExpiringServer):
        """先给旧 cursor，再让旧会话失效。"""

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            """第三次 GET 表明恢复完成并停止监听。"""
            if request.method != "GET":
                return await super().__call__(request)
            self.get_headers.append(request.headers)
            if len(self.get_headers) == 1:
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=b'id: secret-old-cursor\n\n')
            return httpx.Response(404 if len(self.get_headers) == 2 else 405)

    server = PushServer()
    client = await connect(server)
    try:
        await asyncio.wait_for(client._listen_server_stream(), 1)
        assert server.initializations == 2
        assert len(server.get_headers) == 3
        assert server.get_headers[1]["Last-Event-ID"] == "secret-old-cursor"
        assert "Last-Event-ID" not in server.get_headers[2]
        assert server.get_headers[2]["Mcp-Session-Id"] == "s2"
    finally:
        await client.close()


async def test_failed_recovery_blocks_uninitialized_requests() -> None:
    """重新握手失败后不能继续发送不带会话的工具请求。"""
    class FailedServer(ExpiringServer):
        """第二次握手返回坏结果。"""

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            """保留首轮握手，只破坏恢复过程。"""
            if request.method == "POST":
                message = json.loads(request.content)
                if message.get("method") == "initialize" and self.initializations:
                    self.initializations += 1
                    return self.reply(message, {})
            return await super().__call__(request)

    server = FailedServer()
    client = await connect(server)
    try:
        for _ in range(2):
            with pytest.raises(McpToolError, match="recovery failed"):
                await client.call_tool("tool", {})
        assert server.calls == 1
        assert server.initializations == 2
    finally:
        await client.close()


async def test_new_request_waits_for_recovery_handshake() -> None:
    """恢复过程中进入的请求必须等待，不能以无会话状态发送。"""
    entered = asyncio.Event()
    release = asyncio.Event()
    observed_sessions: list[str | None] = []

    class PausedServer(ExpiringServer):
        """停住第二轮握手以复现并发窗口。"""

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            """记录恢复窗口里的工具请求。"""
            if request.method == "POST":
                message = json.loads(request.content)
                if message.get("method") == "initialize" and self.initializations:
                    entered.set()
                    await release.wait()
                if message.get("method") == "tools/call":
                    observed_sessions.append(request.headers.get("Mcp-Session-Id"))
            return await super().__call__(request)

    client = await connect(PausedServer())
    first = asyncio.create_task(client.call_tool("first", {}))
    second: asyncio.Task | None = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(client.call_tool("second", {}))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        assert None not in observed_sessions
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await client.close()


async def test_push_waits_for_concurrent_recovery_and_clears_cursor() -> None:
    """请求恢复握手未完成时，推送 GET 等待且清除旧会话游标。"""
    from taifeng.mcp.sse import SseCursor

    entered = asyncio.Event()
    release = asyncio.Event()

    class PausedServer(ExpiringServer):
        """恢复期间开放一个受观察的推送 GET。"""

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            """停住握手，推送不支持时合法回 405。"""
            if request.method == "GET":
                self.get_headers.append(request.headers)
                return httpx.Response(405)
            if request.method == "POST":
                message = json.loads(request.content)
                if message.get("method") == "initialize" and self.initializations:
                    entered.set()
                    await release.wait()
            return await super().__call__(request)

    server = PausedServer()
    client = await connect(server)
    cursor = SseCursor(last_event_id="secret-old-cursor")
    client._push_generation = client._session_generation
    first = asyncio.create_task(client.call_tool("tool", {}))
    push: asyncio.Task | None = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        push = asyncio.create_task(client._open_push_stream(cursor))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, push)
        assert server.get_headers[0].get("Mcp-Session-Id") == "s2"
        assert "Last-Event-ID" not in server.get_headers[0]
    finally:
        release.set()
        await asyncio.gather(first, *([push] if push else []), return_exceptions=True)
        await client.close()


async def test_server_ping_during_recovery_handshake_does_not_deadlock() -> None:
    """新握手 SSE 中的服务端请求不能被恢复锁挡住回应。"""
    replied = asyncio.Event()

    class PingServer(ExpiringServer):
        """服务端等客户端回答 ping 才完成第二次握手。"""

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            """第二次 initialize 返回需要双向通信的流。"""
            if request.method == "POST":
                message = json.loads(request.content)
                if message.get("id") == "server-ping" and "result" in message:
                    assert request.headers.get("Mcp-Session-Id") == "s2"
                    replied.set()
                    return httpx.Response(202)
                if message.get("method") == "initialize" and self.initializations:
                    self.initializations += 1

                    async def body():
                        """等 ping 回应后才给初始化结果。"""
                        yield b'data: {"jsonrpc":"2.0","id":"server-ping","method":"ping"}\n\n'
                        await replied.wait()
                        result = {"id": message["id"], "result": {
                            "protocolVersion": "2025-06-18", "serverInfo": {},
                        }}
                        yield ("data: " + json.dumps(result) + "\n\n").encode()

                    return httpx.Response(200, content=body(), headers={
                        "content-type": "text/event-stream", "Mcp-Session-Id": "s2",
                    })
            return await super().__call__(request)

    client = await connect(PingServer(), request_timeout_seconds=0.2)
    try:
        result = await client.call_tool("tool", {})
        assert result["content"][0]["text"] == "done"
        assert replied.is_set()
    finally:
        await client.close()


async def test_delayed_server_response_cannot_cross_sessions() -> None:
    """旧流派出的应答任务不能发送到恢复后的新会话。"""
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[httpx.Request] = []
    client = McpHttpClient("https://mcp.test", transport=httpx.MockTransport(
        lambda request: (seen.append(request) or httpx.Response(202)),
    ))
    client._session_id = "old"

    async def delayed_reply() -> None:
        """模拟等待人工处理后才完成的服务端应答。"""
        started.set()
        await release.wait()
        await client._send_response({"id": "server-request", "result": {}})

    token = client._reply_session.set(("old",))
    task = asyncio.create_task(delayed_reply())
    client._reply_session.reset(token)
    try:
        await started.wait()
        client._session_id = "new"
        release.set()
        with pytest.raises(McpToolError, match="expired session"):
            await task
        assert seen == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
