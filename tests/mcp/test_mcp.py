"""MCP stdio client 测试 —— 用 Python 子进程模拟 MCP server。"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from taifeng.mcp import McpStdioClient, register_mcp_tools_async
from taifeng.tool.registry import ToolRegistry


FAKE_MCP_SERVER = r"""
import json
import sys


def reply(req_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            client_info = (msg.get("params") or {}).get("clientInfo") or {}
            reply(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "fake-mcp",
                    "version": "0.1.0",
                    "receivedClientVersion": client_info.get("version"),
                },
            })
        elif method == "notifications/initialized":
            pass  # no response for notifications
        elif method == "tools/list":
            reply(msg_id, {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo back the input",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "uppercase",
                        "description": "Uppercase the input",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                ]
            })
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                reply(msg_id, {
                    "content": [{"type": "text", "text": args.get("text", "")}],
                    "isError": False,
                })
            elif name == "uppercase":
                t = args.get("text", "")
                reply(msg_id, {
                    "content": [{"type": "text", "text": t.upper()}],
                    "isError": False,
                })
            else:
                reply(msg_id, error={"code": -32601, "message": f"unknown tool: {name}"})
        else:
            reply(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})


if __name__ == "__main__":
    main()
"""


@pytest.fixture
def fake_server(tmp_path: Path) -> Path:
    p = tmp_path / "fake_mcp.py"
    p.write_text(textwrap.dedent(FAKE_MCP_SERVER), encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_mcp_initialize_and_list(
    fake_server: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import taifeng

    runtime_version = "runtime-version-sentinel"
    monkeypatch.setattr(taifeng, "__version__", runtime_version)
    client = await McpStdioClient.spawn([sys.executable, str(fake_server)])
    try:
        assert client.server_info.get("name") == "fake-mcp"
        assert client.server_info.get("receivedClientVersion") == runtime_version
        tools = await client.list_tools()
        assert len(tools) == 2
        names = sorted(t["name"] for t in tools)
        assert names == ["echo", "uppercase"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_call_tool(fake_server: Path) -> None:
    client = await McpStdioClient.spawn([sys.executable, str(fake_server)])
    try:
        result = await client.call_tool("uppercase", {"text": "taifeng"})
        content = result["content"][0]
        assert content["text"] == "TAIFENG"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_mcp_tools_async(fake_server: Path) -> None:
    client = await McpStdioClient.spawn([sys.executable, str(fake_server)])
    try:
        registry = ToolRegistry()
        specs = await register_mcp_tools_async(
            client, registry, tool_prefix="mcp_fs_",
        )
        assert len(specs) == 2
        assert "mcp_fs_echo" in registry
        assert "mcp_fs_uppercase" in registry

        # 调用一下
        from taifeng.tool.spec import ToolContext
        from taifeng.loop.cancellation import CancellationToken
        spec = registry.get("mcp_fs_uppercase")
        assert spec is not None
        result = await spec.handler(
            {"text": "hi"},
            ToolContext(call_id="c", cancel=CancellationToken(), thread_id="t"),
        )
        assert not result.is_error
        assert result.output == "HI"
    finally:
        await client.close()
