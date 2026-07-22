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
from typing import Any

from taifeng.tool.registry import ToolRegistry
from taifeng.tool.spec import ToolContext, ToolFunc, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class McpToolError(Exception):
    """MCP 工具调用失败。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


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
        self._request_timeout = request_timeout_seconds

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
                    # 服务端通知 / 当前忽略
                    logger.debug("mcp notification: %s", msg.get("method"))
        finally:
            # 释放所有未决 future
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("mcp connection closed"))
            self._pending.clear()

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    async def _initialize(self) -> None:
        """MCP initialize handshake。"""
        result = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "taifeng", "version": "0.0.1"},
            },
        )
        self._server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
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
        return dict(self._server_info)

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
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass


# ----------------------------------------------------------------------
# Bridge: 把 MCP tool 注册为 Taifeng ToolSpec
# ----------------------------------------------------------------------


def _extract_text_content(result: dict[str, Any]) -> tuple[str, bool]:
    """从 MCP tools/call 结果提取文本 + is_error。"""
    is_error = bool(result.get("isError"))
    content = result.get("content") or []
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            parts.append(f"[image: {item.get('mimeType', 'unknown')}]")
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(parts), is_error


def register_mcp_tools(
    client: McpStdioClient,
    registry: ToolRegistry,
    *,
    tool_prefix: str = "",
    parallel_safe: bool = False,
    timeout_seconds: float = 60.0,
) -> list[ToolSpec]:
    """异步包装：当前是同步入口，内部使用 client.list_tools() 应在 ``await`` 后调。

    为简化使用，这里返回 ``list_tools()`` 的 awaitable 包装 ——
    业务侧应：``specs = await register_mcp_tools_async(client, registry, ...)``。
    """
    raise RuntimeError(
        "use `await register_mcp_tools_async(...)` instead",
    )


async def register_mcp_tools_async(
    client: McpStdioClient,
    registry: ToolRegistry,
    *,
    tool_prefix: str = "",
    parallel_safe: bool = False,
    timeout_seconds: float = 60.0,
) -> list[ToolSpec]:
    """把 MCP server 的所有 tool 注册为 Taifeng ToolSpec。

    Args:
        client: 已 initialize 的 MCP client
        registry: 目标 ToolRegistry
        tool_prefix: 命名前缀（避免与本地 tool 冲突，如 ``"mcp_fs_"``）
        parallel_safe: 默认 False（MCP 工具通常有副作用）
        timeout_seconds: 单次 tools/call 超时
    """
    remote_tools = await client.list_tools()
    registered: list[ToolSpec] = []
    for meta in remote_tools:
        if not isinstance(meta, dict):
            continue
        name = meta.get("name")
        if not name or not isinstance(name, str):
            continue
        description = meta.get("description", "")
        schema = meta.get("inputSchema") or {"type": "object"}

        local_name = f"{tool_prefix}{name}"

        async def _handler_factory(mcp_name: str = name) -> ToolFunc:  # 闭包绑定
            async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
                try:
                    result = await asyncio.wait_for(
                        client.call_tool(mcp_name, args),
                        timeout=timeout_seconds,
                    )
                except McpToolError as e:
                    return ToolResult.error(f"mcp_error: {e}", reason="mcp_error", code=e.code)
                except TimeoutError:
                    return ToolResult.error("mcp_timeout", reason="timeout")
                text, is_error = _extract_text_content(result)
                return ToolResult(
                    output=text,
                    is_error=is_error,
                    data={"mcp_tool": mcp_name},
                )
            return handler

        spec = ToolSpec(
            name=local_name,
            description=f"[MCP] {description}",
            input_schema=schema,
            handler=await _handler_factory(),
            parallel_safe=parallel_safe,
            timeout_seconds=timeout_seconds + 5.0,
        )
        try:
            registry.register(spec)
        except Exception as e:
            logger.warning("failed to register mcp tool %s: %s", local_name, e)
            continue
        registered.append(spec)
    logger.info("registered %d MCP tool(s) from %s", len(registered), client.server_info.get("name"))
    return registered
