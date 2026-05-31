"""McpPrompter 测试（G1 T3）。

覆盖 spec ``permission-gate`` ADDED Requirement
"McpPrompter 是 PermissionPrompter 协议的官方 MCP 实现"。
"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.mcp.prompter import McpPrompter
from taifeng.mcp.server import McpServerInitiatedRequestError
from taifeng.permission.types import PermissionRequest


# --------------------------------------------------------------------
# 假 server：用 stub 替代真实 McpStdioServer.server_initiated_request
# --------------------------------------------------------------------


class _StubServer:
    """记录调用并按 scripted_response 回包。"""

    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.response = response
        self.raise_exc = raise_exc
        self.last_method: str | None = None
        self.last_params: dict[str, Any] | None = None
        self.last_timeout: float | None = None

    async def server_initiated_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        self.last_method = method
        self.last_params = params
        self.last_timeout = timeout
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response or {}


def _make_request() -> PermissionRequest:
    return PermissionRequest.for_tool_call(
        "shell_exec",
        {"command": "ls /etc"},
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="ent",
        turn_index=1,
        call_chain=("ent", "child"),
    )


# --------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_approves_returns_allow() -> None:
    stub = _StubServer(response={
        "action": "accept",
        "content": {"approved": True, "reason": "looks fine"},
    })
    prompter = McpPrompter(stub, timeout_seconds=5.0)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is True
    assert decision.mode == "allow"
    assert decision.reason == "looks fine"
    assert decision.remember_until == "once"


@pytest.mark.asyncio
async def test_user_denies_returns_deny() -> None:
    stub = _StubServer(response={
        "action": "accept",
        "content": {"approved": False, "reason": "unsafe"},
    })
    prompter = McpPrompter(stub)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is False
    assert decision.mode == "deny"
    assert decision.reason == "unsafe"


@pytest.mark.asyncio
async def test_user_rejects_action_returns_deny() -> None:
    stub = _StubServer(response={"action": "reject"})
    prompter = McpPrompter(stub)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is False
    assert decision.reason == "user_rejected"


@pytest.mark.asyncio
async def test_user_cancels_action_returns_deny() -> None:
    stub = _StubServer(response={"action": "cancel"})
    prompter = McpPrompter(stub)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is False
    assert decision.reason == "user_cancelled"


@pytest.mark.asyncio
async def test_unknown_action_returns_deny_with_reason() -> None:
    stub = _StubServer(response={"action": "weird"})
    prompter = McpPrompter(stub)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is False
    assert "weird" in decision.reason
    assert decision.reason.startswith("elicitation_unknown_action")


@pytest.mark.asyncio
async def test_timeout_returns_deny_reason_elicitation_timeout() -> None:
    stub = _StubServer(raise_exc=TimeoutError())
    prompter = McpPrompter(stub, timeout_seconds=0.1)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is False
    assert decision.reason == "elicitation_timeout"


@pytest.mark.asyncio
async def test_server_error_returns_deny() -> None:
    stub = _StubServer(raise_exc=McpServerInitiatedRequestError(
        code=-32601, message="method not found",
    ))
    prompter = McpPrompter(stub)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is False
    assert "-32601" in decision.reason
    assert decision.reason.startswith("elicitation_error")


@pytest.mark.asyncio
async def test_unexpected_exception_returns_deny() -> None:
    stub = _StubServer(raise_exc=RuntimeError("boom"))
    prompter = McpPrompter(stub)
    decision = await prompter.prompt(_make_request())
    assert decision.granted is False
    assert "RuntimeError" in decision.reason


@pytest.mark.asyncio
async def test_request_params_contain_message_and_schema() -> None:
    stub = _StubServer(response={
        "action": "accept", "content": {"approved": True},
    })
    prompter = McpPrompter(stub, timeout_seconds=7.5)
    req = PermissionRequest.for_tool_call(
        "shell_exec",
        {"command": "ls /etc"},
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="ent",
        turn_index=1,
        call_chain=("ent", "child"),
    )
    await prompter.prompt(req)
    assert stub.last_method == "elicitation/create"
    assert stub.last_timeout == 7.5
    assert stub.last_params is not None
    msg = stub.last_params["message"]
    assert "shell_exec" in msg
    assert "ls /etc" in msg
    assert "ent → child" in msg
    schema = stub.last_params["requestedSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["approved"]["type"] == "boolean"
    assert schema["required"] == ["approved"]
