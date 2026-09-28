"""server → client 消息路由（stdio / HTTP 两种传输共用）。

MCP 是双向 JSON-RPC：除了本端请求的响应，server 还会主动发

- **请求**（带 id 的 method 消息，如 ``elicitation/create`` / ``ping``）——必须回响应；
- **通知**（无 id，如 ``notifications/tools/list_changed`` / ``notifications/cancelled``）。

旧实现两种传输各自只认 list_changed，其余请求记 debug 后丢弃，server 只能干等到超时。
本路由器统一处理：

| 消息 | 处理 |
| --- | --- |
| ``ping`` 请求 | 立即回空 result（规范 MUST） |
| ``elicitation/create`` 请求 | 注入了 handler → 交给它；未注入 → ``-32601`` |
| 其他请求 | ``-32601 Method not found``（不静默忽略） |
| ``notifications/cancelled`` | 取消对应在飞请求的应答任务，不回响应（规范 SHOULD） |
| ``notifications/tools/list_changed`` | 以 task 调度监听者 |

所有应答 / 监听都以 task 调度：``route`` 在传输的读循环里同步调用，回调若阻塞读循环，
回调里再发请求就会死锁（响应要靠同一个读循环读回来）。``aclose`` 取消并等待全部任务——
客户端关闭即可打断仍在等用户的 handler。

参照：codex ``codex-rs/rmcp-client``（rmcp ``Service`` 按 ``ServerRequest`` 分派、取消通知
映射到在飞请求）；差异：taifeng 手写 JSON-RPC，路由器与传输解耦，只经 ``send`` 回调写出。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, TypeGuard

from taifeng.mcp.elicitation import answer_elicitation
from taifeng.mcp.protocol import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    jsonrpc_error,
    jsonrpc_result,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from taifeng.mcp.elicitation import ElicitationHandler

logger = logging.getLogger(__name__)


def _is_request_id(value: Any) -> TypeGuard[str | int]:
    """JSON-RPC 请求 id 只能是字符串或整数（bool 是 int 子类，须排除）。"""
    return isinstance(value, str | int) and not isinstance(value, bool)


class ServerMessageRouter:
    """一个 MCP 连接上 server 主动消息的路由器。

    Attributes:
        elicitation_handler: 宿主注入的 elicitation 处理器；None = 不支持 elicitation。
    """

    def __init__(
        self,
        *,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        server_info: Callable[[], dict[str, Any]],
        elicitation_handler: ElicitationHandler | None = None,
    ) -> None:
        """构造路由器。

        Args:
            send: 把一条 JSON-RPC 消息写回 server 的传输原语（stdio 写 stdin；HTTP POST）。
            server_info: 取当前 serverInfo 的回调（握手后才有值，故传回调而非快照）。
            elicitation_handler: 宿主注入的 elicitation 处理器；None 则不声明该能力。
        """
        self._send = send
        self._server_info = server_info
        self.elicitation_handler = elicitation_handler
        self._tools_changed_listeners: list[Callable[[], Coroutine[Any, Any, None]]] = []
        # 本路由器派生的全部任务（应答 + 监听），aclose 时统一取消
        self._tasks: set[asyncio.Task[None]] = set()
        # 在飞的 server 请求：id → 应答任务（notifications/cancelled 据此取消）
        self._inflight: dict[str | int, asyncio.Task[None]] = {}
        self._closed = False

    def client_capabilities(self) -> dict[str, Any]:
        """initialize 时声明的客户端能力：注入了 handler 才声明 ``elicitation``。"""
        return {"elicitation": {}} if self.elicitation_handler is not None else {}

    def add_tools_changed_listener(self, listener: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """登记 ``notifications/tools/list_changed`` 的异步回调。"""
        self._tools_changed_listeners.append(listener)

    def route(self, msg: dict[str, Any]) -> None:
        """路由一条带 ``method`` 的 server 消息（读循环内同步调用，不阻塞）。

        Args:
            msg: 已解析的 JSON-RPC 对象，调用方保证含 ``method`` 键。
        """
        if self._closed:
            return
        method = msg.get("method")
        if not isinstance(method, str):
            logger.warning("mcp: server message with non-string method ignored: %r", method)
            return
        if msg.get("id") is not None:
            self._start_request(msg["id"], method, msg.get("params"))
        elif method == "notifications/tools/list_changed":
            for listener in list(self._tools_changed_listeners):
                self._spawn(listener())
        elif method == "notifications/cancelled":
            self._cancel_inflight(msg.get("params"))
        else:
            logger.debug("mcp notification: %s", method)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """把协程派成本路由器持有的任务。"""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_request(self, req_id: Any, method: str, params: Any) -> None:
        """为一条 server 请求派应答任务；id 非法 / 与在飞请求重号 → 回 Invalid Request。"""
        if not _is_request_id(req_id):
            self._spawn(self._deliver(jsonrpc_error(
                None, JSONRPC_INVALID_REQUEST, f"Invalid Request: bad id {req_id!r}")))
            return
        if req_id in self._inflight:
            self._spawn(self._deliver(jsonrpc_error(
                req_id, JSONRPC_INVALID_REQUEST, "Invalid Request: duplicate in-flight id")))
            return
        task = self._spawn(self._serve(req_id, method, params))
        self._inflight[req_id] = task

        def _forget(_done: asyncio.Task[None]) -> None:
            """应答结束（含被取消）后移出在飞表，迟到的 cancelled 通知按未知 id 忽略。"""
            self._inflight.pop(req_id, None)

        task.add_done_callback(_forget)

    async def _serve(self, req_id: str | int, method: str, params: Any) -> None:
        """算出应答并写回；取消（server 撤回 / 客户端关闭）时不写任何响应。"""
        await self._deliver(await self._respond(req_id, method, params))

    async def _respond(self, req_id: str | int, method: str, params: Any) -> dict[str, Any]:
        """按 method 构造 JSON-RPC 应答。"""
        if method == "ping":
            return jsonrpc_result(req_id, {})
        if method == "elicitation/create" and self.elicitation_handler is not None:
            return await answer_elicitation(
                req_id, params, self.elicitation_handler, self._server_info())
        # 未注入 handler 的 elicitation 与其余未实现方法同样明确拒绝（未声明的能力）
        return jsonrpc_error(req_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

    async def _deliver(self, response: dict[str, Any]) -> None:
        """写回应答；传输失败只能记日志（server 侧会按自己的超时放弃该请求）。"""
        try:
            await self._send(response)
        except Exception:  # noqa: BLE001 —— 投递失败不能打断路由器，记日志后放弃
            logger.warning("mcp: failed to deliver response for server request %r",
                           response.get("id"), exc_info=True)

    def _cancel_inflight(self, params: Any) -> None:
        """``notifications/cancelled``：取消对应在飞请求；未知 / 已完成的 id 按规范忽略。"""
        request_id = params.get("requestId") if isinstance(params, dict) else None
        task = self._inflight.get(request_id) if _is_request_id(request_id) else None
        if task is None:
            logger.debug("mcp: cancellation for unknown or finished request %r", request_id)
            return
        # 走到这里 params 必为对象（requestId 取自它）
        logger.info("mcp: server cancelled request %r (%s)", request_id, params.get("reason"))
        task.cancel()

    async def aclose(self) -> None:
        """停止路由并取消 / 等待全部任务（客户端关闭时调用；不再写任何响应）。"""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        # 取消与任务内异常都属预期收尾，由 gather 收集而非外抛
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["ServerMessageRouter"]
