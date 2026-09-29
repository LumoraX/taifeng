"""taifeng 作为 MCP server 的客户端能力门控（server_capabilities.py + server + McpPrompter）。

规范（2025-06-18 §Lifecycle「Operation」/ §Client「Elicitation」）：双方 MUST 只使用协商成功的
能力；客户端没在 initialize 声明 ``elicitation`` 时 server 不得发 ``elicitation/create``。
门控后审批按 fail-closed 立即 deny（reason 写明客户端不支持），不再等超时；并 emit
``elicitation_unsupported``。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from taifeng.mcp.prompter import McpPrompter
from taifeng.mcp.server import McpStdioServer
from taifeng.mcp.server_capabilities import (
    McpClientCapabilityError,
    missing_client_capability,
    parse_client_capabilities,
)
from taifeng.permission.types import PermissionPolicy, PermissionRequest
from tests.conftest import GUARD_TIMEOUT_SECONDS, guard_ticks, wait_for_condition
from tests.mcp.server_handshake import ELICITATION, negotiate


def _make_pipe() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, list[bytes]]:
    """内存管道：server 写出的字节追加进 written。"""
    reader = asyncio.StreamReader()
    written: list[bytes] = []

    class _Transport:
        def write(self, data: bytes) -> None:
            written.append(data)

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    class _Protocol:
        async def _drain_helper(self) -> None:
            return None

        def connection_lost(self, exc: Any) -> None:
            pass

    writer = asyncio.StreamWriter(_Transport(), _Protocol(), reader, asyncio.get_event_loop())
    return reader, writer, written


def _methods(written: list[bytes]) -> list[str]:
    return [json.loads(raw)["method"] for raw in written if b'"method"' in raw]


class _Running:
    """起一个带 emit 记录的 server。"""

    def __init__(self, pool: Any | None = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

        async def _emit(kind: str, data: dict[str, Any]) -> None:
            self.events.append((kind, data))

        self.server = McpStdioServer(pool or MagicMock(), emit=_emit)
        self.reader, writer, self.written = _make_pipe()
        self.task = asyncio.create_task(self.server.run(stdin=self.reader, stdout=writer))

    async def started(self) -> _Running:
        await wait_for_condition(lambda: self.server._stdout is not None)
        return self

    async def stop(self) -> None:
        self.reader.feed_eof()
        await asyncio.wait_for(self.task, GUARD_TIMEOUT_SECONDS)


def _request() -> PermissionRequest:
    return PermissionRequest.for_tool_call(
        "shell_exec", {"command": "ls"}, thread_id="t", submission_id="s",
        entry_skill_id="e", turn_index=1)


# ---------------------------------------------------------------- 纯函数


def test_capability_lookup_requires_object_values() -> None:
    """只有对象形状的声明才算数；ping 等无需能力的方法恒放行；未 initialize 视为未声明。"""
    assert missing_client_capability("elicitation/create", {"elicitation": {}}) is None
    for caps in (None, {}, {"elicitation": True}, {"elicitation": None}):
        assert missing_client_capability("elicitation/create", caps) == "elicitation"
    assert missing_client_capability("ping", None) is None
    assert missing_client_capability("roots/list", {"elicitation": {}}) == "roots"
    assert parse_client_capabilities({"capabilities": ["x"]}) == {}
    assert parse_client_capabilities({"capabilities": {"elicitation": {}}}) == ELICITATION


# ---------------------------------------------------------------- server 门控


@pytest.mark.parametrize(("capabilities", "initialized"), [
    (None, False),          # 从未 initialize
    ({"sampling": {}}, True),  # initialize 了但没声明 elicitation
])
async def test_server_does_not_send_undeclared_elicitation(
    capabilities: dict[str, Any] | None, initialized: bool,
) -> None:
    """未声明 → 抛 McpClientCapabilityError，stdout 上没有 elicitation/create，并 emit 信号。"""
    running = await _Running().started()
    try:
        if capabilities is not None:
            await negotiate(running.reader, running.written, capabilities)
        with pytest.raises(McpClientCapabilityError) as exc_info:
            await running.server.server_initiated_request(
                "elicitation/create", {"message": "?"}, timeout=5)
    finally:
        await running.stop()
    assert exc_info.value.initialized is initialized
    assert exc_info.value.capability == "elicitation"
    assert "elicitation/create" not in _methods(running.written)
    assert running.events == [("elicitation_unsupported", {
        "method": "elicitation/create", "capability": "elicitation", "initialized": initialized})]


async def test_server_records_capabilities_and_sends_when_declared() -> None:
    """声明了 elicitation → 照常发出；client_capabilities 反映 initialize 的声明。"""
    running = await _Running().started()
    try:
        assert running.server.client_capabilities is None
        await negotiate(running.reader, running.written, ELICITATION)
        assert running.server.client_capabilities == ELICITATION
        with pytest.raises(TimeoutError):
            await running.server.server_initiated_request(
                "elicitation/create", {"message": "?"}, timeout=0.05)
    finally:
        await running.stop()
    assert _methods(running.written) == ["elicitation/create"]
    assert "elicitation_unsupported" not in [kind for kind, _ in running.events]


# ---------------------------------------------------------------- McpPrompter


async def test_prompter_denies_immediately_when_client_lacks_elicitation() -> None:
    """客户端未声明 → policy.check 立即 deny（不等超时），reason 写明不支持。"""
    running = await _Running().started()
    try:
        await negotiate(running.reader, running.written, {})
        policy = PermissionPolicy(
            rules=[], default_mode="ask",
            prompter=McpPrompter(running.server, timeout_seconds=GUARD_TIMEOUT_SECONDS))
        decision = await asyncio.wait_for(policy.check(_request()), GUARD_TIMEOUT_SECONDS)
    finally:
        await running.stop()
    assert decision.granted is False
    assert decision.reason.startswith("elicitation_unsupported:")
    assert "did not declare the 'elicitation' capability" in decision.reason
    assert running.written == []


def _event(kind: str, **data: Any) -> Any:
    return SimpleNamespace(msg=SimpleNamespace(kind=kind, data=data))


class _ApprovalEngine:
    """engine 替身：turn 内先走 policy.check（触发审批），再完成。"""

    def __init__(self, policy: PermissionPolicy, decisions: list[Any]) -> None:
        self._policy = policy
        self._decisions = decisions

    async def submit(self, _sub: Any) -> str:
        return "sub-1"

    async def subscribe(self, _sub_id: str) -> Any:
        self._decisions.append(await self._policy.check(_request()))
        yield _event("assistant_text", delta="done")
        yield _event("turn_completed")


async def test_tools_call_path_denies_without_elicitation_round_trip() -> None:
    """真实 tools/call 路径：不声明 elicitation 的客户端 → turn 内审批立即 deny，响应不被拖到超时。"""
    decisions: list[Any] = []
    pool = MagicMock()
    running = _Running(pool)
    policy = PermissionPolicy(
        rules=[], default_mode="ask",
        prompter=McpPrompter(running.server, timeout_seconds=GUARD_TIMEOUT_SECONDS))

    async def _get_or_create(**_kw: Any) -> Any:
        return _ApprovalEngine(policy, decisions)

    pool.get_or_create = _get_or_create
    await running.started()
    try:
        await negotiate(running.reader, running.written, {})
        running.reader.feed_data((json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "run_skill_turn",
                       "arguments": {"skill_id": "entry", "message": "hi"}},
        }) + "\n").encode("utf-8"))
        response: dict[str, Any] | None = None
        async for _ in guard_ticks():
            replies = [json.loads(raw) for raw in running.written]
            response = next((r for r in replies if r.get("id") == 7), None)
            if response is not None:
                break
    finally:
        await running.stop()
    assert response is not None and response["result"]["isError"] is False
    assert decisions[0].granted is False
    assert decisions[0].reason.startswith("elicitation_unsupported:")
    assert "elicitation/create" not in _methods(running.written)
