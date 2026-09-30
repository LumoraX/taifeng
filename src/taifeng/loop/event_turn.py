"""EventMsg 的 turn 内事件：采样、工具、skill、上下文、压缩、provider、注入、hook、peer。

从 ``loop/event.py`` 原样搬出（W7.1 拆文件，零行为变更）；``MsgKind`` / ``Msg`` 联合与 ``EventMsg``
仍在 ``event.py``，所有事件类经 ``event.py`` 原名再导出。
"""

from __future__ import annotations

from typing import Literal

from taifeng.loop.event_base import _Msg


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


class SkillSelectionRouted(_Msg):
    """相位 3 验证：search_skills 按选择置信度给候选分流时打点（skill-selection-gate）。

    data = {"proceed": int, "trial": int, "escalate": int, "routes": dict[str, str]}
    - proceed / trial / escalate: 各档候选数
    - routes: skill_id → 分流结论
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_selection_routed"] = "skill_selection_routed"


class SkillSelectionGated(_Msg):
    """相位 3 验证：call_skill 派发一个经发现选中的 skill 时，分流门的裁决。

    data = {"skill_id": str, "call_id": str, "route": str, "confidence": float | None,
            "admitted": bool, "basis": str}
    - basis: ``route_proceed`` / ``read_skill`` / ``trial_judge``（放行）；
      ``needs_trial`` / ``trial_rejected`` / ``low_confidence``（拦下）
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_selection_gated"] = "skill_selection_gated"


class SkillAuthorizationGranted(_Msg):
    """相位 4 准入：一次白名单外派发获得授权（skill-authorization，ADR 0089）。

    data = {"caller_skill_id": str, "target_skill_id": str, "call_id": str,
            "origin": "call_skill", "reason": str, "request_reason": str,
            "call_chain": list[str]}
    - reason: 授权策略给出的依据；request_reason: 模型自陈的派发理由
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_authorization_granted"] = "skill_authorization_granted"


class SkillAuthorizationDenied(_Msg):
    """相位 4 准入：一次白名单外派发被授权策略拒绝。data 同 ``SkillAuthorizationGranted``。"""

    kind: Literal["skill_authorization_denied"] = "skill_authorization_denied"


class ContextAssembled(_Msg):
    """ContextEngine 为一次采样装配了不同于完整 history 的视图（ADR 0093）。

    data = {"engine": str, "history_items": int, "view_items": int, "view_tokens": int,
            "cache_invalidated": bool, "anchor_preserved_until": int, "detail": dict[str, int]}
    同一 history 版本只发一次。引擎决定原样发送完整 history 时不发。
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["context_assembled"] = "context_assembled"


class PrewarmStarted(_Msg):
    """预热开始（prewarm，ADR 0092）。data = {"steps": list[str]}"""

    kind: Literal["prewarm_started"] = "prewarm_started"


class PrewarmCompleted(_Msg):
    """预热结束：无论成功、失败还是被取消都发这一条。

    data = {"steps": dict[str, str], "errors": dict[str, str], "cancelled": bool,
            "duration_ms": int, "usage": dict | None}
    - steps: 步骤 → 结果。``instructions``: resolved / skipped；``working_set``: restored /
      skipped；``model``: primed / unsupported / skipped；任一步还可能是 failed / cancelled
    - errors: 失败步骤 → 错误说明
    - cancelled: 是否被取消（用户消息到达或显式 CancelTurn）
    - usage: 模型侧预热消耗的 token；没有消耗为 None
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["prewarm_completed"] = "prewarm_completed"


class SkillPromoted(_Msg):
    """相位 5 沉淀：skill 按战绩被提拔进工作集（skill-working-set，ADR 0090）。

    data = {"skill_id": str, "score": float | None, "success_rate": float | None,
            "decided_samples": int | None, "trust_tier": str | None,
            "trigger_call_id": str | None}
    - score / success_rate / decided_samples: 变更时的战绩；已无战绩记录时为 None
    - trigger_call_id: 触发这次重算的执行；启动时重算产生的变更为 None
    本事件不进 LLM 视图，仅供 TelemetrySink / 审计消费。
    """

    kind: Literal["skill_promoted"] = "skill_promoted"


class SkillEvicted(_Msg):
    """相位 5 沉淀：skill 被逐出工作集（超预算被挤掉、掉线或被隔离）。data 同 ``SkillPromoted``。"""

    kind: Literal["skill_evicted"] = "skill_evicted"


class SkillQuarantined(_Msg):
    """相位 5 沉淀：skill 被隔离——选中得多、成功得少，描述多半过度承诺。

    data 同 ``SkillPromoted``。运维据此去修或删该 skill；战绩重置或好转后自动解除。
    """

    kind: Literal["skill_quarantined"] = "skill_quarantined"


class SkillReleased(_Msg):
    """相位 5 沉淀：skill 解除隔离。data 同 ``SkillPromoted``。"""

    kind: Literal["skill_released"] = "skill_released"


class SkillSpawnRejected(_Msg):
    """子 skill 派发 / 分离发起被准入拒绝（K1 配额与 detached spawn 的结构性门控）。

    data = {"skill_id": str, "call_id": str, "reason": SpawnRejectReason,
            "origin": "call_skill" | "spawn_skill", "path": list[str],
            "limit_kind"?: str, "limit"?: int}
    - reason: 稳定分类（``loop/spawn.py::SpawnRejectReason``）
    - origin: 发起拒绝的工具
    - path: 裁决时的调用路径；无路径信息为空列表
    - limit_kind / limit: 仅配额拒绝（``spawn_limit_*``）携带
    """

    kind: Literal["skill_spawn_rejected"] = "skill_spawn_rejected"


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
    """provider 采样失败后的**有界自愈 / 重试**动作（R3 关键路径事件）。

    两类来源共用，按 ``reason`` 区分：

    - ``reason="context_overflow"``（A1 reactive-compaction-recovery）：provider 以「上下文超长」
      拒绝采样 → 强制压缩 + 重采样一次；紧随其后是一对 phase=overflow 的
      compaction_started / compaction_completed
      （承载 compaction_attempted(trigger=context_overflow) 语义）。
    - ``reason ∈ RetryConfig.retryable_kinds``（``transient_network`` / ``rate_limit`` /
      ``server_error`` …）：
      ``RetryingModelClient`` 在零产出 attempt 失败后退避重试（ADR 0039）；事件在退避**之前** emit。
    """

    kind: Literal["provider_retry"] = "provider_retry"
    """data = {"reason": str, "iteration": int}；网络重试另带 {"attempt", "max_attempts",
    "delay_seconds", "failure_class", "error_kind", "transport_phase", "retry_after_seconds"}
    （字段含义见 ``llm/retrying.RetryAttempt``）。"""


class ProviderCircuitOpened(_Msg):
    """provider 断路器跳闸：连续 N 次「重试耗尽」的最终失败 → 此后快速失败，不再触网（ADR 0042）。

    与 ``denial_circuit_open`` 的分工：那个计的是 turn 内权限/hook 连续拒绝；
    这个计的是**跨 turn** 的 provider 健康度，作用域是一个 endpoint。
    """

    kind: Literal["provider_circuit_opened"] = "provider_circuit_opened"
    """data = {"from_state": str, "to_state": str, "consecutive_failures": int,
    "cooldown_seconds": float, "last_failure_class": str | None,
    "last_error_kind": str | None}（字段含义见 ``llm/breaker.CircuitTransition``）。"""


class ProviderCircuitHalfOpen(_Msg):
    """冷却到期转半开：放行恰一个探测请求，其余并发请求继续快速失败。"""

    kind: Literal["provider_circuit_half_open"] = "provider_circuit_half_open"
    """data 同 ``provider_circuit_opened``。"""


class ProviderCircuitClosed(_Msg):
    """半开探测成功 → 闭合：上游恢复，失败计数与冷却清零。"""

    kind: Literal["provider_circuit_closed"] = "provider_circuit_closed"
    """data 同 ``provider_circuit_opened``。"""


class ToolSetChanged(_Msg):
    """工具注册表发生增 / 删 / 同名替换（dynamic-tool-set）。

    来源：业务侧 ``ToolRegistry.register`` / ``unregister`` / ``replace``，或 MCP server
    ``tools/list_changed`` 触发的重新同步。变更在各 engine 的**下一次采样**生效；
    随之的 cache 失效归因为 ``tool_spec_changed``（预期内）。
    """

    kind: Literal["tool_set_changed"] = "tool_set_changed"
    """data = {"added": list[str], "removed": list[str], "replaced": list[str], "version": int}"""


class BackgroundTaskCompleted(_Msg):
    """``run_in_background`` 发起的后台任务结束（background-completion-wake）。

    事件发在发起该任务的 engine 上；完成摘要同时投递到发起任务的 thread：运行中的
    turn 在下一迭代边界看到；空闲的 spawn 子 thread 被唤醒续跑；根 thread 只落史
    （根 turn 由宿主驱动——宿主可据本事件决定是否提交新 turn）。
    """

    kind: Literal["background_task_completed"] = "background_task_completed"
    """data = {"task_id": str, "thread_id": str（发起 thread）, "delivered_to": str（实际投递 thread）,
              "exit_code": int | None, "killed": bool, "delivered_via": str, "woken": bool}"""


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


class SubmissionQueued(_Msg):
    """根 turn 串行（ADR 0029）：submission 因 root gate 被占用而排队。

    ``waiting_on`` 是当前持有 gate 的 submission id；排队按提交序 FIFO，前者真终态
    （含 post_turn hook）后本 submission 才开始。排队中可被 CancelTurn 取消。
    """

    kind: Literal["submission_queued"] = "submission_queued"
    """data = {"submission_id": str, "waiting_on": str | None}"""


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


class CompactionDeferred(_Msg):
    """压缩增量基线推迟了本轮压缩（ADR 0083）—— history / cache_anchor 保持不变。

    上次压缩后上下文没长多少：再压一次只会破坏缓存、对摘要再做摘要，却腾不出空间。
    本事件与 ``compaction_started`` 互斥（同一次 ``_maybe_compress`` 调用内）。

    data = {"phase": str, "reason": "below_growth_baseline", "token_estimate": int,
            "baseline_tokens": int, "required_tokens": int}
    """

    kind: Literal["compaction_deferred"] = "compaction_deferred"


class OutboundMessage(_Msg):
    """root turn 的最终回答，已经过 ``outbound_message`` hook 归一（ADR 0086）。

    仅在注册了 ``outbound_message`` handler 时发出，先于本 turn 的 ``turn_completed``。
    history 里模型的原话不受改写影响。

    data = {"text": str, "rewritten": bool, "raw_chars": int, "end_reason": str,
            "success": bool, "thread_id": str}
    - text: 归一后的出站文本
    - rewritten: 与模型原话是否不同
    - raw_chars: 模型原话的字符数
    """

    kind: Literal["outbound_message"] = "outbound_message"


class SubagentPolicyOverridden(_Msg):
    """G3 subagent-isolation-policy: 子 turn 派发时 PermissionPolicy 被包装。

    business 侧可订阅本事件审计哪些子 skill 走了 auto_deny / auto_allow，与
    inherit 模式（无事件）区分。emit 时机：``TurnRunner.run_sub_skill`` 创建
    ``_SubagentAutoDecisionPolicy`` 包装时，**SkillDispatched 之后、子 turn 启动
    之前**。inherit 模式不 emit。

    data = {"target_skill_id": str, "mode": "auto_deny|auto_allow", "depth": int}
    """

    kind: Literal["subagent_policy_overridden"] = "subagent_policy_overridden"


__all__ = [
    "AssistantReasoning",
    "AssistantText",
    "BackgroundTaskCompleted",
    "BudgetHintInjected",
    "CacheBreakDetected",
    "CompactionCompleted",
    "CompactionDeferred",
    "CompactionDegradationWarning",
    "CompactionIntegrityRolledBack",
    "CompactionStarted",
    "ContextAssembled",
    "ContextBudgetExceeded",
    "DenialCircuitOpen",
    "DoomLoopCircuitOpen",
    "DoomLoopWarned",
    "LlmRequestRecorded",
    "OrchestrationConditionMissing",
    "OrchestrationPlanResolved",
    "OutboundMessage",
    "PeerAgentWoken",
    "PeerMessageSent",
    "PeerWaitAnyResolved",
    "PeerWaitAnyStarted",
    "PeerWaitResolved",
    "PeerWaitStarted",
    "PermissionPromptTimeout",
    "PinnedStateReinjected",
    "PostTurnHookFired",
    "PreCompactHookSkipped",
    "PreTurnHookDenied",
    "PrewarmCompleted",
    "PrewarmStarted",
    "ProviderCircuitClosed",
    "ProviderCircuitHalfOpen",
    "ProviderCircuitOpened",
    "ProviderRetry",
    "ResourceLimitExceeded",
    "SkillAuthorizationDenied",
    "SkillAuthorizationGranted",
    "SkillCandidatesReturned",
    "SkillCandidatesVerified",
    "SkillDispatchHookDenied",
    "SkillDispatchPermissionDenied",
    "SkillDispatched",
    "SkillEvicted",
    "SkillOutcomeRecorded",
    "SkillPromoted",
    "SkillQuarantined",
    "SkillReleased",
    "SkillReturned",
    "SkillSearchInvoked",
    "SkillSelectionGated",
    "SkillSelectionRouted",
    "SkillSpawnRejected",
    "SubagentPolicyOverridden",
    "SubmissionQueued",
    "SystemMessageInjected",
    "ToolBatchDispatched",
    "ToolCallCompleted",
    "ToolCallStarted",
    "ToolSetChanged",
    "TurnStarted",
    "UserInputInjected",
]
