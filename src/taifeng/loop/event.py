"""EventMsg —— 引擎对外的语义事件。

参照：
    - codex codex-rs/codex-protocol/src/protocol.rs::EventMsg
    - claw-code lane_events.rs

设计选择：业务粒度而非 LLM 粒度。每个 EventMsg 都带 ``submission_id`` 以方便订阅过滤。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Union

from pydantic import BaseModel, Field

from taifeng.loop.event_base import MsgKind as MsgKind
from taifeng.loop.event_base import _Msg as _Msg
from taifeng.loop.event_lifecycle import DirectoryCursorReset as DirectoryCursorReset
from taifeng.loop.event_lifecycle import EngineLog as EngineLog
from taifeng.loop.event_lifecycle import IndexHookAbandoned as IndexHookAbandoned
from taifeng.loop.event_lifecycle import IndexHookFailed as IndexHookFailed
from taifeng.loop.event_lifecycle import InstructionCacheHit as InstructionCacheHit
from taifeng.loop.event_lifecycle import InstructionFetched as InstructionFetched
from taifeng.loop.event_lifecycle import InstructionFetchFailed as InstructionFetchFailed
from taifeng.loop.event_lifecycle import InstructionUpdated as InstructionUpdated
from taifeng.loop.event_lifecycle import InstructionUpdateRejected as InstructionUpdateRejected
from taifeng.loop.event_lifecycle import JoinBarrierFired as JoinBarrierFired
from taifeng.loop.event_lifecycle import JoinBarrierRegistered as JoinBarrierRegistered
from taifeng.loop.event_lifecycle import RebuildSkippedCorrupt as RebuildSkippedCorrupt
from taifeng.loop.event_lifecycle import RewindCheckpointRecorded as RewindCheckpointRecorded
from taifeng.loop.event_lifecycle import RewindRejected as RewindRejected
from taifeng.loop.event_lifecycle import RewindTableRebuilt as RewindTableRebuilt
from taifeng.loop.event_lifecycle import Shutdown as Shutdown
from taifeng.loop.event_lifecycle import SpawnCancelled as SpawnCancelled
from taifeng.loop.event_lifecycle import SpawnCompleted as SpawnCompleted
from taifeng.loop.event_lifecycle import SpawnFailed as SpawnFailed
from taifeng.loop.event_lifecycle import SpawnStarted as SpawnStarted
from taifeng.loop.event_lifecycle import SpawnSuspended as SpawnSuspended
from taifeng.loop.event_lifecycle import SqliteDbCorruptRebuilt as SqliteDbCorruptRebuilt
from taifeng.loop.event_lifecycle import SqliteSchemaRebuilt as SqliteSchemaRebuilt
from taifeng.loop.event_lifecycle import SuspensionExpired as SuspensionExpired
from taifeng.loop.event_lifecycle import SuspensionPartiallyResolved as SuspensionPartiallyResolved
from taifeng.loop.event_lifecycle import SuspensionResolved as SuspensionResolved
from taifeng.loop.event_lifecycle import SuspensionResolveRejected as SuspensionResolveRejected
from taifeng.loop.event_lifecycle import ThreadIndexedOrphan as ThreadIndexedOrphan
from taifeng.loop.event_lifecycle import ThreadResumed as ThreadResumed
from taifeng.loop.event_lifecycle import (
    TranscriptSkippedCorruptLine as TranscriptSkippedCorruptLine,
)
from taifeng.loop.event_lifecycle import TurnCompleted as TurnCompleted
from taifeng.loop.event_lifecycle import TurnFailed as TurnFailed
from taifeng.loop.event_lifecycle import TurnRewound as TurnRewound
from taifeng.loop.event_lifecycle import TurnSuspended as TurnSuspended
from taifeng.loop.event_turn import AssistantReasoning as AssistantReasoning
from taifeng.loop.event_turn import AssistantText as AssistantText
from taifeng.loop.event_turn import BackgroundTaskCompleted as BackgroundTaskCompleted
from taifeng.loop.event_turn import BudgetHintInjected as BudgetHintInjected
from taifeng.loop.event_turn import CacheBreakDetected as CacheBreakDetected
from taifeng.loop.event_turn import CompactionCompleted as CompactionCompleted
from taifeng.loop.event_turn import CompactionDeferred as CompactionDeferred
from taifeng.loop.event_turn import CompactionDegradationWarning as CompactionDegradationWarning
from taifeng.loop.event_turn import CompactionIntegrityRolledBack as CompactionIntegrityRolledBack
from taifeng.loop.event_turn import CompactionStarted as CompactionStarted
from taifeng.loop.event_turn import ContextAssembled as ContextAssembled
from taifeng.loop.event_turn import ContextBudgetExceeded as ContextBudgetExceeded
from taifeng.loop.event_turn import DenialCircuitOpen as DenialCircuitOpen
from taifeng.loop.event_turn import DoomLoopCircuitOpen as DoomLoopCircuitOpen
from taifeng.loop.event_turn import DoomLoopWarned as DoomLoopWarned
from taifeng.loop.event_turn import LlmRequestRecorded as LlmRequestRecorded
from taifeng.loop.event_turn import OrchestrationConditionMissing as OrchestrationConditionMissing
from taifeng.loop.event_turn import OrchestrationPlanResolved as OrchestrationPlanResolved
from taifeng.loop.event_turn import OutboundMessage as OutboundMessage
from taifeng.loop.event_turn import PeerAgentWoken as PeerAgentWoken
from taifeng.loop.event_turn import PeerMessageSent as PeerMessageSent
from taifeng.loop.event_turn import PeerWaitAnyResolved as PeerWaitAnyResolved
from taifeng.loop.event_turn import PeerWaitAnyStarted as PeerWaitAnyStarted
from taifeng.loop.event_turn import PeerWaitResolved as PeerWaitResolved
from taifeng.loop.event_turn import PeerWaitStarted as PeerWaitStarted
from taifeng.loop.event_turn import PermissionPromptTimeout as PermissionPromptTimeout
from taifeng.loop.event_turn import PinnedStateReinjected as PinnedStateReinjected
from taifeng.loop.event_turn import PostTurnHookFired as PostTurnHookFired
from taifeng.loop.event_turn import PreCompactHookSkipped as PreCompactHookSkipped
from taifeng.loop.event_turn import PreTurnHookDenied as PreTurnHookDenied
from taifeng.loop.event_turn import PrewarmCompleted as PrewarmCompleted
from taifeng.loop.event_turn import PrewarmStarted as PrewarmStarted
from taifeng.loop.event_turn import ProviderCircuitClosed as ProviderCircuitClosed
from taifeng.loop.event_turn import ProviderCircuitHalfOpen as ProviderCircuitHalfOpen
from taifeng.loop.event_turn import ProviderCircuitOpened as ProviderCircuitOpened
from taifeng.loop.event_turn import ProviderRetry as ProviderRetry
from taifeng.loop.event_turn import ResourceLimitExceeded as ResourceLimitExceeded
from taifeng.loop.event_turn import SkillAuthorizationDenied as SkillAuthorizationDenied
from taifeng.loop.event_turn import SkillAuthorizationGranted as SkillAuthorizationGranted
from taifeng.loop.event_turn import SkillCandidatesReturned as SkillCandidatesReturned
from taifeng.loop.event_turn import SkillCandidatesVerified as SkillCandidatesVerified
from taifeng.loop.event_turn import SkillDispatched as SkillDispatched
from taifeng.loop.event_turn import SkillDispatchHookDenied as SkillDispatchHookDenied
from taifeng.loop.event_turn import SkillDispatchPermissionDenied as SkillDispatchPermissionDenied
from taifeng.loop.event_turn import SkillEvicted as SkillEvicted
from taifeng.loop.event_turn import SkillOutcomeRecorded as SkillOutcomeRecorded
from taifeng.loop.event_turn import SkillPromoted as SkillPromoted
from taifeng.loop.event_turn import SkillQuarantined as SkillQuarantined
from taifeng.loop.event_turn import SkillReleased as SkillReleased
from taifeng.loop.event_turn import SkillReturned as SkillReturned
from taifeng.loop.event_turn import SkillSearchInvoked as SkillSearchInvoked
from taifeng.loop.event_turn import SkillSelectionGated as SkillSelectionGated
from taifeng.loop.event_turn import SkillSelectionRouted as SkillSelectionRouted
from taifeng.loop.event_turn import SkillSpawnRejected as SkillSpawnRejected
from taifeng.loop.event_turn import SubagentPolicyOverridden as SubagentPolicyOverridden
from taifeng.loop.event_turn import SubmissionQueued as SubmissionQueued
from taifeng.loop.event_turn import SystemMessageInjected as SystemMessageInjected
from taifeng.loop.event_turn import ToolBatchDispatched as ToolBatchDispatched
from taifeng.loop.event_turn import ToolCallCompleted as ToolCallCompleted
from taifeng.loop.event_turn import ToolCallStarted as ToolCallStarted
from taifeng.loop.event_turn import ToolSetChanged as ToolSetChanged
from taifeng.loop.event_turn import TurnStarted as TurnStarted
from taifeng.loop.event_turn import UserInputInjected as UserInputInjected

# ── turn-rewind：回访节点生命周期事件（R3 可观测）─────────────────────

# ── detached-spawn：分离式 skill spawn + join-barrier 生命周期事件（R3 可观测）──

Msg = Union[
    TurnStarted,
    AssistantText,
    AssistantReasoning,
    ToolCallStarted,
    ToolCallCompleted,
    ToolBatchDispatched,
    OrchestrationPlanResolved,
    OrchestrationConditionMissing,
    SkillDispatched,
    SkillReturned,
    SkillOutcomeRecorded,
    SkillSearchInvoked,
    SkillCandidatesReturned,
    SkillCandidatesVerified,
    SkillSpawnRejected,
    ResourceLimitExceeded,
    DenialCircuitOpen,
    DoomLoopWarned,
    DoomLoopCircuitOpen,
    CompactionStarted,
    CompactionCompleted,
    CompactionDegradationWarning,
    CompactionIntegrityRolledBack,
    PinnedStateReinjected,
    BudgetHintInjected,
    ContextBudgetExceeded,
    CacheBreakDetected,
    ProviderRetry,
    LlmRequestRecorded,
    UserInputInjected,
    SystemMessageInjected,
    SubmissionQueued,
    PermissionPromptTimeout,
    SkillDispatchHookDenied,
    SkillDispatchPermissionDenied,
    PreTurnHookDenied,
    PostTurnHookFired,
    PreCompactHookSkipped,
    CompactionDeferred,
    OutboundMessage,
    SkillSelectionRouted,
    SkillSelectionGated,
    SkillAuthorizationGranted,
    SkillAuthorizationDenied,
    ContextAssembled,
    PrewarmStarted,
    PrewarmCompleted,
    SkillPromoted,
    SkillEvicted,
    SkillQuarantined,
    SkillReleased,
    TurnSuspended,
    SuspensionResolved,
    SuspensionPartiallyResolved,
    SuspensionResolveRejected,
    SuspensionExpired,
    ThreadResumed,
    SubagentPolicyOverridden,
    TurnCompleted,
    TurnFailed,
    EngineLog,
    InstructionFetched,
    InstructionCacheHit,
    InstructionUpdated,
    InstructionFetchFailed,
    InstructionUpdateRejected,
    Shutdown,
    TranscriptSkippedCorruptLine,
    SqliteSchemaRebuilt,
    SqliteDbCorruptRebuilt,
    ThreadIndexedOrphan,
    DirectoryCursorReset,
    IndexHookFailed,
    IndexHookAbandoned,
    RebuildSkippedCorrupt,
    RewindCheckpointRecorded,
    TurnRewound,
    RewindRejected,
    RewindTableRebuilt,
    SpawnStarted,
    SpawnSuspended,
    SpawnCompleted,
    SpawnFailed,
    SpawnCancelled,
    JoinBarrierRegistered,
    JoinBarrierFired,
    PeerMessageSent,
    PeerAgentWoken,
    PeerWaitStarted,
    PeerWaitResolved,
    PeerWaitAnyStarted,
    PeerWaitAnyResolved,
    ProviderCircuitOpened,
    ProviderCircuitHalfOpen,
    ProviderCircuitClosed,
    ToolSetChanged,
    BackgroundTaskCompleted,
]

class EventMsg(BaseModel):
    submission_id: str
    msg: Msg = Field(discriminator="kind")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # 审计可观测 层1：事件在「某 engine 事件总线」上的全局单调序号。
    # 由 ``engine._emit`` 入口同步分配（asyncio 单线程无 await 让出点 → 原子不重不漏）；
    # 旧序列化数据冷读默认 0。落库主键用 ``(session_id, seq)``——session_id 由订阅方
    # 按所属 engine 提供，不盖在事件上（详见 docs 设计文档 §4.3）。
    # ⚠️ 全局 seq 连续性自检只对 ``subscribe_all`` 全量流成立；过滤订阅看
    # ``DeliveredEvent.delivery_seq``（per-subscriber 投递序号）。
    seq: int = 0
