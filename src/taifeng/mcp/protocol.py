"""MCP 协议公共件：版本协商、JSON-RPC 错误码、initialize 参数（stdio / HTTP 客户端与 server 共用）。

版本策略（MCP 2025-06-18 §Lifecycle「Version Negotiation」）：

- 客户端 initialize 时 MUST 发一个自己支持的版本，SHOULD 是最新版 → 恒发
  ``LATEST_PROTOCOL_VERSION``；
- server 支持该版本 MUST 原样回；否则 MUST 回一个它支持的版本（SHOULD 为其最新）；
- 客户端不支持 server 回的版本 SHOULD 断开 → ``negotiate_protocol_version`` 抛
  ``McpProtocolVersionError``，由各传输的建连入口关闭连接后上抛，**不静默继续**。

``SUPPORTED_PROTOCOL_VERSIONS`` 为什么含旧版：本客户端用到的协议面（initialize /
tools/list / tools/call / list_changed / ping / cancelled）在三版之间线上兼容；
elicitation、structuredContent、resource_link 是 2025-06-18 的**增量**，旧版 server
只是不会发——协商到旧版不会让任何已实现能力失真。清单外的版本一律断开。

参照：modelcontextprotocol typescript-sdk ``SUPPORTED_PROTOCOL_VERSIONS``（差异：不收
2024-10-07 预发布版）。
"""

from __future__ import annotations

from typing import Any

from taifeng.mcp.bridge import McpToolError

# 客户端 initialize 时声明的版本（elicitation 自此版进入规范）
LATEST_PROTOCOL_VERSION = "2025-06-18"

# 可接受的协商结果（新 → 旧）；用 tuple 而非 frozenset：成员判定不要求可哈希，
# 对端塞进来的非字符串值不会在 ``in`` 上炸 TypeError
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 标准错误码
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class McpProtocolVersionError(McpToolError):
    """server 回的 protocolVersion 不在本客户端支持清单内（或缺失）——建连失败。

    继承 ``McpToolError``：既有 ``except McpToolError`` 的建连调用方无需改动即可接住。
    错误码取 -32602，与规范 lifecycle 示例「Unsupported protocol version」一致。

    Attributes:
        requested: 本客户端请求的版本。
        received: server 回的原值（缺失为 None；非字符串原样保留便于排查）。
        supported: 本客户端可接受的版本清单。
    """

    def __init__(self, received: Any, *, reason: str) -> None:
        """构造错误。

        Args:
            received: server initialize 响应里的 ``protocolVersion`` 原值。
            reason: 人类可读的失败原因（进异常消息）。
        """
        super().__init__(
            JSONRPC_INVALID_PARAMS,
            f"unsupported MCP protocol version {received!r}: {reason} "
            f"(requested {LATEST_PROTOCOL_VERSION}, supported {list(SUPPORTED_PROTOCOL_VERSIONS)})",
        )
        self.requested = LATEST_PROTOCOL_VERSION
        self.received = received
        self.supported = SUPPORTED_PROTOCOL_VERSIONS


def negotiate_protocol_version(initialize_result: Any) -> str:
    """校验 server 的 initialize 响应并返回协商后的版本。

    Args:
        initialize_result: ``initialize`` 请求的 JSON-RPC result。

    Returns:
        server 回的、且在 ``SUPPORTED_PROTOCOL_VERSIONS`` 内的版本。

    Raises:
        McpProtocolVersionError: result 非对象、缺 ``protocolVersion``（规范必填）或版本
            不受支持。调用方 SHOULD 断开连接（规范 lifecycle）。
    """
    if not isinstance(initialize_result, dict):
        raise McpProtocolVersionError(None, reason="initialize result is not an object")
    version = initialize_result.get("protocolVersion")
    if not isinstance(version, str):
        raise McpProtocolVersionError(version, reason="initialize result lacks protocolVersion")
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise McpProtocolVersionError(version, reason="server chose a version this client lacks")
    return version


def select_server_protocol_version(requested: Any) -> str:
    """server 侧版本协商：客户端请求的版本受支持则原样回，否则回最新版。

    规范：server 支持所请求版本 MUST 原样回；否则 MUST 回一个自己支持的版本，SHOULD
    为最新。请求缺失 / 非字符串同样按「不支持」处理——回最新版由客户端决定是否断开，
    这是规范给出的唯一出路（initialize 不以错误拒绝版本）。
    """
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def initialize_params(*, capabilities: dict[str, Any], client_version: str) -> dict[str, Any]:
    """构造客户端 ``initialize`` 请求参数。

    Args:
        capabilities: 客户端能力声明（注入 elicitation handler 时含 ``elicitation``；
            不声明 server 侧能力——旧实现误塞的 ``tools`` 是 server 能力）。
        client_version: ``clientInfo.version``（运行时 ``taifeng.__version__``）。
    """
    return {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": capabilities,
        "clientInfo": {"name": "taifeng", "version": client_version},
    }


def jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    """构造 JSON-RPC 2.0 error response。"""
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """构造 JSON-RPC 2.0 success response。"""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


__all__ = [
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "McpProtocolVersionError",
    "initialize_params",
    "jsonrpc_error",
    "jsonrpc_result",
    "negotiate_protocol_version",
    "select_server_protocol_version",
]
