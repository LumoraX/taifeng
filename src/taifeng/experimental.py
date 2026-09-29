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

from taifeng.context.strategies import (
    BackgroundCompactionStrategy,
    MultimodalEvictionStrategy,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.origin import (
    InputOrigin,
    InputTaint,
    origin_of,
    summarize_taint,
    taint_from_extras,
)
from taifeng.llm.file_input import FileAttachmentV1, FileInputPolicy
from taifeng.llm.providers.replay import (
    JournalReplayClient,
    RecordedCall,
    ReplayDivergenceError,
    ReplayUnsupportedError,
    recorded_calls,
)
from taifeng.llm.recovery import RecoveryRecipeBook
from taifeng.llm.types import FilePart
from taifeng.loop.audit_config import AuditCapabilityError, AuditConfig
from taifeng.loop.audit_resume_resolution import (
    AuditToolOutcomeRequest,
    AuditToolOutcomeResolution,
)
from taifeng.loop.failure_policy import (
    RecipeDeclaringPolicy,
    RecoveryRecipeProvider,
)
from taifeng.mcp.bridge import McpToolBinding, bind_mcp_tools
from taifeng.mcp.http_client import McpHttpClient
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
from taifeng.skill.working_set import (
    FitnessScorer,
    SkillFitnessScore,
    WilsonFitnessScorer,
    WorkingSetPlan,
    WorkingSetPolicy,
    plan_working_set,
)
from taifeng.tool.spec import ReconcileVerdict

__all__ = [
    # strict audit Session + durable Journal（session-journal，🧪）
    "AuditCapabilityError",
    "AuditConfig",
    "JsonlSessionJournalCore",
    # 审计 resume 时人对结果未知工具调用的裁决（ADR 0070，🧪）
    "AuditToolOutcomeRequest",
    "AuditToolOutcomeResolution",
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
    # 用户文件（PDF）输入（llm-file-input，🧪）
    "FileAttachmentV1",
    "FileInputPolicy",
    "FilePart",
]
