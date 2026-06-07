"""EventMsg —— 引擎对外的语义事件。

参照：
    - codex codex-rs/codex-protocol/src/protocol.rs::EventMsg
    - claw-code lane_events.rs

设计选择：业务粒度而非 LLM 粒度。每个 EventMsg 都带 ``submission_id`` 以方便订阅过滤。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Union

from pydantic import BaseModel, Field

MsgKind = Literal[
    "turn_started",
    "assistant_text",
    "assistant_reasoning",
    "tool_call_started",
    "tool_call_completed",
    "tool_batch_dispatched",
    "orchestration_plan_resolved",
    "orchestration_condition_missing",
    "skill_dispatched",
    "skill_returned",
    "compaction_started",
    "compaction_completed",
    "cache_break_detected",
    "permission_prompt_timeout",
    "skill_dispatch_hook_denied",
    "skill_dispatch_permission_denied",
    "pre_turn_hook_denied",
    "pre_compact_hook_skipped",
    "thread_resumed",
    "subagent_policy_overridden",
    "turn_completed",
    "turn_failed",
    "engine_log",
    "instruction_fetched",
    "instruction_cache_hit",
    "instruction_updated",
    "instruction_fetch_failed",
    "instruction_update_rejected",
    "shutdown",
    # store-protocol-decoupling 新增：持久化层事件
    "transcript_skipped_corrupt_line",
    "sqlite_schema_rebuilt",
    "sqlite_db_corrupt_rebuilt",
    "thread_indexed_orphan",
    "directory_cursor_reset",
    "index_hook_failed",
    "index_hook_abandoned",
    "rebuild_skipped_corrupt",
    # suspend-resume 生命周期事件
    "turn_suspended",
    "suspension_resolved",
    "suspension_resolve_rejected",
    # turn-rewind 回访节点生命周期
    "rewind_checkpoint_recorded",
    "turn_rewound",
    "rewind_rejected",
    "rewind_table_rebuilt",
    # detached-spawn 生命周期
    "spawn_started",
    "spawn_suspended",
    "spawn_completed",
    "spawn_failed",
    "spawn_cancelled",
    "join_barrier_registered",
    "join_barrier_fired",
]


class _Msg(BaseModel):
    kind: MsgKind
    data: dict[str, Any] = Field(default_factory=dict)


class TurnStarted(_Msg):
    kind: Literal["turn_started"] = "turn_started"


class AssistantText(_Msg):
    kind: Literal["assistant_text"] = "assistant_text"
    """data = {"delta": str}"""


class AssistantReasoning(_Msg):
    kind: Literal["assistant_reasoning"] = "assistant_reasoning"
    """data = {"delta": str}"""


class ToolCallStarted(_Msg):
    kind: Literal["tool_call_started"] = "tool_call_started"
    """data = {"call_id": str, "name": str, "arguments": str}"""


class ToolCallCompleted(_Msg):
    kind: Literal["tool_call_completed"] = "tool_call_completed"
    """data = {"call_id": str, "name": str, "output": str, "is_error": bool, "duration_ms": int}"""


class ToolBatchDispatched(_Msg):
    """一批 tool call 进入并发派发阶段（阶段 2 开始）。

    data = {"count": int, "max_parallel": int}
    - count: 本批 tool call 数量
    - max_parallel: 当前生效的并发上限（Semaphore 容量）
    并发度=1 时本事件仍 emit（count 可 >1，max_parallel=1 表示串行执行）。
    """

    kind: Literal["tool_batch_dispatched"] = "tool_batch_dispatched"


class OrchestrationPlanResolved(_Msg):
    """声明了 orchestration 的 entry skill 开始执行 —— 声明被解析为执行计划。

    data = {"skill_id": str, "groups": list[dict]}
    groups 形如 [{"type":"parallel","skills":[...]}, {"type":"serial","skills":[...]},
              {"type":"when","condition": str}]
    """

    kind: Literal["orchestration_plan_resolved"] = "orchestration_plan_resolved"


class OrchestrationConditionMissing(_Msg):
    """when.condition 引用的 flag 在上一步输出缺失/非布尔 —— 随后该 turn 硬失败。

    data = {"skill_id": str, "condition": str}
    本事件紧跟其后会触发 TurnFailed（OrchestrationConditionError 被通用 except 捕获）。
    """

    kind: Literal["orchestration_condition_missing"] = "orchestration_condition_missing"


class SkillDispatched(_Msg):
    kind: Literal["skill_dispatched"] = "skill_dispatched"
    """data = {"skill_id": str, "call_id": str, "depth": int, "stack_path": list[str]}"""


class SkillReturned(_Msg):
    kind: Literal["skill_returned"] = "skill_returned"
    """data = {"skill_id": str, "call_id": str, "success": bool, "summary": str}"""


class SkillSpawnRejected(_Msg):
    """K1：子 skill 派发因 spawn 配额超限被拒（fork-bomb 防护）。"""

    kind: Literal["skill_spawn_rejected"] = "skill_spawn_rejected"
    """data = {"skill_id": str, "call_id": str, "limit_kind": str, "limit": int}"""


class ResourceLimitExceeded(_Msg):
    """K2：累计资源（token）触顶 → turn 被强制中止（OOM-killer）。"""

    kind: Literal["resource_limit_exceeded"] = "resource_limit_exceeded"
    """data = {"limit_kind": str, "used": int, "limit": int, "scope": str}"""


class CompactionStarted(_Msg):
    kind: Literal["compaction_started"] = "compaction_started"
    """data = {"phase": str, "strategy": str, "token_estimate": int}"""


class CompactionCompleted(_Msg):
    kind: Literal["compaction_completed"] = "compaction_completed"
    """data = {"success": bool, "cache_invalidated": bool, "removed_count": int, "reason": str|None}"""


class CacheBreakDetected(_Msg):
    kind: Literal["cache_break_detected"] = "cache_break_detected"
    """data = {"unexpected": bool, "reason": str, "token_drop": int}"""


class CompactionDegradationWarning(_Msg):
    """G1c：单一 thread 内压缩次数达到阈值 —— 多次压缩累积会降低准确率，
    提示业务侧/用户考虑开新 thread。"""

    kind: Literal["compaction_degradation_warning"] = "compaction_degradation_warning"
    """data = {"compaction_count": int, "threshold": int}"""


class CompactionIntegrityRolledBack(_Msg):
    """G1b：压缩产物的 tool_call/output 配对完整性校验失败 —— 不应用该压缩结果，
    保留原 history（保留历史优于把损坏会话喂给 provider）。"""

    kind: Literal["compaction_integrity_rolled_back"] = (
        "compaction_integrity_rolled_back"
    )
    """data = {"issues": list[str], "phase": str}"""


class ContextBudgetExceeded(_Msg):
    """G2b：发送前预检 —— 即便经过压缩，估算 token 仍超 hard limit。
    非阻塞告警（估算偏粗，不据此拒发），供业务侧主动限流 / 排查。"""

    kind: Literal["context_budget_exceeded"] = "context_budget_exceeded"
    """data = {"token_estimate": int, "hard_limit": int, "context_window": int}"""


class PermissionPromptTimeout(_Msg):
    """PermissionPolicy 调 prompter 超时 —— 已自动 deny。"""

    kind: Literal["permission_prompt_timeout"] = "permission_prompt_timeout"
    """data = {"scope": str, "target": str, "timeout_seconds": float,
              "call_chain": list[str]}"""


class SkillDispatchHookDenied(_Msg):
    """pre_skill_dispatch hook 拒绝了 child skill 派发。"""

    kind: Literal["skill_dispatch_hook_denied"] = "skill_dispatch_hook_denied"
    """data = {"target_skill_id": str, "caller_skill_id": str,
              "hook_reason": str, "call_chain": list[str]}"""


class SkillDispatchPermissionDenied(_Msg):
    """PermissionPolicy 拒绝了 child skill 派发。"""

    kind: Literal["skill_dispatch_permission_denied"] = (
        "skill_dispatch_permission_denied"
    )
    """data = {"target_skill_id": str, "caller_skill_id": str,
              "reason": str, "call_chain": list[str]}"""


class PreTurnHookDenied(_Msg):
    """pre_turn hook 拒绝了 turn 启动 —— TurnRunner 不会被实例化。

    紧跟其后会 emit ``turn_failed``（error="pre_turn_hook_denied"）；
    user_message 仍持久化（resume 友好）；engine._turn_index 仍 +1。
    """

    kind: Literal["pre_turn_hook_denied"] = "pre_turn_hook_denied"
    """data = {"reason": str, "user_text_preview": str, "iteration": int}"""


class PreCompactHookSkipped(_Msg):
    """pre_compact hook 拒绝了本轮压缩 —— history / cache_anchor 保持不变。

    本事件与 ``compaction_started`` 互斥（同一次 ``_maybe_compress`` 调用内）。
    turn 主循环不报错、继续执行。
    """

    kind: Literal["pre_compact_hook_skipped"] = "pre_compact_hook_skipped"
    """data = {"phase": str, "reason": str, "token_estimate": int,
              "history_length": int}"""


class SubagentPolicyOverridden(_Msg):
    """G3 subagent-isolation-policy: 子 turn 派发时 PermissionPolicy 被包装。

    business 侧可订阅本事件审计哪些子 skill 走了 auto_deny / auto_allow，与
    inherit 模式（无事件）区分。emit 时机：``TurnRunner.run_sub_skill`` 创建
    ``_SubagentAutoDecisionPolicy`` 包装时，**SkillDispatched 之后、子 turn 启动
    之前**。inherit 模式不 emit。

    data = {"target_skill_id": str, "mode": "auto_deny|auto_allow", "depth": int}
    """

    kind: Literal["subagent_policy_overridden"] = "subagent_policy_overridden"


class TurnSuspended(_Msg):
    """turn 在中途挂起，实例可释放；业务凭 thread_id + record_id 提交 Resume 续跑。

    data = {
        "thread_id": str,
        "record_id": str,
        "pending": list[dict],     # 每项 {request_id, reason, payload_schema,
                                   #        related_call_id, detail}
        "cache_invalidated": bool, # tier-2 跨进程 resume 必须为 True
    }
    """

    kind: Literal["turn_suspended"] = "turn_suspended"


class SuspensionResolved(_Msg):
    """Resume 成功配对，turn 续跑。

    data = {"record_id": str, "request_ids": list[str]}
    """

    kind: Literal["suspension_resolved"] = "suspension_resolved"


class SuspensionResolveRejected(_Msg):
    """Resume 被拒（resolution 不全 / 多余、record 已消费、payload 不符 schema 等）。

    data = {"reason": str, "record_id": str | None, "detail": dict}
    """

    kind: Literal["suspension_resolve_rejected"] = "suspension_resolve_rejected"


class ThreadResumed(_Msg):
    """EnginePool 从已有 thread_id 恢复了 engine —— history 已注入。

    business 侧可订阅本事件获悉哪些 session 走了 resume 路径；与新建 thread
    （无对应事件）区分开。emit 时机：pool.get_or_create 内部 engine.run 启动
    之后；订阅者若在此之前 attach 即可消费，否则会丢（既有 emit 语义）。
    """

    kind: Literal["thread_resumed"] = "thread_resumed"
    """data = {
        "thread_id": str,
        "item_count": int,
        "entry_skill_id_at_resume": str,
        "entry_skill_id_recorded": str | None,
    }"""


class TurnCompleted(_Msg):
    kind: Literal["turn_completed"] = "turn_completed"
    """data = {"iterations": int, "duration_ms": int, "usage": dict,
              "end_reason": str, "success": bool, "is_root": bool}

    ``is_root`` —— 本 turn 是否是根 turn（从 Engine 直接派发，不是 ``call_skill``
    派发出来的子 turn）。订阅 ``engine.subscribe(submission_id)`` 的业务桥接层
    应当只在 ``is_root=True`` 时认为本 submission 已结束；子 turn 的 completed
    仍在父 turn 的执行窗口内，**不应**触发桥接层提前退出。

    向后兼容：旧事件流不含此字段；消费方 ``data.get("is_root", False)`` 兜底。
    """


class TurnFailed(_Msg):
    kind: Literal["turn_failed"] = "turn_failed"
    """data = {"error": str, "kind": str, "failure_class": str,
    "suggested_action": str, "recovery": dict, "iterations": int,
    "is_root": bool}

    ``failure_class`` 是 G3 的稳定分类桶（见 ``llm.errors.FailureClass``），
    供 telemetry 聚合；``suggested_action`` 为人类可读处置建议；``recovery``
    是机读的结构化恢复配方（``llm.recovery.RecoveryPlan.to_dict()``）。

    ``is_root`` 语义同 ``TurnCompleted`` —— 子 turn 失败仍属于父 turn 执行窗口内的
    中间事件，业务桥接层不应据此判定 submission 终结。
    """


class EngineLog(_Msg):
    kind: Literal["engine_log"] = "engine_log"
    """data = {"level": str, "message": str, "extra": dict}"""


class InstructionFetched(_Msg):
    """instructions-injection: 动态 source 完成一次 fetch（cache miss）。"""

    kind: Literal["instruction_fetched"] = "instruction_fetched"
    """data = {"layer_name": str, "scope": str, "duration_ms": int, "text_length": int}"""


class InstructionCacheHit(_Msg):
    """instructions-injection: 缓存命中跳过 fetch。"""

    kind: Literal["instruction_cache_hit"] = "instruction_cache_hit"
    """data = {"layer_name": str, "cache_age_seconds": float}"""


class InstructionUpdated(_Msg):
    """instructions-injection: UpdateInstructions Op 成功执行。"""

    kind: Literal["instruction_updated"] = "instruction_updated"
    """data = {"layer_name": str, "new_source_kind": str}"""


class InstructionFetchFailed(_Msg):
    """instructions-injection: InstructionFetchError 抛出前发出。"""

    kind: Literal["instruction_fetch_failed"] = "instruction_fetch_failed"
    """data = {"layer_name": str, "cause_repr": str}"""


class InstructionUpdateRejected(_Msg):
    """instructions-injection: UpdateInstructions Op 拒绝（未知 name 等）。"""

    kind: Literal["instruction_update_rejected"] = "instruction_update_rejected"
    """data = {"layer_name": str, "reason": str}"""


class Shutdown(_Msg):
    kind: Literal["shutdown"] = "shutdown"


class TranscriptSkippedCorruptLine(_Msg):
    """持久化层事件 —— JsonlMessageWriter.load_history 跳过损坏行。

    data = {"thread_id": str, "line_no": int, "cause": str}
    构造 EventMsg 时通常用 submission_id="*" 标记系统级事件（写路径可能在 turn 外触发）。
    """

    kind: Literal["transcript_skipped_corrupt_line"] = "transcript_skipped_corrupt_line"


class SqliteSchemaRebuilt(_Msg):
    """SqliteThreadDirectory 启动期 schema 版本不匹配 → drop + 从 JSONL 重建后发出。

    data = {"old_version": int, "new_version": int, "rebuilt_thread_count": int, "elapsed_ms": float}
    """

    kind: Literal["sqlite_schema_rebuilt"] = "sqlite_schema_rebuilt"


class SqliteDbCorruptRebuilt(_Msg):
    """SqliteThreadDirectory 启动期 integrity_check 失败 → rename 备份 + 重建后发出。

    data = {"backup_path": str, "rebuilt_thread_count": int}
    """

    kind: Literal["sqlite_db_corrupt_rebuilt"] = "sqlite_db_corrupt_rebuilt"


class ThreadIndexedOrphan(_Msg):
    """ThreadDirectory.list_threads 即将返回的 thread 在主存中不存在 → 跳过 + 发事件。

    data = {"thread_id": str}
    """

    kind: Literal["thread_indexed_orphan"] = "thread_indexed_orphan"


class DirectoryCursorReset(_Msg):
    """ThreadDirectory.list_threads cursor 无法解析 → 从头返回 + 发事件。

    data = {"cursor": str, "cause": str}
    """

    kind: Literal["directory_cursor_reset"] = "directory_cursor_reset"


class IndexHookFailed(_Msg):
    """IndexHook 方法抛异常 → 捕获 + 发事件，主路径不受影响。

    data = {"method": str, "thread_id": str | None, "cause": str}
    """

    kind: Literal["index_hook_failed"] = "index_hook_failed"


class IndexHookAbandoned(_Msg):
    """engine.shutdown 5s grace period 后 IndexHook task 仍未完成 → cancel + 发事件。

    data = {"method": str, "thread_id": str | None}
    """

    kind: Literal["index_hook_abandoned"] = "index_hook_abandoned"


class RebuildSkippedCorrupt(_Msg):
    """rebuild_index 扫描时遇到首行损坏 / 不可解析的 thread → 计入 error_count + 发事件。

    data = {"path": str, "cause": str}
    """

    kind: Literal["rebuild_skipped_corrupt"] = "rebuild_skipped_corrupt"


# ── turn-rewind：回访节点生命周期事件（R3 可观测）─────────────────────


class RewindCheckpointRecorded(_Msg):
    """root turn 记下一个回访节点（iteration / dispatch）时发出。

    data = {"node_id": str, "kind": str, "iteration_index": int,
            "history_len": int, "target_id": str | None}
    """

    kind: Literal["rewind_checkpoint_recorded"] = "rewind_checkpoint_recorded"


class TurnRewound(_Msg):
    """一次 Rewind Op 成功回退到某节点并重推时发出。

    data = {"node_id": str, "node_kind": str, "mode": str,
            "cut_index": int, "cache_anchor": int}
    """

    kind: Literal["turn_rewound"] = "turn_rewound"


class RewindRejected(_Msg):
    """一次 Rewind Op 校验失败被拒时发出（禁 silent fallback）。

    data = {"node_id": str, "reason": str}
    reason ∈ {unknown_node, no_rewindable_turn, mode_kind_mismatch, turn_suspended}
    """

    kind: Literal["rewind_rejected"] = "rewind_rejected"


class RewindTableRebuilt(_Msg):
    """冷加载从逻辑 history 重建 rewind 节点表后发出（R3 可观测）。

    engine.__init__ 接收 initial_history 后调用 reconstruct_logical_history +
    derive_rewind_log 重建节点表；pool resume 路径在 _rebuild_spawn_state_from_history
    之后调用 _emit_rewind_table_rebuilt 发出本事件。

    data: {"thread_id": str, "turn_count": int, "node_count": int}
    - thread_id: 当前 thread 的唯一标识
    - turn_count: 重建后 history 中累积 user_message 数（= 已跑 turn 数）
    - node_count: 重建后节点表条目数
    """

    kind: Literal["rewind_table_rebuilt"] = "rewind_table_rebuilt"


# ── detached-spawn：分离式 skill spawn + join-barrier 生命周期事件（R3 可观测）──


class SpawnStarted(_Msg):
    """data = {handle_id, skill_id, child_thread_id}"""

    kind: Literal["spawn_started"] = "spawn_started"


class SpawnSuspended(_Msg):
    """data = {handle_id, thread_id, pending}(= 该 child thread 的挂起)"""

    kind: Literal["spawn_suspended"] = "spawn_suspended"


class SpawnCompleted(_Msg):
    """data = {handle_id, result}"""

    kind: Literal["spawn_completed"] = "spawn_completed"


class SpawnFailed(_Msg):
    """data = {handle_id, error}"""

    kind: Literal["spawn_failed"] = "spawn_failed"


class SpawnCancelled(_Msg):
    """data = {handle_id}"""

    kind: Literal["spawn_cancelled"] = "spawn_cancelled"


class JoinBarrierRegistered(_Msg):
    """data = {barrier_id, handle_ids, then_skill_id}"""

    kind: Literal["join_barrier_registered"] = "join_barrier_registered"


class JoinBarrierFired(_Msg):
    """data = {barrier_id, then_thread_id}"""

    kind: Literal["join_barrier_fired"] = "join_barrier_fired"


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
    SkillSpawnRejected,
    ResourceLimitExceeded,
    CompactionStarted,
    CompactionCompleted,
    CompactionDegradationWarning,
    CompactionIntegrityRolledBack,
    ContextBudgetExceeded,
    CacheBreakDetected,
    PermissionPromptTimeout,
    SkillDispatchHookDenied,
    SkillDispatchPermissionDenied,
    PreTurnHookDenied,
    PreCompactHookSkipped,
    TurnSuspended,
    SuspensionResolved,
    SuspensionResolveRejected,
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
]


class EventMsg(BaseModel):
    submission_id: str
    msg: Msg = Field(discriminator="kind")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
