"""MCP 协议版本协商（protocol.py + stdio / HTTP 客户端 + server）。

规范（2025-06-18 §Lifecycle）：客户端发自己支持的最新版；server 支持则原样回，否则回
它支持的版本；客户端不支持 server 回的版本 SHOULD 断开。HTTP 下协商后每个请求带
``MCP-Protocol-Version: <协商版本>``。
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from taifeng.mcp import McpHttpClient, McpStdioClient
from taifeng.mcp.bridge import McpToolError
from taifeng.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpProtocolVersionError,
    negotiate_protocol_version,
    select_server_protocol_version,
)
from taifeng.mcp.server import McpStdioServer
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

# 回显客户端 initialize 参数、按 argv[1] 决定回哪个版本（"__missing__" = 不回该字段）
_VERSION_SERVER = r"""
import json, sys

reply_version = sys.argv[1]

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        result = {"serverInfo": {"name": "ver", "seen": msg.get("params")}}
        if reply_version != "__missing__":
            result["protocolVersion"] = reply_version
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": []}})
"""


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "ver_server.py"
    path.write_text(textwrap.dedent(_VERSION_SERVER), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 纯函数


def test_latest_is_2025_06_18_and_first_in_supported() -> None:
    """客户端声明最新版 2025-06-18，且它排在支持清单首位。"""
    assert LATEST_PROTOCOL_VERSION == "2025-06-18"
    assert SUPPORTED_PROTOCOL_VERSIONS[0] == LATEST_PROTOCOL_VERSION


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
def test_negotiate_accepts_supported_versions(version: str) -> None:
    """清单内任一版本都可接受（旧 server 回旧版不断开）。"""
    assert negotiate_protocol_version({"protocolVersion": version}) == version


@pytest.mark.parametrize(("result", "received"), [
    ({"protocolVersion": "2099-01-01"}, "2099-01-01"),
    ({"protocolVersion": 20250618}, 20250618),
    ({}, None),
    ("not-an-object", None),
])
def test_negotiate_rejects_unsupported_or_missing(result: Any, received: Any) -> None:
    """清单外 / 非字符串 / 缺失 → McpProtocolVersionError（McpToolError 子类，code=-32602）。"""
    with pytest.raises(McpProtocolVersionError) as exc_info:
        negotiate_protocol_version(result)
    err = exc_info.value
    assert isinstance(err, McpToolError)
    assert err.code == -32602
    assert err.received == received
    assert err.requested == LATEST_PROTOCOL_VERSION


@pytest.mark.parametrize(("requested", "expected"), [
    ("2025-06-18", "2025-06-18"),
    ("2025-03-26", "2025-03-26"),
    ("2024-11-05", "2024-11-05"),
    ("2099-01-01", LATEST_PROTOCOL_VERSION),
    (None, LATEST_PROTOCOL_VERSION),
    (["2025-06-18"], LATEST_PROTOCOL_VERSION),
])
def test_server_selection_echoes_supported_else_latest(requested: Any, expected: str) -> None:
    """server 侧：请求版本受支持原样回，否则回最新版（非字符串不炸）。"""
    assert select_server_protocol_version(requested) == expected


# ---------------------------------------------------------------- stdio


async def test_stdio_declares_latest_and_records_negotiated(tmp_path: Path) -> None:
    """initialize 发 2025-06-18；server 原样回 → protocol_version 为其值；不再误声明 tools。"""
    client = await McpStdioClient.spawn(
        [sys.executable, str(_script(tmp_path)), "2025-06-18"])
    try:
        seen = client.server_info["seen"]
        assert seen["protocolVersion"] == LATEST_PROTOCOL_VERSION
        assert "tools" not in seen["capabilities"]  # tools 是 server 能力，客户端不声明
        assert client.protocol_version == "2025-06-18"
    finally:
        await client.close()


async def test_stdio_accepts_older_supported_version(tmp_path: Path) -> None:
    """server 只会 2024-11-05 → 在支持清单内，照常建连（记录协商结果）。"""
    client = await McpStdioClient.spawn(
        [sys.executable, str(_script(tmp_path)), "2024-11-05"])
    try:
        assert client.protocol_version == "2024-11-05"
        assert await client.list_tools() == []
    finally:
        await client.close()


@pytest.mark.parametrize("reply", ["2099-01-01", "__missing__"])
async def test_stdio_unsupported_version_disconnects(
    tmp_path: Path, reply: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """server 回清单外版本 / 缺版本 → spawn 抛错且子进程已被关闭（不静默继续）。"""
    procs: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def _spy(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_exec(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy)
    with pytest.raises(McpProtocolVersionError):
        await McpStdioClient.spawn([sys.executable, str(_script(tmp_path)), reply])
    assert len(procs) == 1
    assert procs[0].returncode is not None


# ---------------------------------------------------------------- HTTP


class _VersionHttpServer:
    """记录每个请求的版本头；initialize 回 ``reply_version``。"""

    def __init__(self, reply_version: str) -> None:
        self.reply_version = reply_version
        self.seen: list[tuple[str, str | None, str | None]] = []  # (method/动词, 版本头, 会话头)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        version = request.headers.get("mcp-protocol-version")
        session = request.headers.get("mcp-session-id")
        if request.method in ("GET", "DELETE"):
            self.seen.append((request.method, version, session))
            return httpx.Response(405 if request.method == "GET" else 200)
        msg = json.loads(request.content)
        self.seen.append((msg.get("method"), version, session))
        mid = msg.get("id")
        if mid is None:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": self.reply_version, "serverInfo": {"name": "v"}}},
                headers={"Mcp-Session-Id": "S-9"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {"tools": []}})


async def test_http_headers_carry_negotiated_version() -> None:
    """initialize 不带版本头；之后 POST / GET / DELETE 都带**协商后**的版本（此处 2025-03-26）。"""
    server = _VersionHttpServer("2025-03-26")
    client = await McpHttpClient.connect(
        "https://mcp.example/mcp", transport=httpx.MockTransport(server))
    try:
        assert client.protocol_version == "2025-03-26"
        await client.list_tools()
        # GET 推送流在后台任务里打开，等它真正发出再关（否则 close 可能先取消它）
        await wait_for_condition(lambda: any(m == "GET" for m, _, _ in server.seen))
    finally:
        await client.close()
    by_method = {method: (version, session) for method, version, session in server.seen}
    assert by_method["initialize"] == (None, None)
    for method in ("notifications/initialized", "tools/list", "GET", "DELETE"):
        assert by_method[method] == ("2025-03-26", "S-9"), method


async def test_http_unsupported_version_disconnects_and_ends_session() -> None:
    """清单外版本 → connect 抛错；已分配的会话被 DELETE 结束，且未发 initialized。"""
    server = _VersionHttpServer("2099-01-01")
    with pytest.raises(McpProtocolVersionError, match="2099-01-01"):
        await McpHttpClient.connect(
            "https://mcp.example/mcp", transport=httpx.MockTransport(server))
    methods = [method for method, _, _ in server.seen]
    assert methods == ["initialize", "DELETE"]


# ---------------------------------------------------------------- server


async def test_server_initialize_negotiates_version(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """taifeng 作为 server：受支持的请求版本原样回，不受支持回最新版。"""
    import taifeng
    from taifeng.llm.providers import SimClient

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=SimClient(turns=[]),
        compressors=[])
    server = McpStdioServer(pool)
    try:
        for requested, expected in (("2025-03-26", "2025-03-26"),
                                    ("1.0.0", LATEST_PROTOCOL_VERSION)):
            line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": requested}}).encode() + b"\n"
            resp = await server._handle_line(line)  # noqa: SLF001
            assert resp is not None
            assert resp["result"]["protocolVersion"] == expected
    finally:
        await pool.close()
