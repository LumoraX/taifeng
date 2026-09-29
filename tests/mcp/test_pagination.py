"""``tools/list`` 分页（pagination.py + stdio / HTTP 客户端 + 桥同步）。

规范（2025-06-18 §Pagination）：响应带 ``nextCursor`` 表示还有下一页，客户端原样放进下一次
请求的 ``params.cursor``；缺失即结束。失控的 server（无限翻页 / 重复游标 / 游标非字符串）显式报错，
不静默截断成部分列表。
"""

from __future__ import annotations

import json
import sys
import textwrap
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from taifeng.mcp import McpHttpClient, McpStdioClient, bind_mcp_tools
from taifeng.mcp.bridge import McpToolError
from taifeng.mcp.pagination import McpPaginationError, list_all_tools
from taifeng.tool.registry import ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": name, "inputSchema": {"type": "object"}}


class _Pages:
    """按脚本逐页回 result，并记录每次收到的游标。"""

    def __init__(self, pages: list[Any]) -> None:
        self._pages = pages
        self.cursors: list[str | None] = []

    async def __call__(self, cursor: str | None) -> Any:
        self.cursors.append(cursor)
        return self._pages[len(self.cursors) - 1]


# ---------------------------------------------------------------- 纯函数


async def test_follows_next_cursor_until_absent() -> None:
    """三页按序拼接；首页不带游标，之后原样回传上一页的 nextCursor。"""
    pages = _Pages([
        {"tools": [_tool("a")], "nextCursor": "c1"},
        {"tools": [_tool("b"), _tool("c")], "nextCursor": "c2"},
        {"tools": [_tool("d")]},
    ])
    tools = await list_all_tools(pages)
    assert [t["name"] for t in tools] == ["a", "b", "c", "d"]
    assert pages.cursors == [None, "c1", "c2"]


@pytest.mark.parametrize("terminal", [{}, {"nextCursor": None}, {"nextCursor": ""}])
async def test_missing_null_or_empty_cursor_ends(terminal: dict[str, Any]) -> None:
    """nextCursor 缺失 / null / 空串都视为结束（不分页的 server 照常工作）。"""
    pages = _Pages([{"tools": [_tool("only")], **terminal}])
    assert [t["name"] for t in await list_all_tools(pages)] == ["only"]
    assert pages.cursors == [None]


async def test_page_limit_raises_instead_of_truncating() -> None:
    """server 永远给下一页 → 到上限抛 McpPaginationError，不返回已取到的部分。"""
    calls = 0

    async def _endless(cursor: str | None) -> Any:
        nonlocal calls
        calls += 1
        return {"tools": [_tool(f"t{calls}")], "nextCursor": f"c{calls}"}

    with pytest.raises(McpPaginationError, match="within 3 pages") as exc_info:
        await list_all_tools(_endless, max_pages=3)
    assert calls == 3
    assert exc_info.value.pages == 3
    assert isinstance(exc_info.value, McpToolError)


async def test_repeated_cursor_fails_fast() -> None:
    """同一游标回第二次 → 立即报错（继续翻只会原地打转），不等页数上限。"""
    pages = _Pages([
        {"tools": [], "nextCursor": "same"},
        {"tools": [], "nextCursor": "same"},
    ])
    with pytest.raises(McpPaginationError, match="repeated cursor"):
        await list_all_tools(pages, max_pages=50)
    assert pages.cursors == [None, "same"]


@pytest.mark.parametrize(("page", "match"), [
    ({"tools": [], "nextCursor": 7}, "nextCursor must be a string"),
    ({"tools": {"a": 1}}, "'tools' is not an array"),
    ({}, "'tools' is not an array"),
    (["not", "an", "object"], "result is not an object"),
])
async def test_malformed_pages_raise(page: Any, match: str) -> None:
    """游标非字符串 / tools 非数组或缺失 / result 非对象 → 显式错误，不当成空列表。"""
    with pytest.raises(McpToolError, match=match):
        await list_all_tools(_Pages([page]))


async def test_max_pages_must_be_positive() -> None:
    """页数上限 < 1 是配置错误：客户端构造期与函数入口都拒绝。"""
    with pytest.raises(ValueError, match="max_list_pages"):
        await list_all_tools(_Pages([]), max_pages=0)
    with pytest.raises(ValueError, match="max_list_pages"):
        McpHttpClient("https://mcp.example/mcp", max_list_pages=0)
    with pytest.raises(ValueError, match="max_list_pages"):
        await McpStdioClient.spawn([sys.executable, "-c", "pass"], max_list_pages=0)


# ---------------------------------------------------------------- stdio

# argv[1] = 总页数（"endless" = 永远给下一页）；每页一个工具，回显收到的 params
_PAGED_SERVER = r"""
import json, sys

mode = sys.argv[1]
seen = []

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18", "serverInfo": {"name": "paged"}}})
    elif method == "tools/list":
        params = msg.get("params")
        seen.append(params)
        page = len(seen)
        result = {"tools": [{"name": f"t{page}", "inputSchema": {"type": "object"}}]}
        if mode == "endless" or page < int(mode):
            result["nextCursor"] = f"cursor-{page}"
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(seen)}]}})
"""


def _paged_server(tmp_path: Path, mode: str) -> list[str]:
    path = tmp_path / "paged_server.py"
    path.write_text(textwrap.dedent(_PAGED_SERVER), encoding="utf-8")
    return [sys.executable, str(path), mode]


async def test_stdio_bind_registers_tools_from_every_page(tmp_path: Path) -> None:
    """stdio：绑定走分页后的完整列表；首页无 params，后续带 cursor。"""
    client = await McpStdioClient.spawn(_paged_server(tmp_path, "3"))
    registry = ToolRegistry()
    try:
        binding = await bind_mcp_tools(client, registry, watch=False)
        seen = json.loads((await client.call_tool("seen", {}))["content"][0]["text"])
    finally:
        await client.close()
    assert binding.owned == {"t1", "t2", "t3"}
    assert seen == [None, {"cursor": "cursor-1"}, {"cursor": "cursor-2"}]


async def test_stdio_endless_server_fails_bind(tmp_path: Path) -> None:
    """stdio：无限翻页的 server → 绑定失败（McpPaginationError），注册表不留半截工具。"""
    client = await McpStdioClient.spawn(_paged_server(tmp_path, "endless"), max_list_pages=4)
    registry = ToolRegistry()
    try:
        with pytest.raises(McpPaginationError, match="within 4 pages"):
            await bind_mcp_tools(client, registry, watch=False)
    finally:
        await client.close()
    assert registry.names() == frozenset()


# ---------------------------------------------------------------- HTTP


class _PagedHttpServer:
    """streamable HTTP fake：tools/list 分两页，第二次 sync 时第二页换了工具。"""

    def __init__(self) -> None:
        self.cursors: list[Any] = []
        self.second_page = [_tool("beta")]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method in ("GET", "DELETE"):
            return httpx.Response(405)
        msg = json.loads(request.content)
        mid = msg.get("id")
        if mid is None:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "paged-http"}}})
        cursor = (msg.get("params") or {}).get("cursor")
        self.cursors.append(cursor)
        result: dict[str, Any] = (
            {"tools": [_tool("alpha")], "nextCursor": "p2"} if cursor is None
            else {"tools": self.second_page})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": result})


async def test_http_pagination_and_sync_see_full_list() -> None:
    """HTTP：跟完两页；再次 sync 时第二页的变化同样生效（增删判定基于完整列表）。"""
    server = _PagedHttpServer()
    client = await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server))
    registry = ToolRegistry()
    try:
        binding = await bind_mcp_tools(client, registry, watch=False)
        assert binding.owned == {"alpha", "beta"}
        server.second_page = [_tool("gamma")]
        added, removed, replaced = await binding.sync()
    finally:
        await client.close()
    assert (added, removed, replaced) == (["gamma"], ["beta"], [])
    assert server.cursors == [None, "p2", None, "p2"]
