"""Taifeng (泰逢) —— 通用 LLM Agent 微内核。

让 LLM 自主调度文档化 skill 的 Python agent 微内核 —— 动 agent 的"天地气"。

设计文档：docs/architecture/overview.md
"""

__version__ = "0.0.1"

# 公共 API ——————————————————————————————————————

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # OTel 是 optional extra；类型检查时直接暴露符号，运行时按需 lazy import
    from taifeng.telemetry.otel_sink import OtelSinkConfig, OtelTelemetrySink

from taifeng.context import (
    CompositeMemoryStore,
    CompressionContext,
    CompressionOrchestrator,
    CompressionResult,
    CompressionStrategy,
    ContextBudget,
    HandoffCompactionStrategy,
    InitialContextInjection,
    MemoryStore,
    NullMemoryStore,
    PinnedStateRegistry,
    PinnedStateSource,
    PromptCacheStats,
    SlidingWindowStrategy,
    SurgicalTrimStrategy,
)
from taifeng.conversation import (
    DirectoryError,
    IndexHook,
    JsonlMessageStore,
    JsonlMessageWriter,
    MessageStore,
    MessageWriter,
    NoopIndexHook,
    NullThreadDirectory,
    RebuildReport,
    ResponseItem,
    SqliteThreadDirectory,
    ThreadDirectory,
    ThreadFilter,
    ThreadMetadata,
    ThreadNotFoundError,
    ThreadPage,
    assistant_message,
    function_call,
    function_call_output,
    rebuild_index,
    suspension_item,
    user_message,
)
from taifeng.hooks import (
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    PostSkillDispatchHook,
    PostToolUseHook,
    PreSkillDispatchHook,
    PreToolUseHook,
)
from taifeng.instructions import (
    InstructionContext,
    InstructionFetchError,
    InstructionLayer,
    InstructionResolver,
    InstructionScope,
    InstructionSource,
    ResolvedInstruction,
)
from taifeng.llm import (
    ApiMessage,
    ApiRequest,
    LLMError,
    ModelClient,
    ResponseEvent,
    ResponseFormatSpec,
    TokenUsage,
)
from taifeng.loop import (
    CancellationToken,
    EventMsg,
    LlmRequestRecorded,
    Op,
    Submission,
    UpdateInstructions,
    UserMessage,
)
from taifeng.loop.denial_breaker import DenialBreaker, DenialBreakerConfig
from taifeng.loop.doom_loop import DoomLoopConfig, DoomLoopDetector
from taifeng.loop.engine import AgentEngine, DeliveredEvent
from taifeng.loop.failure_policy import (
    ConservativeFailurePolicy,
    FailureContext,
    FailureDisposition,
    FailureDispositionPolicy,
    SuspendByDefaultPolicy,
)
from taifeng.loop.iteration_budget import IterationBudget
from taifeng.loop.pool import EnginePool
from taifeng.loop.submission import Resume, SendToPeer
from taifeng.loop.turn import TurnOutcome, TurnRunner
from taifeng.mcp import McpStdioClient, McpStdioServer, McpToolError
from taifeng.permission import (
    PermissionDecision,
    PermissionPolicy,
    PermissionPrompter,
    PermissionRequest,
    PermissionRule,
)
from taifeng.skill import (
    CallStack,
    DispatchPolicy,
    FilesystemSkillRegistry,
    SkillDefinition,
    SkillRegistry,
    SkillSnapshot,
)
from taifeng.skill.scripts import (
    PythonScriptExecutor,
    ScriptDescriptor,
    ScriptExecutionError,
    ScriptExecutor,
    ScriptInvocation,
    ScriptLanguage,
    ScriptResult,
    ShellScriptExecutor,
)
from taifeng.suspend import (
    PendingRequest,
    ResolveError,
    ResolvePlan,
    SuspendReason,
    SuspendSignal,
    SuspensionRecord,
    SuspensionResolver,
)
from taifeng.tool import (
    ToolCallRuntime,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from taifeng.tool.builtins import (
    BackgroundTaskRegistry,
    BackgroundTaskTimeoutError,
    TodoStore,
    make_apply_patch_tool,
    make_run_in_background_tool,
    make_send_message_tool,
    make_todo_write_tool,
    make_wait_for_task_tool,
    make_wait_peer_tool,
)

__all__ = [
    "AgentEngine",
    "ApiMessage",
    "ApiRequest",
    "CallStack",
    "CancellationToken",
    "CompressionContext",
    "CompressionOrchestrator",
    "CompressionResult",
    "CompressionStrategy",
    "ConservativeFailurePolicy",
    "DeliveredEvent",
    "DenialBreaker",
    "DenialBreakerConfig",
    "DoomLoopConfig",
    "DoomLoopDetector",
    "FailureContext",
    "FailureDisposition",
    "FailureDispositionPolicy",
    "IterationBudget",
    "SuspendByDefaultPolicy",
    "ContextBudget",
    "DirectoryError",
    "DispatchPolicy",
    "EnginePool",
    "EventMsg",
    "LlmRequestRecorded",
    "FilesystemSkillRegistry",
    "HandoffCompactionStrategy",
    "IndexHook",
    "CompositeMemoryStore",
    "MemoryStore",
    "NullMemoryStore",
    "InitialContextInjection",
    "JsonlMessageStore",
    "JsonlMessageWriter",
    "BackgroundTaskRegistry",
    "BackgroundTaskTimeoutError",
    "LLMError",
    "McpStdioClient",
    "McpStdioServer",
    "McpToolError",
    "MessageStore",
    "OtelSinkConfig",
    "OtelTelemetrySink",
    "make_apply_patch_tool",
    "make_send_message_tool",
    "make_todo_write_tool",
    "make_wait_peer_tool",
    "make_run_in_background_tool",
    "make_wait_for_task_tool",
    "MessageWriter",
    "ModelClient",
    "NoopIndexHook",
    "NullThreadDirectory",
    "Op",
    "PendingRequest",
    "PromptCacheStats",
    "RebuildReport",
    "ResolveError",
    "ResolvePlan",
    "Resume",
    "SendToPeer",
    "TodoStore",
    "ResponseEvent",
    "ResponseFormatSpec",
    "ResponseItem",
    "SkillDefinition",
    "SkillRegistry",
    "SkillSnapshot",
    "SlidingWindowStrategy",
    "PinnedStateRegistry",
    "PinnedStateSource",
    "SurgicalTrimStrategy",
    "SqliteThreadDirectory",
    "Submission",
    "SuspendReason",
    "SuspendSignal",
    "SuspensionRecord",
    "SuspensionResolver",
    "ThreadDirectory",
    "ThreadFilter",
    "ThreadMetadata",
    "ThreadNotFoundError",
    "ThreadPage",
    "TokenUsage",
    "ToolCallRuntime",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "UserMessage",
    "HookContext",
    "HookDecision",
    "HookRegistry",
    "HookRunner",
    "InstructionContext",
    "InstructionFetchError",
    "InstructionLayer",
    "InstructionResolver",
    "InstructionScope",
    "InstructionSource",
    "PostSkillDispatchHook",
    "PostToolUseHook",
    "PreSkillDispatchHook",
    "PreToolUseHook",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionPrompter",
    "PermissionRequest",
    "PermissionRule",
    "PythonScriptExecutor",
    "ResolvedInstruction",
    "ScriptDescriptor",
    "ScriptExecutionError",
    "ScriptExecutor",
    "ScriptInvocation",
    "ScriptLanguage",
    "ScriptResult",
    "ShellScriptExecutor",
    "TurnOutcome",
    "TurnRunner",
    "UpdateInstructions",
    "__version__",
    "assistant_message",
    "function_call",
    "function_call_output",
    "rebuild_index",
    "suspension_item",
    "user_message",
]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute：``OtelSinkConfig`` / ``OtelTelemetrySink`` 走 optional extra。

    未装 ``taifeng[telemetry-otel]`` 时，``from taifeng import *`` 不报错；
    只有真访问这两个名字时才触发 OTel SDK import 链，缺包则在 ``OtelTelemetrySink.__init__``
    抛 ``RuntimeError`` 指引安装。
    """
    if name in ("OtelSinkConfig", "OtelTelemetrySink"):
        from taifeng.telemetry import otel_sink

        return getattr(otel_sink, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
