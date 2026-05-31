"""MCP stdio request timeout 配置生效测试（config-consistency-fixes T1 / A1）。

验证 ``McpStdioClient.__init__`` / ``spawn`` 的 ``request_timeout_seconds`` kwarg
真正生效——之前 ``_send_request`` 硬编码 ``timeout=60.0``，
即使外层 ``register_mcp_tools_async`` 调大 ``timeout_seconds`` 也被静默截断。

策略：跑一个永远不响应的 dummy 子进程，构造 client 时给极小 timeout，
``_initialize`` 会触发 JSON-RPC 请求并在该 timeout 上 fire。
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from taifeng.mcp.stdio_client import McpStdioClient, McpToolError


def test_init_accepts_request_timeout_seconds_kwarg() -> None:
    """McpStdioClient.__init__ 暴露 request_timeout_seconds，默认 60.0。"""
    sig = inspect.signature(McpStdioClient.__init__)
    p = sig.parameters.get("request_timeout_seconds")
    assert p is not None, "request_timeout_seconds 必须是构造 kwarg"
    assert p.default == 60.0, f"默认值应为 60.0，实际 {p.default}"


def test_spawn_accepts_request_timeout_seconds_kwarg() -> None:
    """McpStdioClient.spawn 同样暴露 request_timeout_seconds 透传。"""
    sig = inspect.signature(McpStdioClient.spawn)
    p = sig.parameters.get("request_timeout_seconds")
    assert p is not None
    assert p.default == 60.0


@pytest.mark.asyncio
async def test_request_timeout_fires_at_configured_value() -> None:
    """构造 timeout=0.2s + 不响应的 server → 0.2s 内见 McpToolError(timeout)。

    之前 bug：内层硬编码 60s，必须等 60s 才报错；
    fix 后：内层用配置值，0.2s 触发。
    """
    # 用 python 子进程：只读 stdin 不回写 stdout，模拟"永不响应"
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c",
        "import sys; sys.stdin.read()",  # 读到 EOF 之前一直阻塞
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = McpStdioClient(proc, request_timeout_seconds=0.2)
    # 启动 reader（spawn 内部会做，但我们直接构造客户端时要手动跑）
    client._reader_task = asyncio.create_task(client._reader_loop())  # noqa: SLF001

    start = asyncio.get_event_loop().time()
    with pytest.raises(McpToolError) as exc_info:
        await client._send_request("initialize", {"protocolVersion": "2024-11-05"})  # noqa: SLF001
    elapsed = asyncio.get_event_loop().time() - start

    assert "request timeout" in str(exc_info.value)
    assert exc_info.value.code == -32000
    # 真正按配置 0.2s 触发，宽松上界 2s 避免 CI 抖动
    assert elapsed < 2.0, (
        f"应在 ~0.2s 触发 timeout（配置生效），实际 {elapsed:.2f}s "
        f"—— 若 >=60s 说明回退到旧硬编码"
    )

    await client.close()


@pytest.mark.asyncio
async def test_request_timeout_none_disables_inner_timeout() -> None:
    """request_timeout_seconds=None 时 _send_request 不在 client 层超时。

    用外层 wait_for 包一个短超时验证：内层确实没自己 fire。
    """
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c",
        "import sys; sys.stdin.read()",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = McpStdioClient(proc, request_timeout_seconds=None)
    client._reader_task = asyncio.create_task(client._reader_loop())  # noqa: SLF001

    # 外层 wait_for 是唯一 timeout 源 —— 0.3s 必然抛 TimeoutError（asyncio 的），
    # 而不是 McpToolError（client 内部已不超时）
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            client._send_request("initialize", {}),  # noqa: SLF001
            timeout=0.3,
        )

    await client.close()
