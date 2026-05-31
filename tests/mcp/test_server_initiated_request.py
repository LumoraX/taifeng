"""McpStdioServer.server_initiated_request 测试（G1 T2）。

覆盖 spec ``mcp-server`` ADDED Requirements:
    - "Server-initiated request 双向 JSON-RPC"
    - "stdin 路由区分 incoming request / incoming response"
    - "stdout 写入并发互斥"
    - "server_initiated_request 可取消"
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from taifeng.mcp.server import (
    McpServerInitiatedRequestError,
    McpStdioServer,
)


# --------------------------------------------------------------------
# Fixtures: fake bidirectional pipe（in-memory stdin / stdout）
# --------------------------------------------------------------------


def _make_pipe() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, list[bytes]]:
    """构造 (StreamReader, StreamWriter, written_lines_buffer) 三元组。

    StreamWriter.write() 写入的所有字节追加到 written_lines_buffer，调用方
    通过该 list 观察 server 写出的内容。
    """
    reader = asyncio.StreamReader()
    written: list[bytes] = []

    class _FakeTransport:
        def write(self, data: bytes) -> None:
            written.append(data)

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    class _FakeProtocol:
        async def _drain_helper(self) -> None:
            return None

        def connection_lost(self, exc: Any) -> None:
            pass

    loop = asyncio.get_event_loop()
    writer = asyncio.StreamWriter(_FakeTransport(), _FakeProtocol(), reader, loop)
    return reader, writer, written


# --------------------------------------------------------------------
# 测试夹具：起 server.run task + 注入 fake stream
# --------------------------------------------------------------------


async def _start_server() -> tuple[
    McpStdioServer, asyncio.StreamReader, list[bytes], asyncio.Task[None],
]:
    pool = MagicMock()
    pool.skill_registry.snapshot.return_value.ids.return_value = []
    pool.skill_registry.snapshot.return_value.get.return_value = None
    server = McpStdioServer(pool)
    reader, writer, written = _make_pipe()
    task = asyncio.create_task(server.run(stdin=reader, stdout=writer))
    # 等 server.run 把 _stdout bind 上
    for _ in range(50):
        await asyncio.sleep(0.01)
        if server._stdout is not None:
            break
    assert server._stdout is not None
    return server, reader, written, task


async def _stop_server(reader: asyncio.StreamReader, task: asyncio.Task[None]) -> None:
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=2.0)


# --------------------------------------------------------------------
# Scenario: outgoing request id 用 srv_ 前缀 + 收到 response resolve
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_returns_client_response_result() -> None:
    server, reader, written, task = await _start_server()
    try:
        # 模拟 client 收到 srv_1 后回 result
        async def _respond() -> None:
            # 等 server 写出 outgoing 请求
            for _ in range(100):
                await asyncio.sleep(0.01)
                if written:
                    break
            assert written, "server did not write outgoing request"
            sent = json.loads(written[-1].decode("utf-8").strip())
            assert sent["id"] == "srv_1"
            assert sent["method"] == "elicitation/create"
            # client 回包
            response_line = (
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": "srv_1",
                    "result": {"action": "accept", "content": {"approved": True}},
                }) + "\n"
            ).encode("utf-8")
            reader.feed_data(response_line)

        responder = asyncio.create_task(_respond())
        result = await server.server_initiated_request(
            "elicitation/create",
            {"message": "approve?", "requestedSchema": {}},
            timeout=2.0,
        )
        await responder
        assert result["action"] == "accept"
        assert result["content"]["approved"] is True
        assert "srv_1" not in server._pending_outgoing
    finally:
        await _stop_server(reader, task)


# --------------------------------------------------------------------
# Scenario: outgoing request 超时清理
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_timeout_raises_timeout_error() -> None:
    server, reader, written, task = await _start_server()
    try:
        with pytest.raises(TimeoutError):
            await server.server_initiated_request(
                "elicitation/create", {"x": 1}, timeout=0.15,
            )
        # 超时清理后无残留
        assert "srv_1" not in server._pending_outgoing

        # 迟到 response → 仅 warning 不崩
        late = (
            json.dumps({"jsonrpc": "2.0", "id": "srv_1", "result": {}}) + "\n"
        ).encode("utf-8")
        reader.feed_data(late)
        await asyncio.sleep(0.1)
        # task 仍在跑（没 crash）
        assert not task.done()
    finally:
        await _stop_server(reader, task)


# --------------------------------------------------------------------
# Scenario: client 回 error → 抛 McpServerInitiatedRequestError
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_error_response_raises() -> None:
    server, reader, written, task = await _start_server()
    try:
        async def _respond() -> None:
            for _ in range(100):
                await asyncio.sleep(0.01)
                if written:
                    break
            err_line = (
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": "srv_1",
                    "error": {"code": -32602, "message": "bad params"},
                }) + "\n"
            ).encode("utf-8")
            reader.feed_data(err_line)

        responder = asyncio.create_task(_respond())
        with pytest.raises(McpServerInitiatedRequestError) as exc_info:
            await server.server_initiated_request(
                "elicitation/create", {}, timeout=2.0,
            )
        await responder
        assert exc_info.value.code == -32602
        assert "bad params" in exc_info.value.message
    finally:
        await _stop_server(reader, task)


# --------------------------------------------------------------------
# Scenario: 两个并发请求 id 递增 + 不写交错
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_use_incrementing_ids() -> None:
    server, reader, written, task = await _start_server()
    try:
        # 准备两个 client 回包（按 id 匹配）
        async def _respond_both() -> None:
            seen_ids: set[str] = set()
            for _ in range(500):
                await asyncio.sleep(0.005)
                # 取当前所有写出
                for raw in written:
                    text = raw.decode("utf-8").strip()
                    if not text:
                        continue
                    msg = json.loads(text)
                    sid = msg.get("id")
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    response = (
                        json.dumps({
                            "jsonrpc": "2.0",
                            "id": sid,
                            "result": {"ack": sid},
                        }) + "\n"
                    ).encode("utf-8")
                    reader.feed_data(response)
                if len(seen_ids) >= 2:
                    return

        responder = asyncio.create_task(_respond_both())
        r1, r2 = await asyncio.gather(
            server.server_initiated_request("m1", {}, timeout=3.0),
            server.server_initiated_request("m2", {}, timeout=3.0),
        )
        await responder
        # 两个 ack 必须分别是 srv_1 / srv_2
        acks = sorted([r1["ack"], r2["ack"]])
        assert acks == ["srv_1", "srv_2"]
    finally:
        await _stop_server(reader, task)


# --------------------------------------------------------------------
# Scenario: 孤儿 response 仅 log warning 不崩
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_response_does_not_crash() -> None:
    server, reader, written, task = await _start_server()
    try:
        line = (
            json.dumps({
                "jsonrpc": "2.0",
                "id": "srv_99",
                "result": {"ghost": True},
            }) + "\n"
        ).encode("utf-8")
        reader.feed_data(line)
        await asyncio.sleep(0.1)
        assert not task.done()
        # 没有 pending future 受影响
        assert not server._pending_outgoing
    finally:
        await _stop_server(reader, task)
