"""server_initiated_request 的 elicitation 生命周期 telemetry 测试（G1 T4）。

覆盖 spec ``mcp-server`` ADDED Requirement
"Telemetry 事件覆盖 elicitation 生命周期"。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from taifeng.mcp.server import McpStdioServer


def _make_pipe() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, list[bytes]]:
    reader = asyncio.StreamReader()
    written: list[bytes] = []

    class _T:
        def write(self, data: bytes) -> None:
            written.append(data)

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    class _P:
        async def _drain_helper(self) -> None:
            return None

        def connection_lost(self, exc: Any) -> None:
            pass

    loop = asyncio.get_event_loop()
    writer = asyncio.StreamWriter(_T(), _P(), reader, loop)
    return reader, writer, written


@pytest.mark.asyncio
async def test_success_emits_started_then_completed() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, data: dict[str, Any]) -> None:
        events.append((kind, data))

    pool = MagicMock()
    server = McpStdioServer(pool, emit=emit)
    reader, writer, written = _make_pipe()
    task = asyncio.create_task(server.run(stdin=reader, stdout=writer))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if server._stdout is not None:
            break

    async def respond() -> None:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if written:
                break
        line = (
            json.dumps({
                "jsonrpc": "2.0", "id": "srv_1",
                "result": {"action": "accept", "content": {"approved": True}},
            }) + "\n"
        ).encode("utf-8")
        reader.feed_data(line)

    responder = asyncio.create_task(respond())
    await server.server_initiated_request(
        "elicitation/create",
        {"message": "approve?"},
        timeout=2.0,
    )
    await responder
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=2.0)

    kinds = [k for k, _ in events]
    # 期望 started 在前，completed 在后；不应有 timed_out
    assert "elicitation_started" in kinds
    assert "elicitation_completed" in kinds
    assert "elicitation_timed_out" not in kinds
    assert kinds.index("elicitation_started") < kinds.index("elicitation_completed")
    completed = next(d for k, d in events if k == "elicitation_completed")
    assert completed["outcome"] == "ok"
    assert completed["method"] == "elicitation/create"
    assert completed["id"] == "srv_1"
    assert isinstance(completed["duration_ms"], int)


@pytest.mark.asyncio
async def test_timeout_emits_started_then_timed_out_then_completed_timeout() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, data: dict[str, Any]) -> None:
        events.append((kind, data))

    pool = MagicMock()
    server = McpStdioServer(pool, emit=emit)
    reader, writer, _written = _make_pipe()
    task = asyncio.create_task(server.run(stdin=reader, stdout=writer))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if server._stdout is not None:
            break

    with pytest.raises(TimeoutError):
        await server.server_initiated_request(
            "elicitation/create", {"x": 1}, timeout=0.1,
        )

    reader.feed_eof()
    await asyncio.wait_for(task, timeout=2.0)

    kinds = [k for k, _ in events]
    assert "elicitation_started" in kinds
    assert "elicitation_timed_out" in kinds
    assert "elicitation_completed" in kinds
    # 顺序：started → timed_out → completed
    started_i = kinds.index("elicitation_started")
    timeout_i = kinds.index("elicitation_timed_out")
    completed_i = kinds.index("elicitation_completed")
    assert started_i < timeout_i < completed_i
    completed = next(d for k, d in events if k == "elicitation_completed")
    assert completed["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_none_emit_does_not_raise() -> None:
    """emit=None 时走 logger.info；不应触发任何错误。"""
    pool = MagicMock()
    server = McpStdioServer(pool, emit=None)
    reader, writer, _written = _make_pipe()
    task = asyncio.create_task(server.run(stdin=reader, stdout=writer))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if server._stdout is not None:
            break
    with pytest.raises(TimeoutError):
        await server.server_initiated_request("ping", {}, timeout=0.1)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=2.0)
