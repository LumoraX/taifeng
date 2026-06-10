"""主循环 —— AgentEngine、Submission / EventMsg 双向消息总线、TurnRunner、Pool。

设计文档：docs/architecture/agent-loop.md
决策记录：docs/decisions/0005-submission-event-bus.md

注意：本 ``__init__`` 仅 re-export 无内部依赖的基础类型，避免循环引用。
重量级类（AgentEngine / TurnRunner / EnginePool）通过直接子模块路径访问：
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.turn import TurnRunner
    from taifeng.loop.pool import EnginePool

或通过顶层 ``taifeng.AgentEngine`` 访问。
"""

from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import (
    AssistantReasoning,
    AssistantText,
    CacheBreakDetected,
    CompactionCompleted,
    CompactionDegradationWarning,
    CompactionIntegrityRolledBack,
    CompactionStarted,
    ContextBudgetExceeded,
    EngineLog,
    EventMsg,
    InstructionCacheHit,
    InstructionFetched,
    InstructionFetchFailed,
    InstructionUpdated,
    InstructionUpdateRejected,
    Msg,
    MsgKind,
    DenialCircuitOpen,
    PermissionPromptTimeout,
    ResourceLimitExceeded,
    SkillDispatched,
    SkillDispatchHookDenied,
    SkillDispatchPermissionDenied,
    SkillReturned,
    SkillSpawnRejected,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from taifeng.loop.event import (
    Shutdown as ShutdownMsg,
)
from taifeng.loop.submission import (
    CancelTurn,
    CompactNow,
    InjectSystemMessage,
    Interrupt,
    Op,
    RefreshSnapshot,
    Shutdown,
    Submission,
    ThreadRollback,
    UpdateBudget,
    UpdateInstructions,
    UserMessage,
)

__all__ = [
    "AssistantReasoning",
    "AssistantText",
    "CacheBreakDetected",
    "CancelTurn",
    "CancellationToken",
    "CompactNow",
    "CompactionCompleted",
    "CompactionDegradationWarning",
    "CompactionIntegrityRolledBack",
    "CompactionStarted",
    "ContextBudgetExceeded",
    "EngineLog",
    "EventMsg",
    "InjectSystemMessage",
    "InstructionCacheHit",
    "InstructionFetchFailed",
    "InstructionFetched",
    "InstructionUpdateRejected",
    "InstructionUpdated",
    "Interrupt",
    "Msg",
    "MsgKind",
    "Op",
    "DenialCircuitOpen",
    "PermissionPromptTimeout",
    "RefreshSnapshot",
    "Shutdown",
    "ShutdownMsg",
    "SkillDispatchHookDenied",
    "SkillDispatchPermissionDenied",
    "SkillDispatched",
    "ResourceLimitExceeded",
    "SkillReturned",
    "SkillSpawnRejected",
    "Submission",
    "ThreadRollback",
    "ToolCallCompleted",
    "ToolCallStarted",
    "TurnCompleted",
    "TurnFailed",
    "TurnStarted",
    "UpdateBudget",
    "UpdateInstructions",
    "UserMessage",
]
