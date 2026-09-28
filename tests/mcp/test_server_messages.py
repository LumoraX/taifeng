"""ServerMessageRouter 单测：与传输无关的路由规则（请求 id 校验、取消、关闭、监听派发）。

传输层集成（stdio 子进程 / HTTP SSE）见 ``test_elicitation.py``。
"""

from __future__ import annotations

import asyncio
from typing import Any

from taifeng.mcp.elicitation import ElicitationRequest, ElicitationResult
from taifeng.mcp.server_messages import ServerMessageRouter
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition


class _Sink:
    """收集路由器写出的应答。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _router(sink: _Sink, handler: Any = None) -> ServerMessageRouter:
    return ServerMessageRouter(
        send=sink, server_info=lambda: {"name": "s"}, elicitation_handler=handler)


async def test_ping_answered_with_empty_result() -> None:
    """ping → 空 result（规范 MUST 及时应答）。"""
    sink = _Sink()
    router = _router(sink)
    router.route({"jsonrpc": "2.0", "id": 5, "method": "ping"})
    await wait_for_condition(lambda: bool(sink.sent))
    assert sink.sent == [{"jsonrpc": "2.0", "id": 5, "result": {}}]
    await router.aclose()


async def test_bad_and_duplicate_ids_are_invalid_requests() -> None:
    """id 非字符串 / 整数（含 bool）→ -32600 且 id=null；与在飞请求重号 → -32600。"""
    sink = _Sink()
    gate = asyncio.Event()

    async def _slow(request: ElicitationRequest) -> ElicitationResult:
        await gate.wait()
        return ElicitationResult("cancel")

    router = _router(sink, _slow)
    params = {"message": "m", "requestedSchema": {"type": "object"}}
    router.route({"jsonrpc": "2.0", "id": True, "method": "ping"})
    router.route({"jsonrpc": "2.0", "id": "a", "method": "elicitation/create", "params": params})
    router.route({"jsonrpc": "2.0", "id": "a", "method": "elicitation/create", "params": params})
    await wait_for_condition(lambda: len(sink.sent) == 2)
    assert sink.sent[0]["id"] is None
    assert sink.sent[0]["error"]["code"] == -32600
    assert sink.sent[1] == {"jsonrpc": "2.0", "id": "a", "error": {
        "code": -32600, "message": "Invalid Request: duplicate in-flight id"}}
    gate.set()
    await wait_for_condition(lambda: len(sink.sent) == 3)
    assert sink.sent[2]["result"] == {"action": "cancel"}
    await router.aclose()


async def test_unknown_or_malformed_cancellation_is_ignored() -> None:
    """未知 id / 形状不对的 cancelled 通知按规范忽略，不影响在飞请求。"""
    sink = _Sink()
    gate = asyncio.Event()

    async def _slow(request: ElicitationRequest) -> ElicitationResult:
        await gate.wait()
        return ElicitationResult("decline")

    router = _router(sink, _slow)
    router.route({"jsonrpc": "2.0", "id": 1, "method": "elicitation/create",
                  "params": {"message": "m", "requestedSchema": {"type": "object"}}})
    for params in ({"requestId": 99}, {"requestId": [1]}, "junk", None):
        router.route({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params})
    gate.set()
    await wait_for_condition(lambda: bool(sink.sent))
    assert sink.sent[0]["result"] == {"action": "decline"}
    await router.aclose()


async def test_list_changed_listeners_run_as_tasks_and_aclose_cancels_all() -> None:
    """list_changed 以 task 调度监听者；aclose 取消在等的 handler 与监听，之后不再路由。"""
    sink = _Sink()
    listener_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def _listener() -> None:
        listener_started.set()
        await asyncio.Event().wait()

    async def _waiting(request: ElicitationRequest) -> ElicitationResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        raise AssertionError("unreachable")

    router = _router(sink, _waiting)
    router.add_tools_changed_listener(_listener)
    router.route({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    router.route({"jsonrpc": "2.0", "id": 1, "method": "elicitation/create",
                  "params": {"message": "m", "requestedSchema": {"type": "object"}}})
    await asyncio.wait_for(listener_started.wait(), GUARD_TIMEOUT_SECONDS)
    await asyncio.wait_for(router.aclose(), GUARD_TIMEOUT_SECONDS)
    assert handler_cancelled.is_set()
    router.route({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    await asyncio.sleep(0)
    assert sink.sent == []


async def test_delivery_failure_is_logged_not_raised() -> None:
    """写回失败（传输已断）只记日志，路由器继续处理后续消息。"""
    calls: list[dict[str, Any]] = []

    async def _broken(payload: dict[str, Any]) -> None:
        calls.append(payload)
        raise RuntimeError("pipe closed")

    router = ServerMessageRouter(send=_broken, server_info=dict)
    router.route({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    router.route({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    await wait_for_condition(lambda: len(calls) == 2)
    await router.aclose()


def test_capabilities_follow_handler_injection() -> None:
    """注入 handler 才声明 elicitation。"""

    async def _h(request: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult("cancel")

    assert _router(_Sink(), _h).client_capabilities() == {"elicitation": {}}
    assert _router(_Sink()).client_capabilities() == {}
