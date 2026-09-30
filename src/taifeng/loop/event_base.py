"""EventMsg 的 kind 枚举与消息基类（从 ``loop/event.py`` 原样搬出，W7.1 拆文件，零行为变更）。

新增事件：在 ``MsgKind`` 加 kind，在 ``event_turn.py`` / ``event_lifecycle.py`` 定义类，在 ``event.py`` 的
``Msg`` 联合登记。
"""

from __future__ import annotations

from typing import Any, Literal

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
    "skill_spawn_rejected",
    "resource_limit_exceeded",
    "compaction_started",
    "compaction_completed",
    "compaction_degradation_warning",
    "compaction_integrity_rolled_back",
    "context_budget_exceeded",
    "pinned_state_reinjected",
    "budget_hint_injected",
    "cache_break_detected",
    "provider_retry",
    "provider_circuit_opened",
    "provider_circuit_half_open",
    "provider_circuit_closed",
    "tool_set_changed",
    "background_task_completed",
    "llm_request_recorded",
    "user_input_injected",
    "system_message_injected",
    "submission_queued",
    "permission_prompt_timeout",
    "skill_dispatch_hook_denied",
    "skill_dispatch_permission_denied",
    "pre_turn_hook_denied",
    "post_turn_hook_fired",
    "pre_compact_hook_skipped",
    "compaction_deferred",
    "outbound_message",
    "skill_selection_routed",
    "skill_selection_gated",
    "skill_authorization_granted",
    "skill_authorization_denied",
    "context_assembled",
    "prewarm_started",
    "prewarm_completed",
    "skill_promoted",
    "skill_evicted",
    "skill_quarantined",
    "skill_released",
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
    "suspension_partially_resolved",
    "suspension_resolve_rejected",
    "suspension_expired",
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
    "peer_message_sent",
    "peer_agent_woken",
    "peer_wait_started",
    "peer_wait_resolved",
    "peer_wait_any_started",
    "peer_wait_any_resolved",
    "denial_circuit_open",
    "doom_loop_warned",
    "doom_loop_circuit_open",
    "skill_outcome_recorded",
    # 相位 2：skill 发现/召回（search_skills 工具打点，R3 可观测）
    "skill_search_invoked",
    "skill_candidates_returned",
    "skill_candidates_verified",
]


class _Msg(BaseModel):
    kind: MsgKind
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = ["MsgKind", "_Msg"]
