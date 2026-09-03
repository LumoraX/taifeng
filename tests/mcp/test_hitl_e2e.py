"""Server-mode HITL 端到端集成测试（G1 T8）。

验证完整链路（server-initiated request + McpPrompter + PermissionPolicy）：

    PermissionPolicy.check(request)
        → mode='ask' → McpPrompter.prompt
        → McpStdioServer.server_initiated_request("elicitation/create")
        → stdout 写 outgoing JSON-RPC
        ← stdin feed client response
        ← McpPrompter parse response
        → PermissionDecision allow/deny

业务侧通过 PreToolUse hook 把 PermissionPolicy.check 接到任意工具上 —— 此处
直接调 policy.check 已足够验证 G1 真实闭环（端到端 turn + tool 透出 spy 已被
T1 wiring 测试 + T3 prompter 测试 + 既有 builtin 测试覆盖）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from taifeng.mcp.prompter import McpPrompter
from taifeng.mcp.server import McpStdioServer
from taifeng.permission.types import PermissionPolicy, PermissionRequest


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


async def _start_server() -> tuple[
    McpStdioServer, asyncio.StreamReader, list[bytes], asyncio.Task[None],
]:
    pool = MagicMock()
    server = McpStdioServer(pool)
    reader, writer, written = _make_pipe()
    task = asyncio.create_task(server.run(stdin=reader, stdout=writer))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if server._stdout is not None:
            break
    return server, reader, written, task


async def _wait_outgoing(written: list[bytes]) -> dict[str, Any]:
    for _ in range(300):
        await asyncio.sleep(0.01)
        for raw in written:
            text = raw.decode("utf-8").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("method") == "elicitation/create":
                return msg
    raise AssertionError("server did not write elicitation/create within 3s")


@pytest.mark.asyncio
async def test_hitl_full_round_trip_allow() -> None:
    """完整链路：policy.check(ask) → McpPrompter → server elicitation → host allow。"""
    server, reader, written, task = await _start_server()
    try:
        prompter = McpPrompter(server, timeout_seconds=3.0)
        policy = PermissionPolicy(
            rules=[], default_mode="ask", prompter=prompter,
        )

        req = PermissionRequest.for_tool_call(
            "shell_exec",
            {"command": "ls /etc"},
            thread_id="t1",
            submission_id="s1",
            entry_skill_id="entry",
            turn_index=1,
            call_chain=("entry",),
        )

        # 并发：policy.check 等回包；本 task 通过 reader 喂 response
        async def _host() -> None:
            msg = await _wait_outgoing(written)
            assert msg["id"] == "srv_1"
            assert "ls /etc" in msg["params"]["message"]
            line = (
                json.dumps({
                    "jsonrpc": "2.0", "id": "srv_1",
                    "result": {
                        "action": "accept",
                        "content": {"approved": True, "reason": "trusted"},
                    },
                }) + "\n"
            ).encode("utf-8")
            reader.feed_data(line)

        host_task = asyncio.create_task(_host())
        decision = await policy.check(req)
        await host_task

        assert decision.granted is True
        assert decision.mode == "allow"
        assert decision.reason == "trusted"
    finally:
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_hitl_full_round_trip_deny() -> None:
    """完整链路：policy.check(ask) → host deny → policy 返回 deny。"""
    server, reader, written, task = await _start_server()
    try:
        prompter = McpPrompter(server, timeout_seconds=3.0)
        policy = PermissionPolicy(
            rules=[], default_mode="ask", prompter=prompter,
        )

        req = PermissionRequest.for_skill_dispatch(
            "code-review",
            caller_skill_id="programmer",
            call_chain=("programmer",),
            thread_id="t1",
            submission_id="s1",
            entry_skill_id="programmer",
            turn_index=1,
        )

        async def _host() -> None:
            msg = await _wait_outgoing(written)
            line = (
                json.dumps({
                    "jsonrpc": "2.0", "id": msg["id"],
                    "result": {
                        "action": "accept",
                        "content": {"approved": False, "reason": "blocked"},
                    },
                }) + "\n"
            ).encode("utf-8")
            reader.feed_data(line)

        host_task = asyncio.create_task(_host())
        decision = await policy.check(req)
        await host_task

        assert decision.granted is False
        assert decision.reason == "blocked"
    finally:
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_hitl_timeout_falls_back_to_deny() -> None:
    """host 不回包 → prompter 超时 → policy 返回 deny('elicitation_timeout')。"""
    server, reader, _written, task = await _start_server()
    try:
        prompter = McpPrompter(server, timeout_seconds=0.15)
        policy = PermissionPolicy(
            rules=[], default_mode="ask", prompter=prompter,
        )
        req = PermissionRequest.for_tool_call(
            "shell_exec",
            {"command": "ls"},
            thread_id="t",
            submission_id="s",
            entry_skill_id="e",
            turn_index=1,
        )
        decision = await policy.check(req)
        assert decision.granted is False
        assert decision.reason == "elicitation_timeout"
    finally:
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# 真实 tools/call 路径（wave1 task 5）：读循环不得被 turn 阻塞
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402


def _event(kind: str, **data: Any) -> Any:
    return SimpleNamespace(msg=SimpleNamespace(kind=kind, data=data))


class _FakeEngine:
    """最小 engine 替身：turn 内先走 policy.check（触发 server elicitation），再完成。"""

    def __init__(self, policy: PermissionPolicy, decisions: list[Any]) -> None:
        self._policy = policy
        self._decisions = decisions

    async def submit(self, _sub: Any) -> str:
        return "sub-1"

    async def subscribe(self, _sub_id: str) -> Any:
        req = PermissionRequest(scope="shell_exec", target="ls /etc")
        decision = await self._policy.check(req)
        self._decisions.append(decision)
        yield _event("assistant_text", delta="done")
        yield _event("turn_completed")


class _HangingEngine:
    """turn 永不结束——用于验证 EOF 时在飞 tools/call 被收敛。"""

    cancelled = False

    async def submit(self, _sub: Any) -> str:
        return "sub-2"

    async def subscribe(self, _sub_id: str) -> Any:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            type(self).cancelled = True
            raise
        yield _event("turn_completed")  # pragma: no cover


def _pool_with(engine: Any) -> MagicMock:
    pool = MagicMock()

    async def _get_or_create(**_kw: Any) -> Any:
        return engine

    pool.get_or_create = _get_or_create
    return pool


async def _wait_response(written: list[bytes], req_id: Any) -> dict[str, Any]:
    for _ in range(300):
        await asyncio.sleep(0.01)
        for raw in written:
            text = raw.decode("utf-8").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id and "result" in msg:
                return msg
    raise AssertionError(f"no response for id={req_id} within 3s")


@pytest.mark.asyncio
async def test_tools_call_path_hitl_gets_client_answer_before_timeout() -> None:
    """client 发 tools/call → turn 内 elicitation → client 回 accept → tools/call 成功。

    此前读循环在 tools/call 内同步 await 整个 turn，client 的 accept 躺在 stdin
    无人读 → prompter 必超时 → deny。
    """
    decisions: list[Any] = []
    # 先建 server（policy 需要引用 server 上的 prompter）
    pool = MagicMock()
    server = McpStdioServer(pool)
    prompter = McpPrompter(server, timeout_seconds=1.0)
    policy = PermissionPolicy(rules=[], default_mode="ask", prompter=prompter)
    pool.get_or_create = _pool_with(_FakeEngine(policy, decisions)).get_or_create

    reader, writer, written = _make_pipe()
    task = asyncio.create_task(server.run(stdin=reader, stdout=writer))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if server._stdout is not None:
            break
    try:
        call = json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "run_skill_turn",
                       "arguments": {"skill_id": "entry", "message": "hi"}},
        }) + "\n"
        reader.feed_data(call.encode("utf-8"))

        # host：等 elicitation 出现后在 stdin 写回 accept
        msg = await _wait_outgoing(written)
        reader.feed_data((json.dumps({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"action": "accept",
                       "content": {"approved": True, "reason": "ok"}},
        }) + "\n").encode("utf-8"))

        resp = await _wait_response(written, 7)
        assert resp["result"]["isError"] is False
        assert decisions and decisions[0].granted is True
    finally:
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_run_exit_converges_in_flight_tools_call() -> None:
    """tools/call 仍在飞时 stdin EOF → run() 返回前在飞任务被取消。"""
    server = McpStdioServer(_pool_with(_HangingEngine()))
    reader, writer, written = _make_pipe()
    task = asyncio.create_task(server.run(stdin=reader, stdout=writer))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if server._stdout is not None:
            break
    call = json.dumps({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "run_skill_turn",
                   "arguments": {"skill_id": "entry", "message": "hi"}},
    }) + "\n"
    reader.feed_data(call.encode("utf-8"))
    await asyncio.sleep(0.05)
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=2.0)
    assert _HangingEngine.cancelled is True
