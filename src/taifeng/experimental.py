"""taifeng.experimental —— 实验层公共 API（契约仍可能不兼容地变化）。

公共 API 分三层（ADR 0066，``docs/architecture/public-api.md``）：

- **稳定层**：``taifeng`` 顶层 ``__all__``。移除或不兼容修改须走弃用流程（至少跨两个发布版本且
  不少于 30 天的 ``DeprecationWarning``），``tests/test_public_api.py`` 的快照守护每次变更都是有意为之。
- **实验层**：本模块 ``__all__``。能力契约标 🧪 的入口在此导出；可在任意发布中不兼容地变化，
  变化记入 ADR。契约转 ✅ 后晋升到顶层（同时保留本模块的同名导出至少一个发布版本）。
- **内部**：其余子模块中未经上述两处导出的符号，不承诺任何兼容性。

本模块只做再导出，不含实现；导入它不会触发额外副作用。
"""

from __future__ import annotations

from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.providers.replay import (
    JournalReplayClient,
    RecordedCall,
    ReplayDivergenceError,
    ReplayUnsupportedError,
    recorded_calls,
)
from taifeng.loop.audit_config import AuditCapabilityError, AuditConfig
from taifeng.mcp.bridge import McpToolBinding, bind_mcp_tools
from taifeng.mcp.http_client import McpHttpClient
from taifeng.tool.spec import ReconcileVerdict

__all__ = [
    # strict audit Session + durable Journal（session-journal，🧪）
    "AuditCapabilityError",
    "AuditConfig",
    "JsonlSessionJournalCore",
    # Journal 确定性回放（journal-replay，🧪）
    "JournalReplayClient",
    "RecordedCall",
    "ReplayDivergenceError",
    "ReplayUnsupportedError",
    "recorded_calls",
    # 工具崩溃对账的回查结果（tool-crash-reconciliation，🧪）
    "ReconcileVerdict",
    # 工具集动态增删 + MCP streamable HTTP（dynamic-tool-set，🧪）
    "McpHttpClient",
    "McpToolBinding",
    "bind_mcp_tools",
]
