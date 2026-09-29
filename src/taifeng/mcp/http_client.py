"""MCP streamable HTTP 客户端（2025-03-26 起的 transport；协议版本见 ``taifeng.mcp.protocol``）。

与 ``McpStdioClient`` 同一套能力面（实现 ``taifeng.mcp.bridge.McpClient``），可直接交给
``bind_mcp_tools`` / ``register_mcp_tools_async``。

协议要点（参照 MCP 规范 "Streamable HTTP" + codex ``codex-rs/rmcp-client``）：

- 单一端点；每条 JSON-RPC 消息一次 POST，``Accept: application/json, text/event-stream``；
- 响应要么是单个 JSON 对象，要么是 SSE 流（其中可夹带服务端通知与**请求**，直到出现同 id
  响应）；服务端请求（如 ``elicitation/create``）交 ``ServerMessageRouter``，应答另发一次
  POST 送回（服务端回 202）；
- 纯通知（无 id）的 POST 由服务端回 202；
- initialize 响应头 ``Mcp-Session-Id`` 需在之后每个请求带回；
- 协商完成后每个请求（含 GET 推送流与关闭时的 DELETE）带 ``MCP-Protocol-Version: <协商版本>``；
- GET 端点打开服务端推送流（接收 ``notifications/tools/list_changed`` 等），服务端不支持
  时回 405——此时不监听，工具只能在显式 ``sync`` 时刷新；推送流断开 / 被 server 关闭后按
  SSE 惯例重连（带 ``Last-Event-ID``），连续多次重连都没收到事件才放弃；
- 断流续传（规范「Resumability and Redelivery」）：POST 的 SSE 流在给出响应之前断开，且 server
  给事件带过 id → 以 GET + ``Last-Event-ID`` 续传，至多 ``max_stream_resumptions`` 次；server
  从未给过事件 id、GET 回 405 / 错误、次数用尽 → 显式 ``McpToolError``，不静默等到超时；
- 本端请求超时 / 被取消而放弃时另发一次 POST 送 ``notifications/cancelled``——规范明言
  断开连接不等于取消，要取消必须显式通知；
- 关闭时 DELETE 结束会话（best-effort）。

不含 OAuth：鉴权头由宿主通过 ``headers`` 注入（R1：内核不管凭据来源）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

from taifeng.mcp.bridge import McpToolError
from taifeng.mcp.cancellation import CancelNotifier, cancel_reason
from taifeng.mcp.pagination import DEFAULT_MAX_LIST_PAGES, list_all_tools, validate_max_pages
from taifeng.mcp.protocol import initialize_params, negotiate_protocol_version
from taifeng.mcp.server_messages import ServerMessageRouter
from taifeng.mcp.sse import (
    EVENT_STREAM,
    LAST_EVENT_ID_HEADER,
    SseCursor,
    is_event_stream,
    iter_sse_data,
    require_resumed_stream,
    resume_delay,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from taifeng.mcp.elicitation import ElicitationHandler

logger = logging.getLogger(__name__)

_SESSION_HEADER = "Mcp-Session-Id"
_VERSION_HEADER = "MCP-Protocol-Version"
# 传输层失败统一映射的 JSON-RPC 错误码（实现自定义段）
_TRANSPORT_ERROR = -32000

# 断流续传默认：单条 POST 流至多续传 3 次；推送流连续 3 次重连无事件即放弃
DEFAULT_MAX_STREAM_RESUMPTIONS = 3
# 续传 / 重连的基础间隔（秒），按次数指数退避；server 以 SSE ``retry:`` 给出的间隔优先
DEFAULT_STREAM_RESUME_DELAY_SECONDS = 1.0


class _Stream(Enum):
    """读流结局的哨兵：流结束（EOF / 断开）时仍未拿到期望的响应。"""

    ENDED = "ended"


def _decode_message(data: str) -> dict[str, Any] | None:
    """SSE 事件 data → JSON-RPC 消息；非法 JSON / 非对象记告警后跳过（单条坏消息不打断流）。"""
    try:
        msg = json.loads(data)
    except json.JSONDecodeError:
        logger.warning("mcp http: invalid SSE JSON: %r", data[:200])
        return None
    if not isinstance(msg, dict):
        logger.warning("mcp http: non-object SSE message ignored: %r", data[:200])
        return None
    return msg


class McpHttpClient:
    """单个 MCP server 的 streamable HTTP 客户端。"""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        request_timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        elicitation_handler: ElicitationHandler | None = None,
        max_list_pages: int = DEFAULT_MAX_LIST_PAGES,
        max_stream_resumptions: int = DEFAULT_MAX_STREAM_RESUMPTIONS,
        stream_resume_delay_seconds: float = DEFAULT_STREAM_RESUME_DELAY_SECONDS,
    ) -> None:
        """
        Args:
            url: MCP 端点 URL。
            headers: 额外请求头（如 ``Authorization``），由宿主注入。
            request_timeout_seconds: 单条 JSON-RPC 请求超时（``tools/call`` 途中 server
                发起的 elicitation 等待用户的时间也计入）。
            transport: 自定义 httpx transport（测试注入 MockTransport）。
            elicitation_handler: 可选；注入则声明 ``elicitation`` 能力并处理 server 的
                ``elicitation/create``；不注入则不声明，server 仍发时回 ``-32601``。
            max_list_pages: ``tools/list`` 跟 ``nextCursor`` 翻页的页数上限；超限抛
                ``McpPaginationError``（防恶意 server 无限翻页，不静默截断）。
            max_stream_resumptions: POST 的 SSE 流在响应前断开时的续传次数上限；GET 推送流
                连续无事件重连的次数上限。0 = 不续传 / 不重连。
            stream_resume_delay_seconds: 续传 / 重连的基础间隔（按次数翻倍，单次 ≤30s）；
                server 以 SSE ``retry:`` 给出的间隔优先。

        Raises:
            ValueError: ``max_list_pages`` < 1、``max_stream_resumptions`` < 0 或
                ``stream_resume_delay_seconds`` < 0。
        """
        if max_stream_resumptions < 0:
            raise ValueError(f"max_stream_resumptions must be >= 0, got {max_stream_resumptions}")
        if stream_resume_delay_seconds < 0:
            raise ValueError(
                f"stream_resume_delay_seconds must be >= 0, got {stream_resume_delay_seconds}")
        self._max_resumptions = max_stream_resumptions
        self._resume_delay = stream_resume_delay_seconds
        self._max_list_pages = validate_max_pages(max_list_pages)
        self._url = url
        self._timeout = request_timeout_seconds
        self._http = httpx.AsyncClient(
            headers={**(headers or {})}, timeout=request_timeout_seconds, transport=transport)
        self._session_id: str | None = None
        # initialize 协商出的协议版本（握手完成前为 None，此时请求不带版本头）
        self._protocol_version: str | None = None
        self._next_id = 1
        self._server_info: dict[str, Any] = {}
        self._closed = False
        self._listener_task: asyncio.Task[None] | None = None
        # server 主动消息（请求 / list_changed / cancelled）的路由器；应答经 POST 送回
        self._router = ServerMessageRouter(
            send=self._send_response,
            server_info=lambda: dict(self._server_info),
            elicitation_handler=elicitation_handler,
        )
        # 放弃本端请求时的 notifications/cancelled 后台发送器（close 时收敛）
        self._cancels = CancelNotifier(self._send_response)

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        request_timeout_seconds: float = 60.0,
        listen_notifications: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        elicitation_handler: ElicitationHandler | None = None,
        max_list_pages: int = DEFAULT_MAX_LIST_PAGES,
        max_stream_resumptions: int = DEFAULT_MAX_STREAM_RESUMPTIONS,
        stream_resume_delay_seconds: float = DEFAULT_STREAM_RESUME_DELAY_SECONDS,
    ) -> McpHttpClient:
        """建连：initialize 握手 + （可选）打开服务端推送流。

        参数语义见 ``__init__``；``listen_notifications=False`` 不开 GET 推送流。

        Raises:
            McpToolError: 握手失败（HTTP 错误 / JSON-RPC 错误 / 超时）。
            McpProtocolVersionError: server 回的协议版本不受支持（会话已结束）。
        """
        client = cls(url, headers=headers, request_timeout_seconds=request_timeout_seconds,
                     transport=transport, elicitation_handler=elicitation_handler,
                     max_list_pages=max_list_pages, max_stream_resumptions=max_stream_resumptions,
                     stream_resume_delay_seconds=stream_resume_delay_seconds)
        try:
            await client._initialize()
        except BaseException:
            await client.close()
            raise
        if listen_notifications:
            client._listener_task = asyncio.get_running_loop().create_task(
                client._listen_server_stream())
        return client

    # ------------------------------------------------------------------
    # 传输
    # ------------------------------------------------------------------

    def _session_headers(self) -> dict[str, str]:
        """会话级头：协商版本 + 会话 id（均在握手后才有）。

        规范 transports「Protocol Version Header」：客户端 MUST 在 initialize 之后的**所有**
        请求带 ``MCP-Protocol-Version``，取值 SHOULD 为协商结果。initialize 本身版本尚未
        协商，不带（规范只约束后续请求；server 缺头时按 2025-03-26 兜底）。
        """
        headers: dict[str, str] = {}
        if self._protocol_version is not None:
            headers[_VERSION_HEADER] = self._protocol_version
        if self._session_id is not None:
            headers[_SESSION_HEADER] = self._session_id
        return headers

    def _headers(self) -> dict[str, str]:
        """POST 请求的完整协议头。"""
        return {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self._session_headers(),
        }

    async def _send_response(self, payload: dict[str, Any]) -> None:
        """把一条无需响应的消息（对 server 请求的应答 / 取消通知）POST 回端点。

        规范：server 接受则回 202 无 body。
        """
        await self._post(payload, expect_id=None)

    async def _post(self, payload: dict[str, Any], *, expect_id: int | None) -> Any:
        """POST 一条 JSON-RPC 消息；``expect_id`` 为 None 表示通知 / 应答（不等响应）。

        请求（``expect_id`` 非 None）因超时或被取消而放弃时，登记 ``notifications/cancelled``。
        HTTP 无法确知 server 是否已收到请求体，放弃即通知——规范要求接收方忽略未知 id。

        Raises:
            McpToolError: HTTP 非 2xx、JSON-RPC error、超时、流结束且续传失败仍无响应。
            asyncio.CancelledError: 调用方取消（已登记取消通知）。
        """
        if self._closed:
            raise RuntimeError("client closed")
        method = str(payload.get("method"))
        try:
            async with asyncio.timeout(self._timeout):
                return await self._exchange(payload, expect_id)
        except TimeoutError as e:
            if expect_id is not None:
                self._cancels.notify(expect_id, method, f"client timeout after {self._timeout:g}s")
            raise McpToolError(_TRANSPORT_ERROR, f"request timeout: {method}") from e
        except asyncio.CancelledError as e:
            if expect_id is not None:
                self._cancels.notify(expect_id, method, cancel_reason(e))
            raise
        except httpx.TransportError as e:
            raise McpToolError(_TRANSPORT_ERROR, f"transport error: {type(e).__name__}") from e

    async def _exchange(self, payload: dict[str, Any], expect_id: int | None) -> Any:
        """POST 并读出响应；SSE 流在响应之前结束 → 按 ``Last-Event-ID`` 续传（先释放 POST 连接）。"""
        cursor = SseCursor()
        async with self._http.stream(
                "POST", self._url, headers=self._headers(), json=payload) as resp:
            value = await self._read_response(resp, expect_id, cursor)
        if value is _Stream.ENDED and expect_id is not None:
            return await self._resume_for_response(expect_id, cursor)
        return value

    async def _read_response(
        self, resp: httpx.Response, expect_id: int | None, cursor: SseCursor,
    ) -> Any:
        """解析一次 POST 的响应（JSON 或 SSE）：返回匹配 id 的 result；SSE 流提前结束返回哨兵。"""
        if resp.status_code >= 400:
            body = (await resp.aread()).decode("utf-8", errors="replace")[:200]
            raise McpToolError(_TRANSPORT_ERROR, f"http {resp.status_code}: {body}")
        session_id = resp.headers.get(_SESSION_HEADER)
        if session_id:
            self._session_id = session_id
        if expect_id is None:
            return None
        if is_event_stream(resp):
            return await self._consume_stream(resp, expect_id, cursor)
        try:
            msg = json.loads(await resp.aread())
        except json.JSONDecodeError as e:
            raise McpToolError(_TRANSPORT_ERROR, "response body is not valid JSON") from e
        if not isinstance(msg, dict) or msg.get("id") != expect_id:
            raise McpToolError(_TRANSPORT_ERROR, "response id mismatch")
        return self._unwrap(msg)

    async def _consume_stream(
        self, resp: httpx.Response, expect_id: int | None, cursor: SseCursor,
    ) -> Any:
        """读 SSE 流：server 请求 / 通知交路由器，拿到 ``expect_id`` 的响应即返回。

        流正常结束或中途断开（``httpx.TransportError``）都返回 ``_Stream.ENDED``——是否续传由
        调用方按 ``cursor`` 决定。``expect_id`` 为 None（GET 推送流）时响应一律告警忽略。
        """
        try:
            async for data in iter_sse_data(resp, cursor):
                msg = _decode_message(data)
                if msg is None:
                    continue
                # 先看 method：server 请求的 id 可能与本端请求撞号，不能先按 id 匹配
                if "method" in msg:
                    self._router.route(msg)
                elif expect_id is not None and msg.get("id") == expect_id:
                    return self._unwrap(msg)
                else:
                    logger.warning("mcp http: unexpected response id=%r on SSE stream",
                                   msg.get("id"))
        except httpx.TransportError as e:
            logger.info("mcp http: SSE stream broken (%s), last event id %r",
                        type(e).__name__, cursor.resume_id)
        return _Stream.ENDED

    async def _resume_for_response(self, expect_id: int, cursor: SseCursor) -> Any:
        """POST 流在响应前断开：以 GET + ``Last-Event-ID`` 续传，直到拿到响应或次数用尽。

        Raises:
            McpToolError: server 从未给过事件 id（无从续传）、不支持续传（405）、续传被拒、
                或 ``max_stream_resumptions`` 次续传后仍无响应。
        """
        if cursor.resume_id is None:
            raise McpToolError(
                _TRANSPORT_ERROR,
                "SSE stream ended before the response; the server sent no event id to resume from")
        for attempt in range(1, self._max_resumptions + 1):
            await asyncio.sleep(resume_delay(cursor, attempt, self._resume_delay))
            value = await self._resume_once(expect_id, cursor)
            if value is not _Stream.ENDED:
                return value
        raise McpToolError(
            _TRANSPORT_ERROR,
            f"SSE stream ended before the response; gave up after "
            f"{self._max_resumptions} resumption attempt(s)")

    async def _resume_once(self, expect_id: int, cursor: SseCursor) -> Any:
        """发一次续传 GET 并读流；连接失败按一次失败的尝试计（返回哨兵）。"""
        assert cursor.resume_id is not None  # 调用方已确认有可续传的事件 id
        headers = {"Accept": EVENT_STREAM, **self._session_headers(),
                   LAST_EVENT_ID_HEADER: cursor.resume_id}
        try:
            async with self._http.stream("GET", self._url, headers=headers) as resp:
                await require_resumed_stream(resp)
                return await self._consume_stream(resp, expect_id, cursor)
        except httpx.TransportError as e:
            logger.info("mcp http: SSE resumption attempt failed to connect (%s)",
                        type(e).__name__)
            return _Stream.ENDED

    @staticmethod
    def _unwrap(msg: dict[str, Any]) -> Any:
        """JSON-RPC 响应 → result；error → McpToolError。"""
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise McpToolError(int(err.get("code", -1)), str(err.get("message", "")))
        return msg.get("result")

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """发一条带 id 的请求并等待其响应。"""
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        return await self._post(payload, expect_id=req_id)

    async def _listen_server_stream(self) -> None:
        """GET 推送流：断开 / 被 server 关闭后带 ``Last-Event-ID`` 重连。

        规范允许 server 随时关闭推送流；不重连就再也收不到 ``list_changed``。连续
        ``max_stream_resumptions`` 次重连都没收到任何事件即放弃（告警，此后工具只在显式
        ``sync`` 时刷新）；收到事件即清零计数。405 / 错误 / 非 SSE 响应 → 不再尝试。
        """
        cursor = SseCursor()
        fruitless = 0
        while True:
            seen_before = cursor.events
            if not await self._open_push_stream(cursor):
                return
            if cursor.events > seen_before:
                # 这次连接收到过事件：server 在正常推送，只是关了流，重连计数清零
                fruitless = 0
            if fruitless >= self._max_resumptions:
                logger.warning(
                    "mcp http: notification stream ended; giving up after %d reconnect "
                    "attempt(s) without events (tools refresh only on explicit sync)", fruitless)
                return
            fruitless += 1
            await asyncio.sleep(resume_delay(cursor, fruitless, self._resume_delay))

    async def _open_push_stream(self, cursor: SseCursor) -> bool:
        """打开（或续上）一次 GET 推送流并读到它结束。

        Returns:
            True = 流已结束 / 断开，可重连；False = 不应再尝试（405、错误状态、非 SSE、已关闭）。
        """
        headers = {"Accept": EVENT_STREAM, **self._session_headers()}
        if cursor.resume_id is not None:
            headers[LAST_EVENT_ID_HEADER] = cursor.resume_id
        try:
            async with self._http.stream(
                    "GET", self._url, headers=headers, timeout=None) as resp:
                if resp.status_code == 405:
                    logger.debug("mcp http: server does not offer a GET notification stream")
                    return False
                if resp.status_code >= 400 or not is_event_stream(resp):
                    logger.warning("mcp http: notification stream rejected (%s, %r)",
                                   resp.status_code, resp.headers.get("content-type"))
                    return False
                await self._consume_stream(resp, None, cursor)
        except httpx.TransportError as e:
            logger.info("mcp http: notification stream failed to connect (%s)", type(e).__name__)
        return not self._closed

    # ------------------------------------------------------------------
    # 协议方法（McpClient 协议）
    # ------------------------------------------------------------------

    async def _initialize(self) -> None:
        """initialize 握手（版本协商）+ initialized 通知。

        Raises:
            McpProtocolVersionError: server 回的版本不受支持——``connect`` 据此关闭会话后上抛。
        """
        from taifeng import __version__

        result = await self._request("initialize", initialize_params(
            capabilities=self._router.client_capabilities(), client_version=__version__))
        self._protocol_version = negotiate_protocol_version(result)
        self._server_info = result.get("serverInfo", {})
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                         expect_id=None)

    async def list_tools(self) -> list[dict[str, Any]]:
        """``tools/list``：跟完 ``nextCursor`` 分页，返回全部工具元数据。

        Raises:
            McpPaginationError: 翻页超过 ``max_list_pages`` / 游标重复 / 游标非字符串。
            McpToolError: 某页形状非法、HTTP / JSON-RPC 错误或超时。
        """
        return await list_all_tools(self._list_tools_page, max_pages=self._max_list_pages)

    async def _list_tools_page(self, cursor: str | None) -> Any:
        """取一页 ``tools/list``；首页不带 ``params``（兼容不认 cursor 字段的旧 server）。"""
        return await self._request("tools/list", {"cursor": cursor} if cursor is not None else None)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """``tools/call``。"""
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {"content": []}

    @property
    def server_info(self) -> dict[str, Any]:
        """initialize 返回的 serverInfo（副本）。"""
        return dict(self._server_info)

    @property
    def protocol_version(self) -> str | None:
        """initialize 协商出的协议版本；握手完成前为 None。"""
        return self._protocol_version

    def add_tools_changed_listener(self, listener: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """登记 ``notifications/tools/list_changed`` 的异步回调。"""
        self._router.add_tools_changed_listener(listener)

    async def close(self) -> None:
        """停止推送流、取消 server 请求的应答 / 监听任务与未发出的取消通知、DELETE 结束会话（best-effort）。

        在等用户的 elicitation handler 在这里被取消（不再 POST 任何应答）。
        """
        if self._closed:
            return
        self._closed = True
        await self._router.aclose()
        await self._cancels.aclose()
        if self._listener_task is not None:
            self._listener_task.cancel()
            # 推送流任务的取消 / 异常属预期，由 gather 收集而非外抛
            await asyncio.gather(self._listener_task, return_exceptions=True)
        if self._session_id is not None:
            try:
                await self._http.delete(self._url, headers=self._session_headers())
            except httpx.HTTPError:
                logger.debug("mcp http: session DELETE failed (ignored on close)")
        await self._http.aclose()


__all__ = ["McpHttpClient"]
