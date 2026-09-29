"""测试夹具：扮演 MCP 客户端对内存管道上的 ``McpStdioServer`` 完成 initialize。

server 只向在 initialize 声明了对应能力的客户端发 server → client 请求（如
``elicitation/create`` 要求 ``capabilities.elicitation``）；依赖 elicitation 的用例先调本函数。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    import asyncio

ELICITATION: dict[str, Any] = {"elicitation": {}}


def _answered(written: list[bytes], req_id: str) -> bool:
    """server 是否已写出对 ``req_id`` 的响应。"""
    for raw in written:
        text = raw.decode("utf-8").strip()
        if text and json.loads(text).get("id") == req_id:
            return True
    return False


async def negotiate(
    reader: asyncio.StreamReader,
    written: list[bytes],
    capabilities: dict[str, Any],
) -> None:
    """发 initialize（声明 ``capabilities``）+ initialized，等到响应后清空写出缓冲。

    清空缓冲让调用方的「server 写出的第一条 / 最后一条」断言只看到握手之后的消息。
    """
    init = {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": capabilities,
        "clientInfo": {"name": "test-host", "version": "0"}}}
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    reader.feed_data((json.dumps(init) + "\n" + json.dumps(initialized) + "\n").encode("utf-8"))
    await wait_for_condition(lambda: _answered(written, "init"),
                             message="server 未在守卫期限内回 initialize")
    written.clear()
