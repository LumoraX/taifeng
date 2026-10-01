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

from typing import Any

from taifeng.context.engine import (
    AssembledContext,
    AssembleRequest,
    ContextEngine,
    ContextEngineError,
    TailWindowContextEngine,
    TurnUpdate,
)
from taifeng.context.strategies import (
    BackgroundCompactionStrategy,
    MultimodalEvictionStrategy,
)
from taifeng.conversation.journal.backend import (
    ZERO_HASH,
    CommittedRecord,
    SealedBatch,
    SessionJournalCore,
    descriptor_fingerprint,
    ended_record_id,
    index_envelopes,
    resolve_idempotent_ack,
    seal_batch,
    snapshot_records,
    verify_envelopes,
)
from taifeng.conversation.journal.canonical import canonical_hash, record_fingerprint
from taifeng.conversation.journal.errors import (
    CommitNotStartedError,
    JournalAlreadyExistsError,
    JournalBusyError,
    JournalConflictError,
    JournalError,
    JournalIntegrityError,
    JournalLeaseError,
    JournalLockUnsupportedError,
    JournalRecoveryRequiredError,
    JournalSessionEndedError,
    JournalSessionNotFoundError,
)
from taifeng.conversation.journal.file_io import DefaultSyncFileAdapter, SyncFileAdapter
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.legacy_import import (
    LegacyImportError,
    LegacyImportResult,
    import_legacy_transcript,
)
from taifeng.conversation.journal.memory import (
    InMemoryJournalStorage,
    InMemorySessionJournalCore,
)
from taifeng.conversation.journal.models import (
    SESSION_ENDED_RECORD_TYPE,
    WRITER_TAKEOVER_RECORD_TYPE,
    ActorRef,
    Durability,
    JournalAck,
    JournalEnvelope,
    JournalHealth,
    JournalRecord,
    JournalVerification,
    RootThreadDescriptor,
    SessionCreateResult,
    SessionDescriptor,
    SessionLease,
    SessionOpenResult,
    WriterTakeoverV1,
    build_initialization_records,
    build_takeover_record,
)
from taifeng.conversation.journal.projection_rebuild import (
    ProjectionRebuildResult,
    rebuild_projections,
)
from taifeng.conversation.journal.redaction import RedactedPayload, redact_payload
from taifeng.conversation.journal.timeline import (
    JournalTimelineProjector,
    TimelineFilter,
    TimelineItem,
    TimelinePage,
)
from taifeng.conversation.journal.writer_lock import (
    FcntlWriterLockAdapter,
    WriterLockAdapter,
    WriterLockBusyError,
)
from taifeng.conversation.origin import (
    InputOrigin,
    InputTaint,
    origin_of,
    summarize_taint,
    taint_from_extras,
)
from taifeng.llm.prewarm import CachePrimingPrewarmer, ModelPrewarmer, PrewarmOutcome
from taifeng.llm.providers.replay import (
    JournalReplayClient,
    RecordedCall,
    ReplayDivergenceError,
    ReplayUnsupportedError,
    recorded_calls,
)
from taifeng.llm.recovery import RecoveryRecipeBook
from taifeng.loop.audit_config import AuditCapabilityError, AuditConfig
from taifeng.loop.audit_resume_resolution import (
    AuditToolOutcomeRequest,
    AuditToolOutcomeResolution,
)
from taifeng.loop.audit_suspension import AuditedResumeRejectedError
from taifeng.loop.failure_policy import (
    RecipeDeclaringPolicy,
    RecoveryRecipeProvider,
)
from taifeng.loop.replay_session import (
    RecordedSubmission,
    ReplayReport,
    ReplayStep,
    recorded_submissions,
    replay_session,
)
from taifeng.loop.submission import Prewarm
from taifeng.skill.authorization import (
    CallbackSkillAuthorization,
    PermissionSkillAuthorization,
    SkillAuthorizationDecision,
    SkillAuthorizationPolicy,
    SkillAuthorizationRequest,
)
from taifeng.skill.fitness import (
    InMemorySkillFitnessStore,
    SkillFitness,
    SkillFitnessCatalog,
    SkillFitnessLedger,
    SkillFitnessRecorder,
    SkillFitnessStore,
)
from taifeng.skill.fitness_shadow import (
    ShadowEvaluation,
    ShadowObserver,
    SkillFitnessShadow,
)
from taifeng.skill.selection import (
    RoutedCandidate,
    SelectionCandidate,
    SelectionConfidencePolicy,
    SkillSelectionGate,
    ThresholdSelectionPolicy,
    TrialJudge,
    TrialVerdict,
    VerifierTrialJudge,
)
from taifeng.skill.trust import SkillTrustPolicy, SourceTrustPolicy
from taifeng.skill.working_set import (
    FitnessScorer,
    SkillFitnessScore,
    TierRule,
    WilsonFitnessScorer,
    WorkingSetPlan,
    WorkingSetPolicy,
    plan_working_set,
)
from taifeng.skill.working_set_runtime import (
    SkillWorkingSet,
    WorkingSetChange,
    WorkingSetView,
)
from taifeng.tool.replay import (
    KERNEL_TOOLS,
    RecordedToolCall,
    ToolReplayLedger,
    recorded_tool_calls,
    replay_tools,
)
from taifeng.tool.spec import ReconcileVerdict

__all__ = [
    # strict audit Session + durable Journal（session-journal，🧪）
    "AuditCapabilityError",
    "AuditConfig",
    "JsonlSessionJournalCore",
    # Journal 后端 seam（session-journal-backend，ADR 0114，🧪）
    # —— core 协议与它签名里的类型
    "SessionJournalCore",
    "ActorRef",
    "Durability",
    "JournalAck",
    "JournalEnvelope",
    "JournalHealth",
    "JournalRecord",
    "JournalVerification",
    "RootThreadDescriptor",
    "SessionCreateResult",
    "SessionDescriptor",
    "SessionLease",
    "SessionOpenResult",
    "WriterTakeoverV1",
    # —— 错误
    "CommitNotStartedError",
    "JournalAlreadyExistsError",
    "JournalBusyError",
    "JournalConflictError",
    "JournalError",
    "JournalIntegrityError",
    "JournalLeaseError",
    "JournalLockUnsupportedError",
    "JournalRecoveryRequiredError",
    "JournalSessionEndedError",
    "JournalSessionNotFoundError",
    # —— 换存储：注入 JsonlSessionJournalCore 的两个适配器
    "DefaultSyncFileAdapter",
    "FcntlWriterLockAdapter",
    "SyncFileAdapter",
    "WriterLockAdapter",
    "WriterLockBusyError",
    # —— 换 core：存储无关的构件与参考实现
    "SESSION_ENDED_RECORD_TYPE",
    "WRITER_TAKEOVER_RECORD_TYPE",
    "ZERO_HASH",
    "CommittedRecord",
    "InMemoryJournalStorage",
    "InMemorySessionJournalCore",
    "SealedBatch",
    "build_initialization_records",
    "build_takeover_record",
    "canonical_hash",
    "descriptor_fingerprint",
    "ended_record_id",
    "index_envelopes",
    "record_fingerprint",
    "resolve_idempotent_ack",
    "seal_batch",
    "snapshot_records",
    "verify_envelopes",
    # Journal Phase 5：Timeline 投影、脱敏、旧 transcript 导入、投影重建（ADR 0104，🧪）
    "JournalTimelineProjector",
    "TimelineFilter",
    "TimelineItem",
    "TimelinePage",
    "RedactedPayload",
    "redact_payload",
    "LegacyImportError",
    "LegacyImportResult",
    "import_legacy_transcript",
    "ProjectionRebuildResult",
    "rebuild_projections",
    # 审计 resume 时人对结果未知工具调用的裁决（ADR 0070，🧪）
    "AuditToolOutcomeRequest",
    "AuditToolOutcomeResolution",
    # 审计模式下不适用的 Resume（ADR 0097，🧪）
    "AuditedResumeRejectedError",
    # Journal 确定性回放（journal-replay，🧪）；工具回放与整条重放（ADR 0105，🧪）
    "KERNEL_TOOLS",
    "RecordedToolCall",
    "ToolReplayLedger",
    "recorded_tool_calls",
    "replay_tools",
    "RecordedSubmission",
    "ReplayReport",
    "ReplayStep",
    "recorded_submissions",
    "replay_session",
    "JournalReplayClient",
    "RecordedCall",
    "ReplayDivergenceError",
    "ReplayUnsupportedError",
    "recorded_calls",
    # 工具崩溃对账的回查结果（tool-crash-reconciliation，🧪）
    "ReconcileVerdict",
    # skill 战绩聚合（skill-fitness，🧪：只沉淀不决策）
    "InMemorySkillFitnessStore",
    "SkillFitness",
    "SkillFitnessCatalog",
    "SkillFitnessLedger",
    "SkillFitnessRecorder",
    "SkillFitnessStore",
    # 按战绩算分与工作集规划 + 影子模式（skill-working-set，🧪：只算分不生效）
    "FitnessScorer",
    "ShadowEvaluation",
    "ShadowObserver",
    "SkillFitnessScore",
    "SkillFitnessShadow",
    "WilsonFitnessScorer",
    "WorkingSetPlan",
    "WorkingSetPolicy",
    "plan_working_set",
    # 上下文引擎（context-engine，🧪）
    "AssembleRequest",
    "AssembledContext",
    "ContextEngine",
    "ContextEngineError",
    "TailWindowContextEngine",
    "TurnUpdate",
    # 预热（prewarm，🧪）
    "CachePrimingPrewarmer",
    "ModelPrewarmer",
    "Prewarm",
    "PrewarmOutcome",
    # 白名单外 skill 的派发授权（skill-authorization，🧪）
    "CallbackSkillAuthorization",
    "PermissionSkillAuthorization",
    "SkillAuthorizationDecision",
    "SkillAuthorizationPolicy",
    "SkillAuthorizationRequest",
    # 按选择置信度分流（skill-selection-gate，🧪）
    "RoutedCandidate",
    "SelectionCandidate",
    "SelectionConfidencePolicy",
    "SkillSelectionGate",
    "ThresholdSelectionPolicy",
    "TrialJudge",
    "TrialVerdict",
    "VerifierTrialJudge",
    # 工作集生效与来源信任分层（skill-working-set，🧪）
    "SkillTrustPolicy",
    "SkillWorkingSet",
    "SourceTrustPolicy",
    "TierRule",
    "WorkingSetChange",
    "WorkingSetView",
    # 输入来源标记（input-origin，🧪）
    "InputOrigin",
    "InputTaint",
    "origin_of",
    "summarize_taint",
    "taint_from_extras",
    # 可声明的失败恢复配方（failure-recovery-recipes，🧪）
    "RecipeDeclaringPolicy",
    "RecoveryRecipeBook",
    "RecoveryRecipeProvider",
    # 后台延迟压缩（compaction-background，🧪）
    "BackgroundCompactionStrategy",
    # 多模态重载荷驱逐（compaction-multimodal-eviction，🧪）
    "MultimodalEvictionStrategy",
]

# 已晋升到稳定层的名字：在本模块保留至少一个发布版本（ADR 0066 决策 5），访问时提示新位置。
_PROMOTED: dict[str, str] = {
    # ADR 0110（2026.9.30 之后的第一个发布）：MCP HTTP 与工具集绑定
    "McpHttpClient": "taifeng.mcp.http_client",
    "McpToolBinding": "taifeng.mcp.bridge",
    "bind_mcp_tools": "taifeng.mcp.bridge",
    # ADR 0116：用户文件（PDF）输入
    "FileAttachmentV1": "taifeng.llm.file_input",
    "FileInputPolicy": "taifeng.llm.file_input",
    "FilePart": "taifeng.llm.types",
}


def __getattr__(name: str) -> Any:
    """已晋升的名字照常可用，但发 ``DeprecationWarning`` 指向 ``taifeng`` 顶层。"""
    module_path = _PROMOTED.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    import warnings

    warnings.warn(
        f"taifeng.experimental.{name} has been promoted to the stable layer; "
        f"import it from taifeng instead (the experimental alias will be removed in a later release)",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(module_path), name)
