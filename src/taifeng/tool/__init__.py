"""Tool 系统 —— ToolSpec 协议、并行 / 独占调度、内置工具。

设计文档：docs/architecture/agent-loop.md
"""

from taifeng.tool.registry import DuplicateToolError, ToolRegistry, UnknownToolError
from taifeng.tool.runtime import ToolCallRuntime
from taifeng.tool.spec import ToolContext, ToolFunc, ToolHandler, ToolResult, ToolSpec

__all__ = [
    "DuplicateToolError",
    "ToolCallRuntime",
    "ToolContext",
    "ToolFunc",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "UnknownToolError",
]
