"""MCP stdio JSON-RPC 2.0 客户端。

协议：
    - request:  {"jsonrpc": "2.0", "id": int, "method": str, "params": dict}
    - response: {"jsonrpc": "2.0", "id": int, "result": ...} | {"jsonrpc": "2.0", "id": int, "error": {...}}
    - 每条消息以单行 JSON 表示，stdin/stdout 行分隔

启动外部 server (示例)::

    npx -y @modelcontextprotocol/server-filesystem /tmp
    uvx mcp-server-git --repository /path/to/repo

用法::

    client = await McpStdioClient.spawn(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
    tools = await client.list_tools()
    specs = register_mcp_tools(client, registry)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from taifeng.mcp.bridge import (
    McpToolError,
    extract_text_content,
    register_mcp_tools,
    register_mcp_tools_async,
)
from taifeng.mcp.protocol import initialize_params, negotiate_protocol_version

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

# 兼容旧导入路径（桥接逻辑已迁到 taifeng.mcp.bridge，与传输无关）
_extract_text_content = extract_text_content


class McpStdioClient:
    """单个 MCP server 的 stdio JSON-RPC 客户端。"""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        request_timeout_seconds: float | None = 60.0,
    ) -> None:
        """
        Args:
            proc: 已 spawn 的 MCP server 子进程（stdin/stdout 已 PIPE）
            request_timeout_seconds: 单条 JSON-RPC 请求的超时秒数；
                ``None`` 表示不在 client 层超时（由调用方包装控制）。
                与 ``register_mcp_tools_async`` 的 ``timeout_seconds`` 配合使用时，
                建议设为相同值，避免双层 timeout 互相截断
                （详见 spec config-consistency-fixes A1）。
        """
        self._proc = proc
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._initialized = False
        self._server_info: dict[str, Any] = {}
        # initialize 协商出的协议版本（握手完成前为 None）
        self._protocol_version: str | None = None
        self._request_timeout = request_timeout_seconds
        # dynamic-tool-set：notifications/tools/list_changed 的异步监听者
        self._tools_changed_listeners: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self._listener_tasks: set[asyncio.Task[None]] = set()

    @classmethod
    async def spawn(
        cls,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        request_timeout_seconds: float | None = 60.0,
    ) -> McpStdioClient:
        """fork 一个 MCP server 子进程并完成 JSON-RPC handshake。

        Args:
            request_timeout_seconds: 透传到 ``McpStdioClient.__init__``；
                ``None`` 表示无 client 层 timeout
        """
        if not command:
            raise ValueError("empty command")
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        client = cls(proc, request_timeout_seconds=request_timeout_seconds)
        client._reader_task = asyncio.create_task(client._reader_loop())
        try:
            await client._initialize()
        except Exception:
            await client.close()
            raise
        return client

    # ------------------------------------------------------------------
    # JSON-RPC primitives
    # ------------------------------------------------------------------

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._closed:
            raise RuntimeError("client closed")
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                payload["params"] = params
            line = json.dumps(payload, separators=(",", ":")) + "\n"
            assert self._proc.stdin is not None
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        try:
            if self._request_timeout is None:
                # 无 client 层 timeout：由调用方（如 register_mcp_tools_async 的
                # 外层 wait_for）控制；这里直接等
                return await future
            return await asyncio.wait_for(future, timeout=self._request_timeout)
        except TimeoutError as e:
            self._pending.pop(req_id, None)
            raise McpToolError(-32000, f"request timeout: {method}") from e

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._closed:
            return
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        assert self._proc.stdin is not None
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _reader_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            while not self._closed:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("mcp: invalid JSON: %r", line)
                    continue
                # 响应消息（有 id 字段）
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(McpToolError(err.get("code", -1), err.get("message", "")))
                    else:
                        fut.set_result(msg.get("result"))
                else:
                    self._on_server_message(msg)
        finally:
            # 释放所有未决 future
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("mcp connection closed"))
            self._pending.clear()

    def _on_server_message(self, msg: dict[str, Any]) -> None:
        """服务端主动消息：tools/list_changed 触发监听者，其余记 debug。

        监听者以 task 调度（reader loop 不能被回调阻塞，否则回调里再发请求会死锁：
        响应要靠同一个 reader loop 读回来）。
        """
        method = msg.get("method")
        if method == "notifications/tools/list_changed":
            for listener in list(self._tools_changed_listeners):
                task = asyncio.get_running_loop().create_task(listener())
                self._listener_tasks.add(task)
                task.add_done_callback(self._listener_tasks.discard)
            return
        logger.debug("mcp notification: %s", method)

    def add_tools_changed_listener(self, listener: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """登记 ``notifications/tools/list_changed`` 的异步回调（``bind_mcp_tools`` 使用）。"""
        self._tools_changed_listeners.append(listener)

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    async def _initialize(self) -> None:
        """MCP initialize handshake：声明最新协议版本，校验 server 回的版本后发 initialized。

        Raises:
            McpProtocolVersionError: server 回的版本不在支持清单内（或缺失）——``spawn``
                据此关闭子进程后上抛（规范：客户端不支持该版本 SHOULD 断开）。
        """
        from taifeng import __version__

        result = await self._send_request(
            "initialize", initialize_params(capabilities={}, client_version=__version__))
        self._protocol_version = negotiate_protocol_version(result)
        self._server_info = result.get("serverInfo", {})
        # 必须发 initialized notification
        await self._send_notification("notifications/initialized")
        self._initialized = True
        logger.info(
            "mcp connected: %s v%s",
            self._server_info.get("name", "?"),
            self._server_info.get("version", "?"),
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        """tools/list 返回 tool 元数据列表。"""
        result = await self._send_request("tools/list")
        if not isinstance(result, dict):
            return []
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """tools/call 执行远端 tool。"""
        result = await self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        if not isinstance(result, dict):
            return {"content": []}
        return result

    @property
    def server_info(self) -> dict[str, Any]:
        """initialize 返回的 serverInfo（副本）。"""
        return dict(self._server_info)

    @property
    def protocol_version(self) -> str | None:
        """initialize 协商出的协议版本；握手完成前为 None。"""
        return self._protocol_version

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            try:
                self._proc.stdin.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=3.0)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        # 未完成的 list_changed 同步任务随连接一起取消（连接已断，同步必然失败）
        for task in list(self._listener_tasks):
            task.cancel()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass


__all__ = [
    "McpStdioClient",
    "McpToolError",
    "register_mcp_tools",
    "register_mcp_tools_async",
]
