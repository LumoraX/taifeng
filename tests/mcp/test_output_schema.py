"""MCP 工具 ``outputSchema`` 校验（output_schema.py + 桥 handler + sync 替换判定）。

规范（2025-06-18 §Tools「Output Schema」）：声明了 outputSchema 的工具，server MUST 返回合规的
structuredContent，客户端 SHOULD 校验。不合规 → 该次调用判错（``mcp_output_schema_violation``），
未通过校验的内容不交给模型；``isError: true`` 的结果不校验。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.mcp import McpHttpClient, bind_mcp_tools
from taifeng.mcp.output_schema import (
    McpOutputSchemaError,
    parse_output_schema,
    structured_output_violations,
)
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.spec import ToolContext, ToolResult

WEATHER_SCHEMA = {
    "type": "object",
    "properties": {"temperature": {"type": "number"}, "conditions": {"type": "string"}},
    "required": ["temperature", "conditions"],
}


def _ctx() -> ToolContext:
    return ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")


class _FakeClient:
    """进程内 McpClient 替身：tools/list 与 tools/call 的返回均可按测试改写。"""

    server_info: dict[str, Any] = {"name": "fake"}

    def __init__(self, tools: list[dict[str, Any]], result: dict[str, Any]) -> None:
        self.tools = tools
        self.result = result

    async def list_tools(self) -> list[dict[str, Any]]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.result

    def add_tools_changed_listener(self, listener: Any) -> None:
        return None


def _weather_tool(schema: Any = WEATHER_SCHEMA) -> dict[str, Any]:
    return {"name": "weather", "inputSchema": {"type": "object"}, "outputSchema": schema}


async def _call(client: _FakeClient, name: str = "weather") -> ToolResult:
    registry = ToolRegistry()
    await bind_mcp_tools(client, registry, watch=False)
    return await registry.require(name).handler({}, _ctx())


# ---------------------------------------------------------------- 纯函数


def test_parse_output_schema_absent_valid_and_malformed() -> None:
    """未声明 → None；object schema 原样返回；非对象 / 根类型非 object → 显式错误。"""
    assert parse_output_schema({"name": "x"}) is None
    assert parse_output_schema({"name": "x", "outputSchema": WEATHER_SCHEMA}) is WEATHER_SCHEMA
    for bad in ("string", {"type": "array"}, {"properties": {}}):
        with pytest.raises(McpOutputSchemaError, match="outputSchema must be an object schema"):
            parse_output_schema({"name": "x", "outputSchema": bad})


def test_violations_skip_error_results_and_flag_missing_or_mismatch() -> None:
    """isError 不校验；缺 structuredContent / 类型不符 / 缺必填 → 违例；合规 → 空。"""
    assert structured_output_violations(WEATHER_SCHEMA, None, is_error=True) == []
    assert structured_output_violations(WEATHER_SCHEMA, None, is_error=False) == [
        "$: structuredContent is missing but the tool declares an outputSchema"]
    violations = structured_output_violations(
        WEATHER_SCHEMA, {"temperature": "hot"}, is_error=False)
    assert "$.temperature: expected number, got string" in violations
    assert "$: missing required property 'conditions'" in violations
    assert structured_output_violations(
        WEATHER_SCHEMA, {"temperature": 22.5, "conditions": "晴"}, is_error=False) == []


# ---------------------------------------------------------------- 桥 handler


async def test_conforming_structured_content_passes() -> None:
    """合规结果照常投影：structured_content 进 data，output 为文本。"""
    result = await _call(_FakeClient([_weather_tool()], {
        "content": [{"type": "text", "text": "22.5°C 晴"}],
        "structuredContent": {"temperature": 22.5, "conditions": "晴"}}))
    assert not result.is_error
    assert result.output == '22.5°C 晴\n{"temperature": 22.5, "conditions": "晴"}'
    assert result.data["structured_content"] == {"temperature": 22.5, "conditions": "晴"}


async def test_violating_structured_content_is_an_error_without_payload() -> None:
    """不合规 → mcp_output_schema_violation，违例进 data；server 的原文不进 output。"""
    result = await _call(_FakeClient([_weather_tool()], {
        "content": [{"type": "text", "text": "秘密原文"}],
        "structuredContent": {"temperature": "hot", "conditions": "晴"}}))
    assert result.is_error
    assert result.data["reason"] == "mcp_output_schema_violation"
    assert result.data["mcp_tool"] == "weather"
    assert result.data["violations"] == ["$.temperature: expected number, got string"]
    assert result.output.startswith("mcp_output_schema_violation: $.temperature")
    assert "秘密原文" not in result.output


async def test_missing_structured_content_is_an_error() -> None:
    """声明了 outputSchema 却只给文本 → 判错（规范 MUST 由 server 提供结构化结果）。"""
    result = await _call(_FakeClient([_weather_tool()], {
        "content": [{"type": "text", "text": "22.5"}]}))
    assert result.is_error
    assert result.data["reason"] == "mcp_output_schema_violation"


async def test_error_results_and_undeclared_tools_are_not_validated() -> None:
    """isError 结果原样透传；未声明 outputSchema 的工具不做任何校验。"""
    failed = await _call(_FakeClient([_weather_tool()], {
        "content": [{"type": "text", "text": "上游限流"}], "isError": True}))
    assert failed.is_error and failed.output == "上游限流"
    assert "reason" not in failed.data
    plain = await _call(_FakeClient(
        [{"name": "weather", "inputSchema": {"type": "object"}}],
        {"content": [], "structuredContent": {"anything": [1, 2]}}))
    assert not plain.is_error


async def test_malformed_output_schema_skips_tool(caplog: pytest.LogCaptureFixture) -> None:
    """outputSchema 形状非法的工具不注册（并告警），同列表其他工具照常注册。"""
    client = _FakeClient(
        [_weather_tool({"type": "array"}), {"name": "ok", "inputSchema": {"type": "object"}}],
        {"content": []})
    registry = ToolRegistry()
    with caplog.at_level(logging.WARNING, logger="taifeng.mcp.bridge"):
        binding = await bind_mcp_tools(client, registry, watch=False)
    assert binding.owned == {"ok"}
    assert "weather" in caplog.text and "outputSchema" in caplog.text


# ---------------------------------------------------------------- sync 替换判定


async def test_output_schema_change_replaces_tool_and_revalidates() -> None:
    """同名工具只有 outputSchema 变化 → 替换；新 handler 按新 schema 校验；撤销声明也替换。"""
    client = _FakeClient([_weather_tool()], {
        "content": [], "structuredContent": {"temperature": 22.5, "conditions": "晴"}})
    registry = ToolRegistry()
    binding = await bind_mcp_tools(client, registry, watch=False)
    assert binding.output_schemas == {"weather": WEATHER_SCHEMA}
    assert await binding.sync() == ([], [], [])  # 未变化不替换

    stricter = {**WEATHER_SCHEMA, "required": ["temperature", "conditions", "humidity"]}
    client.tools = [_weather_tool(stricter)]
    assert await binding.sync() == ([], [], ["weather"])
    assert binding.output_schemas == {"weather": stricter}
    result = await registry.require("weather").handler({}, _ctx())
    assert result.data["violations"] == ["$: missing required property 'humidity'"]

    client.tools = [{"name": "weather", "inputSchema": {"type": "object"}}]
    assert await binding.sync() == ([], [], ["weather"])
    assert binding.output_schemas == {}
    assert not (await registry.require("weather").handler({}, _ctx())).is_error


# ---------------------------------------------------------------- HTTP 端到端


def _http_server(structured: dict[str, Any]) -> httpx.MockTransport:
    """最小 streamable HTTP server：一个声明 outputSchema 的工具，调用回给定的结构化结果。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method in ("GET", "DELETE"):
            return httpx.Response(405)
        msg = json.loads(request.content)
        mid = msg.get("id")
        if mid is None:
            return httpx.Response(202)
        results = {
            "initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "schema"}},
            "tools/list": {"tools": [_weather_tool()]},
            "tools/call": {"content": [], "structuredContent": structured},
        }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid,
                                         "result": results[msg["method"]]})

    return httpx.MockTransport(_handler)


@pytest.mark.parametrize(("structured", "is_error"), [
    ({"temperature": 1.5, "conditions": "阴"}, False),
    ({"temperature": 1.5}, True),
])
async def test_http_bound_tool_validates_structured_content(
    structured: dict[str, Any], is_error: bool,
) -> None:
    """经 HTTP 传输绑定的工具同样按 outputSchema 校验（合规通过 / 缺必填判错）。"""
    client = await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=_http_server(structured))
    registry = ToolRegistry()
    try:
        await bind_mcp_tools(client, registry, watch=False)
        result = await registry.require("weather").handler({}, _ctx())
    finally:
        await client.close()
    assert result.is_error is is_error
