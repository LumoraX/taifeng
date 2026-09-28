"""MCP 客户端侧 elicitation（2025-06-18 §Client「Elicitation」）：注入口协议与应答构造。

taifeng 作为 **MCP 客户端**时，server 可在处理请求途中发 ``elicitation/create`` 向用户
要结构化输入。内核不做 UI：宿主注入 ``ElicitationHandler``，内核负责

- 注入了 → initialize 声明 ``capabilities.elicitation``，把请求交给 handler，按 JSON-RPC 回复；
- 未注入 → 不声明该能力；server 仍发则回 ``-32601``（见 ``ServerMessageRouter``）；
- handler 抛异常 / 返回值不合法 / content 违反 requestedSchema → 回 JSON-RPC error，
  连接照常存活（异常细节只进本地日志，不回传 server）。

与 ``taifeng.mcp.prompter.McpPrompter`` 方向相反：那是 taifeng 作为 **server** 向客户端
发 elicitation。

参照：codex ``codex-rs/rmcp-client/src/elicitation_client_service.rs``（server 请求交给
注入的 ``SendElicitation``，按 accept / decline / cancel 回 ``ElicitResult``）；差异：
taifeng 以 Protocol 注入、内核只做形状与 schema 子集校验，不含 openai 扩展表单。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from taifeng.mcp.protocol import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    jsonrpc_error,
    jsonrpc_result,
)
from taifeng.tool.arg_validation import schema_violations

logger = logging.getLogger(__name__)

type ElicitationAction = Literal["accept", "decline", "cancel"]
"""规范的三动作：accept = 提交数据；decline = 明确拒绝；cancel = 未作选择即关闭。"""

type ElicitationValue = str | int | float | bool
"""content 的取值类型（规范：requestedSchema 限扁平对象 + 原始类型属性）。"""

_ACTIONS = ("accept", "decline", "cancel")


@dataclass(frozen=True)
class ElicitationRequest:
    """交给宿主 handler 的一次 elicitation 请求。

    Attributes:
        message: server 给用户看的说明文字。
        requested_schema: server 期望的回答结构（扁平 object JSON Schema 子集）。
        server_info: 发起方 server 的 ``serverInfo``——规范 SHOULD 向用户标明是哪个
            server 在索取信息。
    """

    message: str
    requested_schema: dict[str, Any]
    server_info: dict[str, Any]


@dataclass(frozen=True)
class ElicitationResult:
    """宿主 handler 的回答。构造期即校验，非法组合直接 ``ValueError``。

    Attributes:
        action: ``accept`` / ``decline`` / ``cancel``。
        content: 仅 ``accept`` 可带；键为字符串、值为原始类型。
    """

    action: ElicitationAction
    content: dict[str, ElicitationValue] | None = None

    def __post_init__(self) -> None:
        """拒绝规范外动作、非 accept 带 content、非原始类型取值。"""
        if self.action not in _ACTIONS:
            raise ValueError(f"elicitation action must be one of {_ACTIONS}, got {self.action!r}")
        if self.content is None:
            return
        if self.action != "accept":
            raise ValueError("elicitation content is only allowed with action='accept'")
        for key, value in self.content.items():
            if not isinstance(key, str) or not isinstance(value, str | int | float | bool):
                raise ValueError(f"elicitation content[{key!r}] must be a primitive value")

    def to_wire(self) -> dict[str, Any]:
        """转成 ``elicitation/create`` 的 JSON-RPC result 对象。"""
        wire: dict[str, Any] = {"action": self.action}
        if self.content is not None:
            wire["content"] = dict(self.content)
        return wire


class ElicitationHandler(Protocol):
    """宿主注入的 elicitation 处理器（异步；可直接传 ``async def`` 函数）。

    调用会被取消（``asyncio.CancelledError``）的两种情形，实现方不应吞掉取消：
    server 发 ``notifications/cancelled`` 撤回该请求；客户端 ``close()``。超时由请求方
    （server）负责——规范 lifecycle「Timeouts」把超时与取消通知归于发送方；宿主若要
    自己的时限，可在 handler 内自行限时并返回 ``cancel``。
    """

    async def __call__(self, request: ElicitationRequest) -> ElicitationResult:
        """展示请求、收集用户回答并返回。"""
        ...


def parse_elicitation_params(params: Any, server_info: dict[str, Any]) -> ElicitationRequest:
    """校验 ``elicitation/create`` 的 params 并构造 ``ElicitationRequest``。

    Raises:
        ValueError: params 非对象、``message`` 非字符串、``requestedSchema`` 不是
            ``type: object`` 的对象。
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    message = params.get("message")
    if not isinstance(message, str):
        raise ValueError("'message' must be a string")
    schema = params.get("requestedSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("'requestedSchema' must be an object schema with type 'object'")
    return ElicitationRequest(message=message, requested_schema=schema, server_info=server_info)


async def answer_elicitation(
    req_id: str | int,
    params: Any,
    handler: ElicitationHandler,
    server_info: dict[str, Any],
) -> dict[str, Any]:
    """调用宿主 handler 并构造 JSON-RPC 应答（成功 result 或 error）。

    ``asyncio.CancelledError`` 原样上抛（取消的请求按规范不回响应）；其余失败一律转
    JSON-RPC error，不让一次坏回答打断连接。

    Returns:
        可直接发回 server 的 JSON-RPC response。
    """
    try:
        request = parse_elicitation_params(params, server_info)
    except ValueError as exc:
        return jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, f"Invalid params: {exc}")
    try:
        result = await handler(request)
    except Exception as exc:  # noqa: BLE001 —— 宿主 handler 的任何失败都不能拖垮连接
        # 细节只进本地日志：异常消息可能含宿主内部信息，不回传给（可能不可信的）server
        logger.exception("mcp elicitation handler failed for request %r", req_id)
        return jsonrpc_error(
            req_id, JSONRPC_INTERNAL_ERROR, f"elicitation handler failed: {type(exc).__name__}")
    if not isinstance(result, ElicitationResult):
        return jsonrpc_error(
            req_id, JSONRPC_INTERNAL_ERROR,
            f"elicitation handler returned {type(result).__name__}, expected ElicitationResult")
    if result.content is not None:
        # 规范安全条款：双方 SHOULD 按 requestedSchema 校验 content；不合规不外发
        violations = schema_violations(request.requested_schema, dict(result.content))
        if violations:
            return jsonrpc_error(
                req_id, JSONRPC_INTERNAL_ERROR,
                "elicitation content violates requestedSchema: " + "; ".join(violations))
    return jsonrpc_result(req_id, result.to_wire())


__all__ = [
    "ElicitationAction",
    "ElicitationHandler",
    "ElicitationRequest",
    "ElicitationResult",
    "ElicitationValue",
    "answer_elicitation",
    "parse_elicitation_params",
]
