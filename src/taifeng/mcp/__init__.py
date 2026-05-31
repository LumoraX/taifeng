"""MCP (Model Context Protocol) —— 双向 stdio JSON-RPC 2.0 支持。

参照：
    - claw-code crates/runtime/src/mcp_*.rs
    - codex codex-rs/mcp-client / mcp-server crates
    - https://modelcontextprotocol.io

支持（client 端）：
    - stdio transport（子进程 + JSON-RPC 2.0）
    - initialize / tools/list / tools/call
    - 把 MCP tool 注册为 Taifeng ToolSpec，通过统一 ToolRegistry 派发

支持（server 端，M3 mcp-server-mode）：
    - stdio transport
    - initialize / tools/list / tools/call（暴露 ``run_skill_turn`` meta-tool）
    - resources/list / resources/read（SKILL.md 作为 ``taifeng://skill/<id>`` 资源）
    - CLI 入口 ``python -m taifeng mcp serve <skills_dir> --storage <dir>``

不支持（后续）：
    - prompts/list, prompts/get
    - sampling/create_message（server → client 反向 LLM 调用）
    - WebSocket / HTTP transport
    - OAuth / authentication
"""

from taifeng.mcp.prompter import McpPrompter
from taifeng.mcp.server import McpServerInitiatedRequestError, McpStdioServer
from taifeng.mcp.stdio_client import (
    McpStdioClient,
    McpToolError,
    register_mcp_tools,
    register_mcp_tools_async,
)

__all__ = [
    "McpPrompter",
    "McpServerInitiatedRequestError",
    "McpStdioClient",
    "McpStdioServer",
    "McpToolError",
    "register_mcp_tools",
    "register_mcp_tools_async",
]
