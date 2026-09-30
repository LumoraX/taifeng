"""互通验证：taifeng 的 MCP 客户端对接官方 MCP Python SDK 写的 server。

内核的 MCP 测试用的是自带的假 server 与 httpx ``MockTransport``——能验证内核按自己对规范的理解
工作，验证不了「对规范的理解与别人一致」。本脚本拿官方 SDK 的 server 当对端，走真实的 stdio 子
进程与真实的 HTTP 连接：

0. 三种接法各跑一遍：直接拉起的 stdio 子进程、经 ``CommandExecutor`` 启动的 stdio 子进程、
   streamable HTTP；
1. 协议版本协商（官方 SDK 的最新版本比内核声明的新，须协商到双方都支持的版本）；
2. ``tools/list``、``tools/call``（文本结果、结构化结果）；
3. ``bind_mcp_tools`` 注册成 ``ToolSpec`` 后经工具 handler 调用；
4. server 运行期增加工具并发 ``tools/list_changed``，绑定自动同步。

需要官方 SDK，不在默认依赖里：

    cd taifeng
    PYTHONPATH=src uv run --with mcp python examples/mcp_interop/verify.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import Any

import httpx

from taifeng import (
    CancellationToken,
    LocalCommandExecutor,
    McpHttpClient,
    McpStdioClient,
    ToolContext,
    ToolRegistry,
    bind_mcp_tools,
)

_SERVER = str(Path(__file__).with_name("server.py"))
_failures: list[str] = []


def _check(condition: bool, label: str) -> None:
    print(f"  {'✅' if condition else '❌'} {label}")
    if not condition:
        _failures.append(label)


async def _until(predicate: Any, *, seconds: float = 10.0) -> bool:
    """轮询到条件成立（list_changed 的同步在后台进行）。"""
    for _ in range(int(seconds / 0.05)):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _exercise(client: Any, label: str) -> None:
    """同一套检查，两种传输各跑一遍。"""
    print(f"\n[{label}] server={client.server_info.get('name')} 协议版本={client.protocol_version}")
    _check(client.server_info.get("name") == "interop-reference", "initialize 握手完成")
    _check(client.protocol_version == "2025-06-18", "协商到内核声明的协议版本 2025-06-18")

    listed = {tool["name"] for tool in await client.list_tools()}
    _check({"add", "echo", "install_extra"} <= listed, f"tools/list 列出三个工具（{sorted(listed)}）")
    _check("extra" not in listed, "extra 此时还不存在")

    registry = ToolRegistry()
    binding = await bind_mcp_tools(client, registry, tool_prefix="ref_")
    _check({"ref_add", "ref_echo", "ref_install_extra"} <= binding.owned, "bind_mcp_tools 注册为 ToolSpec")

    def ctx(call_id: str) -> ToolContext:
        return ToolContext(call_id=call_id, cancel=CancellationToken(), thread_id="interop")

    echoed = await registry.get("ref_echo").handler({"text": "你好 MCP"}, ctx("c1"))  # type: ignore[union-attr]
    _check(not echoed.is_error and "你好 MCP" in echoed.output, f"文本结果原样返回（{echoed.output!r}）")
    added = await registry.get("ref_add").handler({"a": 17, "b": 25}, ctx("c2"))  # type: ignore[union-attr]
    _check(not added.is_error and "42" in added.output, f"结构化结果可读（{added.output!r}）")
    bad = await registry.get("ref_add").handler({"a": "x", "b": 1}, ctx("c3"))  # type: ignore[union-attr]
    _check(bad.is_error, "server 拒绝的参数落成错误结果，不抛异常")

    installed = await registry.get("ref_install_extra").handler({}, ctx("c4"))  # type: ignore[union-attr]
    _check(not installed.is_error, "install_extra 调用成功")
    synced = await _until(lambda: "ref_extra" in binding.owned)
    _check(synced, "tools/list_changed 之后绑定自动同步出新工具")
    if synced:
        extra = await registry.get("ref_extra").handler({}, ctx("c5"))  # type: ignore[union-attr]
        _check(not extra.is_error and "extra-ready" in extra.output, "新工具可调用")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_listening(port: int, proc: asyncio.subprocess.Process) -> None:
    for _ in range(200):
        if proc.returncode is not None:
            raise RuntimeError(f"reference server exited with {proc.returncode}")
        try:
            async with httpx.AsyncClient() as http:
                await http.get(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
            return
        except httpx.TransportError:
            await asyncio.sleep(0.05)
    raise RuntimeError("reference server did not start listening")


async def main() -> int:
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        print("❌ 需要官方 MCP SDK：PYTHONPATH=src uv run --with mcp python examples/mcp_interop/verify.py")
        return 2

    stdio = await McpStdioClient.spawn([sys.executable, _SERVER, "stdio"])
    try:
        await _exercise(stdio, "stdio")
    finally:
        await stdio.close()

    # 同一个 server 经 CommandExecutor 启动（宿主把 server 放进沙盒时走的路径，ADR 0112）
    sandboxed = await McpStdioClient.spawn(
        [sys.executable, _SERVER, "stdio"], executor=LocalCommandExecutor())
    try:
        await _exercise(sandboxed, "stdio 经 CommandExecutor")
    finally:
        await sandboxed.close()

    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, _SERVER, "http", str(port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _wait_listening(port, proc)
        http = await McpHttpClient.connect(f"http://127.0.0.1:{port}/mcp")
        try:
            await _exercise(http, "streamable HTTP")
        finally:
            await http.close()
    finally:
        proc.terminate()
        await proc.wait()

    print("\n✅ 与官方 MCP SDK server 互通验证全部通过" if not _failures else f"\n❌ 失败 {len(_failures)} 项")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
