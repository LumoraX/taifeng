"""MCP (Model Context Protocol) —— 双向 stdio JSON-RPC 2.0 支持。

参照：
    - claw-code crates/runtime/src/mcp_*.rs
    - codex codex-rs/mcp-client / mcp-server crates
    - https://modelcontextprotocol.io

支持（client 端）：
    - stdio transport（子进程 + JSON-RPC 2.0）/ streamable HTTP transport（MCP 2025-03-26）
    - initialize / tools/list / tools/call / notifications/tools/list_changed
    - 把 MCP tool 注册为 Taifeng ToolSpec，通过统一 ToolRegistry 派发；
      ``bind_mcp_tools`` 随 list_changed 自动增删 / 替换（dynamic-tool-set）

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
from taifeng.mcp.http_client import McpHttpClient
from taifeng.mcp.prompter import McpPrompter
from taifeng.mcp.server import McpServerInitiatedRequestError, McpStdioServer
from taifeng.mcp.stdio_client import (
    McpStdioClient,
    McpToolError,
    register_mcp_tools,
    register_mcp_tools_async,
)

__all__ = [
    "McpClient",
    "McpHttpClient",
    "McpPrompter",
    "McpToolBinding",
    "bind_mcp_tools",
    "McpServerInitiatedRequestError",
    "McpStdioClient",
    "McpStdioServer",
    "McpToolError",
    "register_mcp_tools",
    "register_mcp_tools_async",
]
