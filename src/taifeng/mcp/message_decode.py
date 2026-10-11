"""两种 MCP 传输共用的 UTF-8 帧解析与响应形状校验。"""

from __future__ import annotations

import json
import logging
from typing import Any

from taifeng.mcp.bridge import McpToolError

logger = logging.getLogger(__name__)


def decode_message(data: str | bytes) -> dict[str, Any] | None:
    """坏帧只记录错误类型与长度，不把远端内容送进日志。"""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        message = json.loads(text)
    except (UnicodeError, ValueError) as error:
        logger.warning("mcp: invalid frame type=%s length=%d", type(error).__name__, len(data))
        return None
    if not isinstance(message, dict):
        logger.warning("mcp: non-object frame type=%s length=%d", type(message).__name__, len(data))
        return None
    return message


def unwrap_response(message: dict[str, Any]) -> Any:
    """解开 JSON-RPC 响应；错误形状不合法时统一拒绝。"""
    if "error" in message:
        error = message["error"]
        if not isinstance(error, dict):
            raise McpToolError(-32000, "invalid JSON-RPC error object")
        code, text = error.get("code"), error.get("message")
        if not isinstance(code, int) or isinstance(code, bool) or not isinstance(text, str):
            raise McpToolError(-32000, "invalid JSON-RPC error fields")
        raise McpToolError(code, text)
    if "result" not in message:
        raise McpToolError(-32000, "invalid JSON-RPC response: missing result")
    return message["result"]


def require_object(value: Any, label: str) -> dict[str, Any]:
    """要求协议中的对象字段确为对象，不生成假空成功。"""
    if not isinstance(value, dict):
        raise McpToolError(-32000, f"invalid {label}: expected object")
    return value


def value_metadata(value: Any) -> tuple[str, int]:
    """远端值的日志视图只包含类型和长度。"""
    length = len(value) if isinstance(value, (str, bytes, list, dict, tuple)) else 0
    return type(value).__name__, length
