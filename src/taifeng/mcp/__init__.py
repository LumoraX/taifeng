"""MCP (Model Context Protocol) —— 双向 stdio JSON-RPC 2.0 支持。

参照：
    - claw-code crates/runtime/src/mcp_*.rs
    - codex codex-rs/mcp-client / mcp-server crates
    - https://modelcontextprotocol.io

支持（client 端）：
    - stdio transport（子进程 + JSON-RPC 2.0）/ streamable HTTP transport
    - 协议版本：声明 2025-06-18，接受 ``SUPPORTED_PROTOCOL_VERSIONS`` 内的协商结果，
      其余断开（``McpProtocolVersionError``）
    - initialize / tools/list（跟完 ``nextCursor`` 分页，页数上限防失控）/ tools/call /
      notifications/tools/list_changed
    - 把 MCP tool 注册为 Taifeng ToolSpec，通过统一 ToolRegistry 派发；
      ``bind_mcp_tools`` 随 list_changed 自动增删 / 替换（dynamic-tool-set）
    - tools/call 结果无损投影：图片进 ``ToolResult.attachments``、structuredContent 进
      ``ToolResult.data``、resource / audio 显式标注（``taifeng.mcp.content``）
    - server → client 请求：``ping`` 应答；``elicitation/create`` 交宿主注入的
      ``ElicitationHandler``（未注入回 -32601）；``notifications/cancelled`` 取消在飞应答

支持（server 端，M3 mcp-server-mode）：
    - stdio transport
    - initialize / tools/list / tools/call（暴露 ``run_skill_turn`` meta-tool）
    - resources/list / resources/read（SKILL.md 作为 ``taifeng://skill/<id>`` 资源）
    - CLI 入口 ``python -m taifeng mcp serve <skills_dir> --storage <dir>``

不支持（后续）：
    - prompts/list, prompts/get
    - sampling/create_message（server → client 反向 LLM 调用）
    - WebSocket transport
    - OAuth（鉴权头由宿主经 ``McpHttpClient(headers=...)`` 注入）
"""

from taifeng.mcp.bridge import McpClient, McpToolBinding, bind_mcp_tools
from taifeng.mcp.content import McpContentError
from taifeng.mcp.elicitation import ElicitationHandler, ElicitationRequest, ElicitationResult
from taifeng.mcp.http_client import McpHttpClient
from taifeng.mcp.pagination import McpPaginationError
from taifeng.mcp.prompter import McpPrompter
from taifeng.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpProtocolVersionError,
)
from taifeng.mcp.server import McpServerInitiatedRequestError, McpStdioServer
from taifeng.mcp.stdio_client import (
    McpStdioClient,
    McpToolError,
    register_mcp_tools,
    register_mcp_tools_async,
)

__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "ElicitationHandler",
    "ElicitationRequest",
    "ElicitationResult",
    "McpClient",
    "McpContentError",
    "McpHttpClient",
    "McpPaginationError",
    "McpPrompter",
    "McpProtocolVersionError",
    "McpToolBinding",
    "bind_mcp_tools",
    "McpServerInitiatedRequestError",
    "McpStdioClient",
    "McpStdioServer",
    "McpToolError",
    "register_mcp_tools",
    "register_mcp_tools_async",
]
