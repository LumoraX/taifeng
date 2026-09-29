"""taifeng 作为 MCP server 时的客户端能力门控（2025-06-18 §Lifecycle「Operation」/ §Client「Elicitation」）。

规范：运行期双方 **MUST** 只使用协商成功的能力；支持 elicitation 的客户端 **MUST** 在 initialize
声明 ``capabilities.elicitation``。反过来，客户端没声明时 server 不得发 ``elicitation/create``——旧实现
照发不误，老客户端要么回 ``-32601``，要么干脆不理，审批只能等到超时才判 deny。

本模块只做判定：``McpStdioServer`` 在 initialize 时记下客户端能力（``parse_client_capabilities``），
发 server → client 请求前查 ``missing_client_capability``；缺能力就不发，抛
``McpClientCapabilityError``，由调用方（``McpPrompter``）按 fail-closed 判 deny。

判定口径：能力项必须是 JSON 对象（规范形状 ``{}``）；缺失、``null``、``true`` 等一律视为未声明——
审批通道宁可拒绝也不向不确定能否应答的客户端发请求。initialize 之前（尚未协商）同样视为未声明。
"""

from __future__ import annotations

from typing import Any

# server → client 请求方法 → 客户端须声明的能力（ping 不需要任何能力，不在表内）
REQUIRED_CLIENT_CAPABILITY: dict[str, str] = {
    "elicitation/create": "elicitation",
    "sampling/createMessage": "sampling",
    "roots/list": "roots",
}


class McpClientCapabilityError(Exception):
    """客户端未声明某 server → client 请求所需的能力，server 没有发出该请求。

    Attributes:
        method: 被拦下的请求方法。
        capability: 所需的客户端能力名。
        initialized: 客户端是否已完成 initialize（False = 尚未协商任何能力）。
    """

    def __init__(self, *, method: str, capability: str, initialized: bool) -> None:
        """构造错误。

        Args:
            method: 被拦下的请求方法。
            capability: 所需的客户端能力名。
            initialized: 客户端是否已完成 initialize。
        """
        state = ("did not declare the" if initialized
                 else "has not initialized, so has not declared the")
        super().__init__(f"client {state} {capability!r} capability required by {method}")
        self.method = method
        self.capability = capability
        self.initialized = initialized


def parse_client_capabilities(initialize_params: dict[str, Any]) -> dict[str, Any]:
    """取 initialize 请求里的客户端能力（非对象按「未声明任何能力」处理）。"""
    capabilities = initialize_params.get("capabilities")
    return dict(capabilities) if isinstance(capabilities, dict) else {}


def missing_client_capability(
    method: str, client_capabilities: dict[str, Any] | None,
) -> str | None:
    """返回 ``method`` 所需、但客户端未声明的能力名；无需能力或已声明返回 None。

    Args:
        method: 将要发出的 server → client 请求方法。
        client_capabilities: initialize 时记下的客户端能力；None = 尚未 initialize。
    """
    capability = REQUIRED_CLIENT_CAPABILITY.get(method)
    if capability is None:
        return None
    declared = (client_capabilities or {}).get(capability)
    return None if isinstance(declared, dict) else capability


__all__ = [
    "REQUIRED_CLIENT_CAPABILITY",
    "McpClientCapabilityError",
    "missing_client_capability",
    "parse_client_capabilities",
]
