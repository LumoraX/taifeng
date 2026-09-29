"""MCP stdio JSON-RPC 2.0 客户端。

协议：
    - request:  {"jsonrpc": "2.0", "id": int, "method": str, "params": dict}
    - response: {"jsonrpc": "2.0", "id": int, "result": ...}
                | {"jsonrpc": "2.0", "id": int, "error": {...}}
    - 每条消息以单行 JSON 表示，stdin/stdout 行分隔
    - 双向：server 也会发带 id 的请求（``elicitation/create`` / ``ping``）与通知，
      交 ``ServerMessageRouter`` 处理，应答写回 server 的 stdin
    - 本端请求超时 / 被取消而放弃时发 ``notifications/cancelled``（``CancelNotifier``）

启动外部 server (示例)::

    npx -y @modelcontextprotocol/server-filesystem /tmp
    uvx mcp-server-git --repository /path/to/repo

用法::

    client = await McpStdioClient.spawn(
        ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        elicitation_handler=my_handler,  # 可选：处理 server 的 elicitation/create
    )
    tools = await client.list_tools()
    binding = await bind_mcp_tools(client, registry)
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
from taifeng.mcp.cancellation import CancelNotifier, cancel_reason
from taifeng.mcp.pagination import DEFAULT_MAX_LIST_PAGES, list_all_tools, validate_max_pages
from taifeng.mcp.protocol import initialize_params, negotiate_protocol_version
from taifeng.mcp.server_messages import ServerMessageRouter

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from taifeng.mcp.elicitation import ElicitationHandler

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
        elicitation_handler: ElicitationHandler | None = None,
        max_list_pages: int = DEFAULT_MAX_LIST_PAGES,
    ) -> None:
        """
        Args:
            proc: 已 spawn 的 MCP server 子进程（stdin/stdout 已 PIPE）
            request_timeout_seconds: 单条 JSON-RPC 请求的超时秒数；
                ``None`` 表示不在 client 层超时（由调用方包装控制）。
                与 ``register_mcp_tools_async`` 的 ``timeout_seconds`` 配合使用时，
                建议设为相同值，避免双层 timeout 互相截断
                （详见 spec config-consistency-fixes A1）。注意 ``tools/call`` 途中
                server 发起的 elicitation 等待用户的时间也计入该超时。
            elicitation_handler: 可选；注入则 initialize 声明 ``elicitation`` 能力，
                server 的 ``elicitation/create`` 交它处理；不注入则不声明，server 仍发
                时回 ``-32601``。
            max_list_pages: ``tools/list`` 跟 ``nextCursor`` 翻页的页数上限；超限抛
                ``McpPaginationError``（防恶意 server 无限翻页，不静默截断）。
        """
        self._proc = proc
        self._max_list_pages = validate_max_pages(max_list_pages)
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        # 写 server stdin 的互斥锁：本端请求 / 通知与对 server 请求的应答并发写出
        self._lock = asyncio.Lock()
        self._initialized = False
        self._server_info: dict[str, Any] = {}
        # initialize 协商出的协议版本（握手完成前为 None）
        self._protocol_version: str | None = None
        self._request_timeout = request_timeout_seconds
        # server 主动消息（请求 / list_changed / cancelled）的路由器
        self._router = ServerMessageRouter(
            send=self._write_message,
            server_info=lambda: dict(self._server_info),
            elicitation_handler=elicitation_handler,
        )
        # 放弃本端请求时的 notifications/cancelled 后台发送器（close 时收敛）
        self._cancels = CancelNotifier(self._write_message)

    @classmethod
    async def spawn(
        cls,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        request_timeout_seconds: float | None = 60.0,
        elicitation_handler: ElicitationHandler | None = None,
        max_list_pages: int = DEFAULT_MAX_LIST_PAGES,
    ) -> McpStdioClient:
        """fork 一个 MCP server 子进程并完成 JSON-RPC handshake。

        Args:
            request_timeout_seconds: 透传到 ``McpStdioClient.__init__``；
                ``None`` 表示无 client 层 timeout
            elicitation_handler: 透传到 ``McpStdioClient.__init__``。
            max_list_pages: 透传到 ``McpStdioClient.__init__``。

        Raises:
            McpProtocolVersionError: server 回的协议版本不受支持（子进程已关闭）。
        """
        if not command:
            raise ValueError("empty command")
        # 坏配置在拉起子进程之前拒绝（构造期再抛会留下孤儿进程）
        validate_max_pages(max_list_pages)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        client = cls(proc, request_timeout_seconds=request_timeout_seconds,
                     elicitation_handler=elicitation_handler, max_list_pages=max_list_pages)
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

    async def _write_message(self, payload: dict[str, Any]) -> None:
        """把一条 JSON-RPC 消息写到 server stdin（过写锁）。

        Raises:
            RuntimeError: 客户端已关闭。
        """
        if self._closed:
            raise RuntimeError("client closed")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        async with self._lock:
            assert self._proc.stdin is not None
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """发一条带 id 的请求并等待其响应。

        请求写出后因超时或被取消而放弃时，向 server 发 ``notifications/cancelled``（原因：
        超时秒数 / 取消消息）；尚未写出就被取消的请求 server 不知其存在，不发。

        Raises:
            RuntimeError: 客户端已关闭 / 连接断开。
            McpToolError: JSON-RPC error 或超时（-32000）。
            asyncio.CancelledError: 调用方取消（已登记取消通知）。
        """
        if self._closed:
            raise RuntimeError("client closed")
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        written = False
        try:
            await self._write_message(payload)
            written = True
            if self._request_timeout is None:
                # 无 client 层 timeout：由调用方（如 register_mcp_tools_async 的
                # 外层 wait_for）控制；这里直接等
                return await future
            return await asyncio.wait_for(future, timeout=self._request_timeout)
        except TimeoutError as e:
            self._cancels.notify(
                req_id, method, f"client timeout after {self._request_timeout:g}s")
            raise McpToolError(-32000, f"request timeout: {method}") from e
        except asyncio.CancelledError as e:
            if written:
                self._cancels.notify(req_id, method, cancel_reason(e))
            raise
        finally:
            # 超时 / 外层取消后迟到的响应按孤儿处理，不再落到已放弃的 future 上
            self._pending.pop(req_id, None)

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发一条通知（无 id、无响应）；客户端已关闭时不发。"""
        if self._closed:
            return
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_message(payload)

    async def _reader_loop(self) -> None:
        """逐行读 server stdout 并分派；退出时让所有未决请求以连接关闭失败。"""
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
                if not isinstance(msg, dict):
                    logger.warning("mcp: non-object JSON-RPC message ignored: %r", line[:200])
                    continue
                self._dispatch_message(msg)
        finally:
            # 释放所有未决 future
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("mcp connection closed"))
            self._pending.clear()

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """带 ``method`` 的是 server 发起的请求 / 通知（交路由器）；否则按 id 结算本端请求。

        必须先看 ``method``：server 请求与本端请求的 id 各自编号、可能撞号，先按 id 匹配
        会把 server 的请求误当成本端请求的响应。
        """
        if "method" in msg:
            self._router.route(msg)
            return
        msg_id = msg.get("id")
        fut = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
        if fut is None or fut.done():
            logger.warning("mcp: response for unknown or abandoned request id=%r", msg_id)
            return
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            fut.set_exception(McpToolError(err.get("code", -1), err.get("message", "")))
        else:
            fut.set_result(msg.get("result"))

    def add_tools_changed_listener(self, listener: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """登记 ``notifications/tools/list_changed`` 的异步回调（``bind_mcp_tools`` 使用）。"""
        self._router.add_tools_changed_listener(listener)

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

        result = await self._send_request("initialize", initialize_params(
            capabilities=self._router.client_capabilities(), client_version=__version__))
        self._protocol_version = negotiate_protocol_version(result)
        self._server_info = result.get("serverInfo", {})
        # 必须发 initialized notification
        await self._send_notification("notifications/initialized")
        self._initialized = True
        logger.info(
            "mcp connected: %s v%s (protocol %s)",
            self._server_info.get("name", "?"),
            self._server_info.get("version", "?"),
            self._protocol_version,
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        """``tools/list``：跟完 ``nextCursor`` 分页，返回全部工具元数据。

        Raises:
            McpPaginationError: 翻页超过 ``max_list_pages`` / 游标重复 / 游标非字符串。
            McpToolError: 某页形状非法、JSON-RPC 错误或超时。
        """
        return await list_all_tools(self._list_tools_page, max_pages=self._max_list_pages)

    async def _list_tools_page(self, cursor: str | None) -> Any:
        """取一页 ``tools/list``；首页不带 ``params``（兼容不认 cursor 字段的旧 server）。"""
        return await self._send_request(
            "tools/list", {"cursor": cursor} if cursor is not None else None)

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
        """关闭连接：先收敛 server 请求的应答 / 监听任务与未发出的取消通知，再关 stdin、等子进程退出。

        在等用户的 elicitation handler 在这里被取消（不再写任何响应——连接即将断开）。
        """
        if self._closed:
            return
        self._closed = True
        await self._router.aclose()
        await self._cancels.aclose()
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001 —— 关闭期管道已断属预期
                logger.debug("mcp: closing server stdin failed", exc_info=True)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=3.0)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            # 读循环的取消 / 收尾异常属预期，收集而非外抛
            await asyncio.gather(self._reader_task, return_exceptions=True)


__all__ = [
    "McpStdioClient",
    "McpToolError",
    "register_mcp_tools",
    "register_mcp_tools_async",
]
