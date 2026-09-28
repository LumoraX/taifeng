"""dynamic-tool-set —— 工具注册表运行时增删 / 替换、变更事件、MCP list_changed 同步。"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.mcp import McpHttpClient, McpStdioClient, bind_mcp_tools
from taifeng.mcp.bridge import McpToolError
from taifeng.tool.registry import ToolRegistry, ToolSetChange, UnknownToolError
from taifeng.tool.spec import ToolResult, ToolSpec
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path


def _spec(name: str, description: str = "d") -> ToolSpec:
    async def _handler(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok(name)

    return ToolSpec(name=name, description=description,
                    input_schema={"type": "object", "properties": {}}, handler=_handler)


# ---------------------------------------------------------------- 注册表


def test_registry_unregister_replace_bump_version_and_notify() -> None:
    """register / unregister / replace 各 +1 版本并通知监听者。"""
    reg = ToolRegistry()
    seen: list[ToolSetChange] = []
    reg.subscribe(seen.append)

    reg.register(_spec("a"))
    reg.replace(_spec("a", "新描述"))
    removed = reg.unregister("a")

    assert removed.description == "新描述"
    assert "a" not in reg
    assert reg.version == 3
    assert [(c.added, c.replaced, c.removed, c.version) for c in seen] == [
        (("a",), (), (), 1), ((), ("a",), (), 2), ((), (), ("a",), 3)]


def test_registry_unknown_operations_raise() -> None:
    """删 / 替换不存在的工具显式报错（不静默忽略）。"""
    reg = ToolRegistry()
    with pytest.raises(UnknownToolError):
        reg.unregister("ghost")
    with pytest.raises(UnknownToolError):
        reg.replace(_spec("ghost"))


def test_registry_listener_failure_is_isolated() -> None:
    """坏监听者不阻断变更，也不影响其他监听者；退订后不再收到通知。"""
    reg = ToolRegistry()
    seen: list[int] = []

    def _boom(change: ToolSetChange) -> None:
        raise RuntimeError("listener bug")

    reg.subscribe(_boom)
    unsubscribe = reg.subscribe(lambda c: seen.append(c.version))
    reg.register(_spec("a"))
    unsubscribe()
    reg.register(_spec("b"))
    assert seen == [1]
    assert reg.names() == frozenset({"a", "b"})


# ---------------------------------------------------------------- engine 接线


async def test_pool_emits_tool_set_changed_and_next_sample_sees_new_tool(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """运行时注册新工具 → 各 engine 收到 tool_set_changed；下一次采样的工具列表含它。"""
    client = SimClient(turns=[SimTurn(text="一"), SimTurn(text="二")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="dyn", entry_skill_id="code-reviewer")
    entry = engine._entry_skill  # noqa: SLF001
    engine._entry_skill = replace(  # noqa: SLF001
        entry, tool_names=frozenset({*entry.tool_names, "late_tool"}))
    events: list[Any] = []

    async def _collect() -> None:
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    async def _turn(text: str) -> None:
        sub_id = await engine.submit(taifeng.UserMessage(text=text))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

    await _turn("第一轮")
    pool._tool_runtime._registry.register(_spec("late_tool"))  # noqa: SLF001
    await wait_for_condition(lambda: any(m.kind == "tool_set_changed" for m in events))
    await _turn("第二轮")

    changed = next(m for m in events if m.kind == "tool_set_changed")
    assert changed.data["added"] == ["late_tool"]
    requests = client.ledger.requests()
    assert "late_tool" not in {t.name for t in requests[0].request.tools}
    assert "late_tool" in {t.name for t in requests[1].request.tools}
    task.cancel()
    await pool.close()


# ---------------------------------------------------------------- MCP stdio


_LIST_CHANGING_SERVER = r"""
import json, sys

tools = [{"name": "alpha", "description": "A", "inputSchema": {"type": "object"}}]

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"serverInfo": {"name": "chg"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
    elif method == "tools/call":
        # 调用 mutate 后工具集变化：alpha 改描述、新增 beta，并推 list_changed 通知
        tools = [{"name": "alpha", "description": "A2", "inputSchema": {"type": "object"}},
                 {"name": "beta", "description": "B", "inputSchema": {"type": "object"}}]
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "ok"}]}})
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
"""


async def test_stdio_list_changed_resyncs_registry(tmp_path: Path) -> None:
    """server 推 list_changed → 绑定自动重新同步：新增 beta、替换 alpha。"""
    script = tmp_path / "server.py"
    script.write_text(textwrap.dedent(_LIST_CHANGING_SERVER), encoding="utf-8")
    client = await McpStdioClient.spawn([sys.executable, str(script)])
    reg = ToolRegistry()
    changes: list[ToolSetChange] = []
    reg.subscribe(changes.append)
    try:
        binding = await bind_mcp_tools(client, reg, tool_prefix="m_")
        assert binding.owned == {"m_alpha"}
        await client.call_tool("alpha", {})
        await wait_for_condition(lambda: "m_beta" in reg)
        await wait_for_condition(lambda: reg.require("m_alpha").description == "[MCP] A2")
        assert binding.owned == {"m_alpha", "m_beta"}
        assert any(c.replaced == ("m_alpha",) for c in changes)
        binding.detach()
        assert reg.names() == frozenset()
    finally:
        await client.close()


async def test_bind_does_not_steal_existing_tool_name() -> None:
    """与非本绑定的已注册工具重名 → 跳过，不抢占。"""

    class _Fake:
        server_info: dict[str, Any] = {"name": "fake"}

        async def list_tools(self) -> list[dict[str, Any]]:
            return [{"name": "clash", "inputSchema": {"type": "object"}}]

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"content": []}

        def add_tools_changed_listener(self, listener: Any) -> None:
            return None

    reg = ToolRegistry([_spec("clash", "本地工具")])
    binding = await bind_mcp_tools(_Fake(), reg)
    assert binding.owned == set()
    assert reg.require("clash").description == "本地工具"


# ---------------------------------------------------------------- MCP streamable HTTP


class _FakeHttpServer:
    """最小 streamable HTTP MCP server：JSON / SSE 两种响应 + 会话头 + 推送流。"""

    def __init__(self, *, get_status: int = 405) -> None:
        self.tools = [{"name": "alpha", "description": "A", "inputSchema": {"type": "object"}}]
        self.get_status = get_status
        self.session_headers: list[str | None] = []
        self.deleted = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(self.get_status)
        if request.method == "DELETE":
            self.deleted = True
            return httpx.Response(200)
        msg = json.loads(request.content)
        self.session_headers.append(request.headers.get("mcp-session-id"))
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return httpx.Response(202)
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "serverInfo": {"name": "http-fake"}}}, headers={"Mcp-Session-Id": "S-1"})
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid,
                                             "result": {"tools": self.tools}})
        if method == "tools/call":
            if msg["params"]["name"] == "fail":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid,
                                                 "error": {"code": -32602, "message": "bad"}})
            # SSE 响应：先推 list_changed 通知，再给出本次调用的结果
            self.tools = [*self.tools, {"name": "beta", "inputSchema": {"type": "object"}}]
            body = (
                "event: message\ndata: " + json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}) + "\n\n"
                + "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "done"}]}}) + "\n\n"
            )
            return httpx.Response(200, content=body.encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(404)


async def test_http_client_json_sse_session_and_list_changed() -> None:
    """JSON 与 SSE 响应都能解析；会话头带回；SSE 内的 list_changed 触发重新同步。"""
    server = _FakeHttpServer()
    client = await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server))
    reg = ToolRegistry()
    try:
        assert client.server_info == {"name": "http-fake"}
        binding = await bind_mcp_tools(client, reg)
        assert binding.owned == {"alpha"}
        result = await client.call_tool("alpha", {})
        assert result["content"][0]["text"] == "done"
        await wait_for_condition(lambda: "beta" in reg)
        # initialize 之后的请求都带回会话 id
        assert server.session_headers[0] is None
        assert set(server.session_headers[1:]) == {"S-1"}
        with pytest.raises(McpToolError, match="bad"):
            await client.call_tool("fail", {})
    finally:
        await client.close()
    assert server.deleted


async def test_http_client_maps_http_errors() -> None:
    """HTTP 非 2xx → McpToolError（不裸抛 httpx 异常）。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(McpToolError, match="http 500"):
        await McpHttpClient.connect("https://mcp.example/mcp",
                                    transport=httpx.MockTransport(_handler))
