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
    "llm_request_recorded",
    "user_input_injected",
    "system_message_injected",
    "permission_prompt_timeout",
    "skill_dispatch_hook_denied",
    "skill_dispatch_permission_denied",
    "pre_turn_hook_denied",
    "post_turn_hook_fired",
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


class SkillOutcomeRecorded(_Msg):
    """K-沉淀：一次 skill 执行落了战绩记录（认知回路 ⑦ 地基）。

    data = SkillExecutionRecord.as_payload()（含 skill_id / call_id / outcome /
    outcome_signal_source / selection_origin / cost_* / end_reason 等）。
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_outcome_recorded"] = "skill_outcome_recorded"


class SkillSearchInvoked(_Msg):
    """相位 2 召回：search_skills 工具发起一次 skill 候选检索时打点（认知回路：发现）。

    data = {"query": str, "top_k": int, "pool_size": int}
    - query: 本次检索的查询文本
    - top_k: 请求返回的候选上限
    - pool_size: 被检索的 skill 池规模（可见候选总数）
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_search_invoked"] = "skill_search_invoked"


class SkillCandidatesReturned(_Msg):
    """相位 2 召回：search_skills 返回候选 skill 列表时打点（认知回路：召回结果）。

    data = {"count": int, "top_ids": list[str]}
    - count: 实际返回的候选数量
    - top_ids: 候选 skill_id 列表（按相关度排序）
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_candidates_returned"] = "skill_candidates_returned"


class SkillCandidatesVerified(_Msg):
    """相位 2 验证：search_skills 召回后做完输入要求适配精验时打点（认知回路：精验）。

    data = {"verified_count": int, "dropped_count": int}
    - verified_count: 验证通过（applicable=True）保留的候选数
    - dropped_count: 被验证滤掉的候选数（= 召回数 - 验证通过数；含「描述像但输入要求不满足」与无 body 的误召）
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_candidates_verified"] = "skill_candidates_verified"


class SkillSpawnRejected(_Msg):
    """K1：子 skill 派发因 spawn 配额超限被拒（fork-bomb 防护）。"""

    kind: Literal["skill_spawn_rejected"] = "skill_spawn_rejected"
    """data = {"skill_id": str, "call_id": str, "limit_kind": str, "limit": int}"""


class ResourceLimitExceeded(_Msg):
    """K2：累计资源（token）触顶 → turn 被强制中止（OOM-killer）。"""

    kind: Literal["resource_limit_exceeded"] = "resource_limit_exceeded"
    """data = {"limit_kind": str, "used": int, "limit": int, "scope": str}"""


class DenialCircuitOpen(_Msg):
    """turn 内 permission/hook 连续拒绝越阈值 → 断路器触发，turn 提前终止。

    data = {"consecutive": int, "recent": int, "window_size": int,
            "last_denied_target": str}（target 仅名字，不带 args 正文）
    """

    kind: Literal["denial_circuit_open"] = "denial_circuit_open"


class DoomLoopWarned(_Msg):
    """turn 内连续 N 次同 (tool,args) 成功调用空转 → 先警：注中性事实让模型自改。

    data = {"tool": str, "consecutive": int, "threshold": int}
    （仅工具名 + 计数，不带 args 正文 —— PII 约束）
    """

    kind: Literal["doom_loop_warned"] = "doom_loop_warned"


class DoomLoopCircuitOpen(_Msg):
    """警告后仍连续重复到 2N 次 → 断路器触发，turn 在迭代边界提前终止。

    data = {"tool": str, "consecutive": int, "threshold": int}
    """

    kind: Literal["doom_loop_circuit_open"] = "doom_loop_circuit_open"


class CompactionStarted(_Msg):
    kind: Literal["compaction_started"] = "compaction_started"
    """data = {"phase": str, "strategy": str, "token_estimate": int}"""


class CompactionCompleted(_Msg):
    kind: Literal["compaction_completed"] = "compaction_completed"
    """data = {"success": bool, "cache_invalidated": bool, "removed_count": int, "reason": str|None}"""


class PinnedStateReinjected(_Msg):
    """压缩成功后 agent-owned 状态钉回 history tail(postcompact re-injection)。

    data = {"sources": [{"name": str, "chars": int}], "total_chars": int,
            "dropped": [str], "phase": str}
    不带渲染正文(PII 约束);dropped = 总预算装不下被整体跳过的 source 名。
    无 source 注册或全部渲染 None 时不 emit(零噪声)。
    """

    kind: Literal["pinned_state_reinjected"] = "pinned_state_reinjected"


class BudgetHintInjected(_Msg):
    """上下文用量穿越 soft_limit，pre-turn 注一条中性预算事实（budget-awareness）。

    ADR 0017 规则②原语：让模型自知剩余预算从而主动收敛。穿越一次注一次
    （回落复位）。不带渲染正文外的产品意见。

    data = {"used": int, "context_window": int, "ratio": float,
            "remaining_to_hard": int}
    ratio = used / context_window（保留两位）；remaining_to_hard = hard_limit - used。
    """

    kind: Literal["budget_hint_injected"] = "budget_hint_injected"


class PeerMessageSent(_Msg):
    """peer 点对点消息已投递(送达路径与降级如实标注;不带正文,仅长度+预览)。

    data = {"from": str, "to": str, "mode": str, "delivered_via":
            "pending_input"|"history", "mode_downgraded": bool,
            "text_len": int, "text_preview": str}
    """

    kind: Literal["peer_message_sent"] = "peer_message_sent"


class PeerAgentWoken(_Msg):
    """TriggerTurn 唤醒了一个空闲 spawn child(新 detached turn 已启动)。

    data = {"thread_id": str, "handle_id": str}
    """

    kind: Literal["peer_agent_woken"] = "peer_agent_woken"


class PeerWaitStarted(_Msg):
    """wait_peer 开始阻塞等待句柄终态。data = {"handle_id", "timeout_seconds"}"""

    kind: Literal["peer_wait_started"] = "peer_wait_started"


class PeerWaitResolved(_Msg):
    """wait_peer 等待结束。

    data = {"handle_id", "outcome": "terminal"|"timeout"|"cancelled", "status"}
    """

    kind: Literal["peer_wait_resolved"] = "peer_wait_resolved"


class PeerWaitAnyStarted(_Msg):
    """wait_any 开始 any-of-N 等待。data = {"handle_ids": list[str], "timeout_seconds"}

    与 PeerWaitStarted 分开而非复用:data 形状不同(单个 handle_id vs 句柄集),
    复用会逼订阅方按字段存在性做分支判断,损害归因性(R3)。
    """

    kind: Literal["peer_wait_any_started"] = "peer_wait_any_started"


class PeerWaitAnyResolved(_Msg):
    """wait_any 等待结束。

    data = {"settled_ids": list[str], "pending_ids": list[str],
            "outcome": "terminal"|"timeout"}
    正文(各句柄 result)不入事件 data——与 peer 事件族一致,只带可归因的形状信息。
    """

    kind: Literal["peer_wait_any_resolved"] = "peer_wait_any_resolved"


class CacheBreakDetected(_Msg):
    kind: Literal["cache_break_detected"] = "cache_break_detected"
    """data = {"unexpected": bool, "reason": str, "token_drop": int}"""


class ProviderRetry(_Msg):
    """A1：provider 以「上下文超长」拒绝采样 → 触发有界自愈（强制压缩 + 重采样）。

    紧随其后会出现一对 phase=overflow 的 compaction_started / compaction_completed
    （承载 compaction_attempted(trigger=context_overflow) 语义），以及重采样事件。
    """

    kind: Literal["provider_retry"] = "provider_retry"
    """data = {"reason": str, "iteration": int}；reason 当前取值 context_overflow。"""


class LlmRequestRecorded(_Msg):
    """审计可观测 层1：单次实际发往 provider 的 request 留痕。

    在 ``turn.py`` build_api_request 之后、发送 provider 之前 emit（即便后续超时/
    失败，request 仍留痕）；受 ``enable_request_capture`` 全局开关控制（默认关，零
    泄漏面）。每次实际构建发送的 request 各一条（retry/压缩重建是新一轮构建 → 新一
    条），与 ``provider_retry`` 交错可还原「哪版 request」。

    ⚠️ 含完整文字 prompt + conversation（敏感），但图片正文始终替换成结构描述：
    OtelSink **按 kind 整条跳过**不外发；可靠落盘 / 访问控制 / 保留期仍归业务消费者。
    """

    kind: Literal["llm_request_recorded"] = "llm_request_recorded"
    """data = 图片正文脱敏后的 ApiRequest JSON（model / prompts / messages / tools ...）。"""


class UserInputInjected(_Msg):
    """B1：InjectUserInput 投递结果。

    delivered=true → 投进活跃 turn 的 pending 队列（下一迭代边界并入 prompt）；
    false → 无活跃 turn，文本落历史但未起新 turn（codex inject_no_new_turn）。
    """

    kind: Literal["user_input_injected"] = "user_input_injected"
    """data = {"submission_id": str, "delivered": bool, "text_preview": str,
    "reason": str | None}

    ``reason="turn_ended"``（delivered=false）：注入投进了活跃 turn 的 pending 队列，但
    turn 在消费前结束（取消 / 异常），文本由 engine 收尾落史、未进入该 turn 的 prompt。
    """


class SystemMessageInjected(_Msg):
    """InjectSystemMessage 投递结果（ADR 0029：在飞期间走 runner pending 队列）。

    delivered=true → 已并入活跃 turn 的 history（下一迭代可见）或无活跃 turn 时直接
    落史；false + reason="turn_ended" → 投进 pending 后 turn 结束，engine 收尾落史。
    """

    kind: Literal["system_message_injected"] = "system_message_injected"
    """data = {"submission_id": str, "delivered": bool, "text_preview": str,
    "reason": str | None}"""


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


class PostTurnHookFired(_Msg):
    """post_turn hook 在 root turn 真终态被触发 —— 收尾审计点(R3)。

    仅在 end_reason ∉ {suspended, cancelled} 且有注册 post_turn 钩子时 emit;
    审计型,不改变已终结的 turn。范式对齐 ``pre_turn_hook_denied``。
    """

    kind: Literal["post_turn_hook_fired"] = "post_turn_hook_fired"
    """data = {"end_reason": str, "iteration": int, "hook_count": int}"""


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


class SuspensionPartiallyResolved(_Msg):
    """多 pending record 的部分核销(multi-pending-partial-resume)。

    record 含多个 pending(如 parallel 批多子同时挂起)时,Resume 按 request 级
    核销:本事件表示本次只结算了一部分,record 仍活跃、父 turn 不续跑——直到
    全部 pending 核销才落整体 resolved-marker + emit suspension_resolved + 续跑。

    data = {"record_id": str, "thread_id": str,
            "resolved_request_ids": list[str], "remaining_request_ids": list[str]}
    """

    kind: Literal["suspension_partially_resolved"] = "suspension_partially_resolved"


class SuspensionResolveRejected(_Msg):
    """Resume 被拒（resolution 不全 / 多余、record 已消费、payload 不符 schema 等）。

    data = {"reason": str, "record_id": str | None, "detail": dict}
    """

    kind: Literal["suspension_resolve_rejected"] = "suspension_resolve_rejected"


class SuspensionExpired(_Msg):
    """挂起 record 到期，内核自动裁决（suspension-ttl，R3 可观测）。

    随后的续跑 / 终态沿用既有 resume 事件族（suspension_resolved / turn_* /
    spawn_*），record_id 贯通可归因「挂起为何自行动作」。

    data = {"record_id": str, "thread_id": str, "on_expire": str,
            "reasons": list[str]}   # 该 record 各 pending 的 reason
    """

    kind: Literal["suspension_expired"] = "suspension_expired"


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
        "recovered_unknown_call_ids": list[str],
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
    """data = {handle_id, thread_id, record_id, pending}(= 该 child thread 的挂起)。

    ``record_id`` 与 ``turn_suspended`` 同源(子 thread 落盘挂起 record 的幂等键):
    消费方按 (handle_id, record_id) 去重 / 分轮 —— 首挂与每次二次挂起(Resume 续跑
    后再挂)各带不同 record_id(新挂起点 = 新 record);同一 record_id 重放(冷恢复 /
    部分核销后仍挂)视作同一逻辑挂起。子 thread 无挂起 record 的边界下为 None。
    """

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
    PermissionPromptTimeout,
    SkillDispatchHookDenied,
    SkillDispatchPermissionDenied,
    PreTurnHookDenied,
    PostTurnHookFired,
    PreCompactHookSkipped,
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
