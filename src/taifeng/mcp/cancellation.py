"""客户端放弃请求时通知 server（2025-06-18 §Utilities「Cancellation」+ §Lifecycle「Timeouts」）。

规范：请求在超时内没等到响应，发送方 SHOULD 发 ``notifications/cancelled``（带 ``requestId`` 与可选
``reason``）并停止等待；``initialize`` MUST NOT 被取消；取消后迟到的响应 SHOULD 忽略。旧实现在本端
超时 / 取消时一声不吭地放弃，server 侧的处理（可能正等 elicitation 应答）要一直跑到它自己的超时。

本模块三件事：

1. ``cancelled_notification`` —— 构造通知报文；
2. ``CancelNotifier`` —— 两种传输共用的发送器。放弃请求的那一刻本端往往正处于取消中（``await``
   会被再次打断、拖慢取消），所以通知以**后台任务**发出，``aclose`` 统一收敛（R4）；
3. ``await_or_abandon`` —— 桥用：给一次远端调用加超时并接 ``CancellationToken``，放弃时以
   ``task.cancel(<原因>)`` 打断在飞请求，客户端从 ``CancelledError`` 的消息里取出原因写进通知。

「原因」经由取消消息传递，而不是扩展 ``McpClient.call_tool`` 的签名：宿主自己包 ``asyncio.timeout`` /
``task.cancel()`` 直接调客户端时同样会发通知（原因取默认文案），协议面不变。

参照：modelcontextprotocol typescript-sdk ``Protocol.request``（``AbortSignal`` / 超时 →
``_onCancel`` 发 ``notifications/cancelled``，``reason`` 取 abort 原因）；差异：taifeng 以 asyncio
取消消息承载原因，通知在后台任务里发出。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from taifeng.loop.cancellation import CancellationToken

logger = logging.getLogger(__name__)

CANCELLED_METHOD = "notifications/cancelled"

# CancelledError 不带消息时（宿主直接 task.cancel()）写进通知的原因
DEFAULT_CANCEL_REASON = "request cancelled by client"

# reason 会发给（未必可信的）server：只放本端生成的短说明，超长截断
_MAX_REASON_CHARS = 200


def cancelled_notification(request_id: str | int, reason: str) -> dict[str, Any]:
    """构造 ``notifications/cancelled``（规范字段 ``requestId`` + ``reason``）。"""
    return {
        "jsonrpc": "2.0",
        "method": CANCELLED_METHOD,
        "params": {"requestId": request_id, "reason": reason[:_MAX_REASON_CHARS]},
    }


def cancel_reason(exc: BaseException) -> str:
    """从 ``CancelledError`` 的消息取放弃原因；没有消息时给默认文案。

    ``await_or_abandon`` 用 ``task.cancel(<原因>)`` 打断在飞请求，asyncio 把该消息原样带进
    被打断协程里抛出的 ``CancelledError``。
    """
    message = exc.args[0] if exc.args else None
    return message if isinstance(message, str) and message else DEFAULT_CANCEL_REASON


class CancelNotifier:
    """一个 MCP 连接上 ``notifications/cancelled`` 的后台发送器。"""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """构造发送器。

        Args:
            send: 把一条 JSON-RPC 消息写给 server 的传输原语（stdio 写 stdin；HTTP POST）。
        """
        self._send = send
        self._tasks: set[asyncio.Task[None]] = set()

    def notify(self, request_id: str | int, method: str, reason: str) -> None:
        """登记一次放弃：以后台任务发出取消通知（``initialize`` 按规范不取消）。

        Args:
            request_id: 被放弃请求的 JSON-RPC id。
            method: 被放弃请求的方法名（日志 + ``initialize`` 判定）。
            reason: 放弃原因（进通知 ``reason``）。
        """
        if method == "initialize":
            return
        logger.info("mcp: abandoning request %r (%s): %s", request_id, method, reason)
        task = asyncio.get_running_loop().create_task(
            self._deliver(cancelled_notification(request_id, reason)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, notification: dict[str, Any]) -> None:
        """发出一条取消通知；失败只记日志（通知是 fire-and-forget，server 终会按自身超时放弃）。"""
        try:
            await self._send(notification)
        except Exception:  # noqa: BLE001 —— 投递失败不能反噬已经在收尾的请求方
            logger.warning("mcp: failed to send cancellation for request %r",
                           notification["params"]["requestId"], exc_info=True)

    async def aclose(self) -> None:
        """取消并等待尚未发出的通知（连接即将关闭，server 会随连接结束放弃一切）。"""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        # 取消与任务内异常都属预期收尾，由 gather 收集而非外抛
        await asyncio.gather(*tasks, return_exceptions=True)


def _token_reason(cancel: CancellationToken) -> str:
    """token 取消 → 通知原因（含取消原因与说明）。"""
    detail = f": {cancel.detail}" if cancel.detail else ""
    return f"client cancelled the request ({cancel.reason}{detail})"


async def await_or_abandon[T](
    call: Coroutine[Any, Any, T],
    *,
    timeout_seconds: float,
    cancel: CancellationToken,
) -> T:
    """等一次远端调用完成；超时或 token 取消时以带原因的取消打断它。

    调用跑在独立任务里，与超时、``cancel.wait_cancelled()`` 竞速。放弃时先
    ``task.cancel(<原因>)`` 并等它收尾（客户端在收尾时登记 ``notifications/cancelled``），再把
    结局交给调用方。本函数自身被外部取消时同样打断在飞调用（原因为调用方取消）。

    Args:
        call: 远端调用协程（如 ``client.call_tool(name, args)``）。
        timeout_seconds: 本次调用的时限。
        cancel: 工具上下文的取消 token（turn 取消 / 截止时间 / 关停）。

    Returns:
        调用结果。

    Raises:
        TimeoutError: 超时（消息为放弃原因）。
        asyncio.CancelledError: token 已取消（由工具运行时按 token 取消收敛为 cancelled 结果）。
        Exception: 调用自身抛出的异常原样上抛。
    """
    task = asyncio.ensure_future(call)
    waiter = asyncio.ensure_future(cancel.wait_cancelled())
    try:
        done, _ = await asyncio.wait(
            {task, waiter}, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        token_fired = waiter in done
        reason = (_token_reason(cancel) if token_fired
                  else f"client timeout after {timeout_seconds:g}s")
        task.cancel(reason)
        # 等在飞请求按取消收尾；其结局（CancelledError）已由放弃语义取代，不再外抛
        await asyncio.gather(task, return_exceptions=True)
        if token_fired:
            cancel.raise_if_cancelled()
        raise TimeoutError(reason)
    finally:
        waiter.cancel()
        if not task.done():
            # 走到这里只有一种可能：本函数被外部取消（如工具运行时的外层超时 / 任务关停）
            task.cancel("client cancelled the request (caller cancelled)")


__all__ = [
    "CANCELLED_METHOD",
    "DEFAULT_CANCEL_REASON",
    "CancelNotifier",
    "await_or_abandon",
    "cancel_reason",
    "cancelled_notification",
]
