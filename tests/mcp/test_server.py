"""McpStdioServer 单测（M3 / mcp-server-mode）。

策略：不真启动 stdio subprocess，直接构造 `McpStdioServer` 实例
+ 调 `_handle_line(json_bytes)` / `_dispatch(...)` 验证 response dict 结构。
覆盖 spec ``mcp-server`` 全部 ADDED Requirement。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.mcp.server import (
    MCP_PROTOCOL_VERSION,
    SKILL_URI_PREFIX,
    McpStdioServer,
)


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


async def _build_pool(
    skills_dir: Path, threads_dir: Path,
    *, mock_turns: list[SimTurn] | None = None,
) -> taifeng.EnginePool:
    client = SimClient(turns=mock_turns or [])
    return await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )


def _req(rid: int, method: str, params: dict | None = None) -> bytes:
    payload = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        payload["params"] = params
    return (json.dumps(payload) + "\n").encode("utf-8")


# --------------------------------------------------------------------
# Requirement: initialize handshake 合规
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_returns_protocol_handshake(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool, server_name="taifeng-test")
    resp = await server._handle_line(_req(1, "initialize", {}))  # noqa: SLF001

    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "taifeng-test"
    assert "tools" in result["capabilities"]
    assert "resources" in result["capabilities"]

    await pool.close()


# --------------------------------------------------------------------
# Requirement: tools/list 暴露单个 meta-tool
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_returns_run_skill_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(2, "tools/list", {}))  # noqa: SLF001

    tools = resp["result"]["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "run_skill_turn"
    schema = tool["inputSchema"]
    assert "skill_id" in schema["required"]
    assert "message" in schema["required"]
    assert "session_id" in schema["properties"]

    await pool.close()


# --------------------------------------------------------------------
# Requirement: tools/call 派发到 EnginePool 完整 turn
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_run_skill_turn_returns_final_text(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(
        skills_dir, threads_dir,
        mock_turns=[SimTurn(
            text="hello-from-skill",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )],
    )
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(3, "tools/call", {  # noqa: SLF001
        "name": "run_skill_turn",
        "arguments": {
            "skill_id": "code-reviewer",
            "message": "hi",
            "session_id": "test-1",
        },
    }))

    result = resp["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "hello-from-skill"

    await pool.close()


@pytest.mark.asyncio
async def test_tools_call_unknown_skill_returns_is_error(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(4, "tools/call", {  # noqa: SLF001
        "name": "run_skill_turn",
        "arguments": {"skill_id": "ghost-skill", "message": "hi"},
    }))

    # 业务错误走 isError，而非 JSON-RPC 顶层错误
    assert "error" not in resp
    result = resp["result"]
    assert result["isError"] is True
    assert "ghost-skill" in result["content"][0]["text"]

    await pool.close()


@pytest.mark.asyncio
async def test_tools_call_missing_message_returns_invalid_params(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(5, "tools/call", {  # noqa: SLF001
        "name": "run_skill_turn",
        "arguments": {"skill_id": "code-reviewer"},  # 缺 message
    }))

    # 参数缺失走 JSON-RPC -32602
    assert "result" not in resp
    assert resp["error"]["code"] == -32602
    assert "message" in resp["error"]["message"].lower()

    await pool.close()


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_name_returns_method_not_found(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(6, "tools/call", {  # noqa: SLF001
        "name": "explode_universe",
        "arguments": {},
    }))

    assert resp["error"]["code"] == -32601
    await pool.close()


# --------------------------------------------------------------------
# Requirement: resources/list + read
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resources_list_exposes_all_skills(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(7, "resources/list", {}))  # noqa: SLF001

    resources = resp["result"]["resources"]
    # conftest fixture 提供 2 个 skill：style-checker + code-reviewer
    assert len(resources) == 2
    uris = {r["uri"] for r in resources}
    assert f"{SKILL_URI_PREFIX}style-checker" in uris
    assert f"{SKILL_URI_PREFIX}code-reviewer" in uris
    for r in resources:
        assert r["mimeType"] == "text/markdown"

    await pool.close()


@pytest.mark.asyncio
async def test_resources_read_returns_skill_body(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    uri = f"{SKILL_URI_PREFIX}style-checker"
    resp = await server._handle_line(_req(8, "resources/read", {"uri": uri}))  # noqa: SLF001

    contents = resp["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == uri
    assert contents[0]["mimeType"] == "text/markdown"
    # fixture ATOMIC_SKILL body 含 "风格审查"
    assert "风格审查" in contents[0]["text"]

    await pool.close()


@pytest.mark.asyncio
async def test_resources_read_unknown_skill_returns_invalid_params(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(9, "resources/read", {  # noqa: SLF001
        "uri": f"{SKILL_URI_PREFIX}ghost",
    }))

    assert resp["error"]["code"] == -32602
    assert "ghost" in resp["error"]["message"]
    await pool.close()


@pytest.mark.asyncio
async def test_resources_read_rejects_non_taifeng_scheme(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(10, "resources/read", {  # noqa: SLF001
        "uri": "file:///etc/passwd",
    }))

    assert resp["error"]["code"] == -32602
    await pool.close()


# --------------------------------------------------------------------
# Requirement: JSON-RPC 错误处理
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(_req(11, "tools/explode", {}))  # noqa: SLF001

    assert resp["error"]["code"] == -32601
    await pool.close()


@pytest.mark.asyncio
async def test_parse_error_when_stdin_not_json(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)
    resp = await server._handle_line(b"not-json-at-all\n")  # noqa: SLF001

    assert resp["error"]["code"] == -32700
    # parse error 时 id 为 null
    assert resp["id"] is None
    await pool.close()


@pytest.mark.asyncio
async def test_notifications_method_returns_no_response(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """JSON-RPC notification（无 id）/ notifications/* method 不需要响应。"""
    pool = await _build_pool(skills_dir, threads_dir)
    server = McpStdioServer(pool)

    # 1) notifications/initialized → no response
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }) + "\n"
    resp = await server._handle_line(payload.encode("utf-8"))  # noqa: SLF001
    assert resp is None

    await pool.close()
