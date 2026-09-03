"""TurnRunner —— 单 turn 执行：采样 → 处理事件 → 工具调度 → mid-turn 压缩 → 决定是否继续。

参照：codex codex-rs/core/src/session/turn.rs::run_turn
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from taifeng.context.budget import (
    ContextBudget,
    estimate_history_bytes,
    estimate_history_tokens,
)
from taifeng.context.budget_hint import evaluate_budget_hint, render_budget_hint
from taifeng.context.cache_stats import PromptCacheStats
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    reasoning,
)
from taifeng.conversation.store import AtomicBatchMessageStore
from taifeng.llm.client import model_capabilities
from taifeng.llm.errors import (
    AttachmentTooLargeError,
    ImageCountExceededError,
    InvalidImageError,
    InvalidResponseError,
    LLMError,
    RequestTooLargeError,
    UnsupportedModalityError,
    UnsupportedPersistenceCapabilityError,
    classify_failure,
)
from taifeng.llm.image_input import (
    DISABLED_IMAGE_POLICY,
    ImageInputPolicy,
    InputCostEstimator,
    admit_tool_attachments,
    redact_sensitive_request_data,
)
from taifeng.llm.providers.openai._shared import MAX_REQUEST_BYTES_METADATA_KEY
from taifeng.llm.recovery import recommend_recovery
from taifeng.llm.responses_types import (
    NormalizedFunctionCallItem,
    NormalizedMessageItem,
    NormalizedOutputItem,
    NormalizedReasoningItem,
)
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit_llm import (
    commit_audited_llm_response,
    model_session_for_turn,
    record_model_cache_read,
)
from taifeng.loop.audit_skill import AuditedSkillDispatch
from taifeng.loop.audit_tool import audited_tool_batch
from taifeng.loop.denial_breaker import DenialBreaker, DenialBreakerConfig
from taifeng.loop.doom_loop import DoomLoopConfig, DoomLoopDetector
from taifeng.loop.event import (
    AssistantReasoning,
    AssistantText,
    BudgetHintInjected,
    CacheBreakDetected,
    CompactionCompleted,
    CompactionDegradationWarning,
    CompactionIntegrityRolledBack,
    CompactionStarted,
    ContextBudgetExceeded,
    DenialCircuitOpen,
    DoomLoopCircuitOpen,
    DoomLoopWarned,
    EngineLog,
    EventMsg,
    LlmRequestRecorded,
    PinnedStateReinjected,
    PreCompactHookSkipped,
    ProviderRetry,
    ResourceLimitExceeded,
    RewindCheckpointRecorded,
    SkillDispatched,
    SkillReturned,
    SkillSpawnRejected,
    SubagentPolicyOverridden,
    ToolBatchDispatched,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    TurnSuspended,
)
from taifeng.loop.failure_policy import (
    DEFAULT_FAILURE_POLICY,
    FailureContext,
    FailureDisposition,
    FailureDispositionPolicy,
)
from taifeng.loop.injection import injection_event
from taifeng.loop.iteration_budget import IterationBudget
from taifeng.loop.prompt import build_api_request
from taifeng.loop.rewind import RewindLog, count_turns
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch
from taifeng.skill.dispatch import CallStack, DispatchPolicy
from taifeng.suspend.signal import SuspendSignal  # 运行时 except 捕获，不可放 TYPE_CHECKING
from taifeng.tool.spec import ToolContext, ToolResult

if TYPE_CHECKING:
    from taifeng.context.pinned_state import PinnedStateRegistry
    from taifeng.conversation.store import MessageStore
    from taifeng.llm.client import ModelClient
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.eligibility import RuntimeCapabilities
    from taifeng.skill.registry import SkillSnapshot
    from taifeng.tool.runtime import ToolCallRuntime

logger = logging.getLogger(__name__)


DEFAULT_MAX_INNER_ITERATIONS = 32  # 单 turn 内 LLM ↔ tool 循环上限（默认值）
# 兼容旧 import
MAX_INNER_ITERATIONS = DEFAULT_MAX_INNER_ITERATIONS


def _sha1_short(text: str) -> str:
    """短 sha1（16 hex）—— 用于 prompt 结构指纹，仅做相等性比较。"""
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _history_orphan_call_ids(history: list[ResponseItem]) -> set[str]:
    """返回未配对的 function_call / function_call_output 的 call_id 集合（孤儿）。

    function_call 与 function_call_output 应一一对应（同 call_id）。对称差即
    未配对项；compacted 摘要吃掉成对段属正常（两侧同时消失，不算孤儿）。
    """
    fc = {
        h.payload.get("call_id") for h in history if h.kind == "function_call"
    }
    fco = {
        h.payload.get("call_id")
        for h in history
        if h.kind == "function_call_output"
    }
    return {cid for cid in (fc ^ fco) if cid is not None}


def _latest_user_text(history: list[ResponseItem]) -> str:
    """取 history 中最近一条 user_message 的文本（无则空串）。

    用途：① 长期记忆 prefetch 的默认检索 query；② 注入 ToolContext 的 ``current_task``，
    供 ``search_skills`` 把「原始任务」（含输入上下文）喂给验证门——验证要判「当前任务给
    的输入是否满足该 skill 的输入要求」，必须看到用户原话，不能用为词面匹配优化、剥离了
    输入上下文的关键词 query（详情五根因）。

    Args:
        history: 当前 in-memory 历史视图。

    Returns:
        最近一条用户消息的文本；history 中无 user_message 时返回空串。
    """
    for it in reversed(history):
        if it.kind == "user_message":
            return str(it.payload.get("text", ""))
    return ""


def _llm_failure_context(
    err: Exception, *, is_root: bool, iteration: int
) -> FailureContext:
    """从采样阶段的 LLMError 构造失败裁决上下文(origin=llm_error)。

    原 ``_should_suspend_on_error`` 的硬编码判据已收编进
    :class:`~taifeng.loop.failure_policy.ConservativeFailurePolicy`(内核默认,
    零行为变化);turn 层只负责构造上下文,裁决交注入的 policy。

    Args:
        err: 采样阶段重试耗尽后的异常(调用方保证为 LLMError)。
        is_root: 当前 runner 是否 root turn。
        iteration: 失败发生时的迭代序号(1-based)。

    Returns:
        供 FailureDispositionPolicy.decide 的不可变上下文。
    """
    return FailureContext(
        origin="llm_error",
        failure_class=getattr(err, "failure_class", None),
        end_reason=None,
        error_kind=type(err).__name__,
        retryable=bool(getattr(err, "retryable", False)),
        is_root=is_root,
        iteration=iteration,
    )


def _responses_sample_id(
    *, thread_id: str, submission_id: str, turn_index: int, iteration: int
) -> str:
    """构造跨热/冷恢复稳定的 logical Responses sample id。"""
    return f"{thread_id}:{submission_id}:turn:{turn_index}:llm:{iteration}"


def _responses_conversation_items(
    raw_items: list[dict[str, Any]],
    *,
    thread_id: str,
    model: str,
    sample_id: str,
) -> tuple[list[ResponseItem], str, list[dict[str, Any]]]:
    """把已验证 terminal normalized items 投影为 durable conversation items。"""
    from pydantic import TypeAdapter

    normalized = TypeAdapter(list[NormalizedOutputItem]).validate_python(raw_items)
    response_items: list[ResponseItem] = []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in normalized:
        metadata = {
            "llm_sample_id": sample_id,
            "provider_output_index": item.output_index,
        }
        if isinstance(item, NormalizedReasoningItem):
            payload: dict[str, Any] = {"text": "", "summary": item.visible_text}
            if item.state is not None:
                payload["provider_state"] = item.state.model_dump(mode="json")
            response_items.append(
                ResponseItem(
                    kind="reasoning",
                    thread_id=thread_id,
                    payload=payload,
                    metadata=metadata,
                )
            )
        elif isinstance(item, NormalizedMessageItem):
            text_parts.append(item.text)
            response_items.append(
                assistant_message(item.text, thread_id=thread_id, model=model).model_copy(
                    update={"metadata": metadata}
                )
            )
        elif isinstance(item, NormalizedFunctionCallItem):
            response_items.append(
                function_call(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=item.arguments,
                    thread_id=thread_id,
                ).model_copy(update={"metadata": metadata})
            )
            tool_calls.append(
                {
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments": item.arguments,
                    "origin_sample_id": sample_id,
                }
            )
    if not response_items:
        raise InvalidResponseError("Responses normalized output is empty")
    return response_items, "".join(text_parts), tool_calls


class _BatchSuspend(Exception):  # noqa: N818
    """内部:_dispatch_tools 把整批挂起 pending 上抛给 run_turn。

    挂起不是错误,只是 turn 的正常中断信号;run() 在宽 except 之前先捕获本异常,
    据此把结局退栈为 suspended(否则会被分类成 TurnFailed,即 Task 6 修过的吞错类问题)。
    """

    def __init__(self, pending: tuple[Any, ...]) -> None:
        """初始化批量挂起异常。

        Args:
            pending: 本 turn 全部挂起点的 PendingRequest(不可变 tuple)。
        """
        self.pending = pending
        super().__init__(f"batch suspend: {len(pending)} pending")


@dataclass(frozen=True)
class TurnOutcome:
    success: bool
    iterations: int
    duration_ms: int
    usage: TokenUsage
    final_text: str
    end_reason: str
    error: str | None = None
    suspension: Any = None
    """SuspensionRecord | None;end_reason=="suspended" 时非空(R5 跨进程 resume 的真相)。"""


@dataclass
class TurnRunner:
    """单 turn 执行器。"""

    entry_skill: SkillDefinition
    snapshot: SkillSnapshot
    model_client: ModelClient
    tool_runtime: ToolCallRuntime
    store: MessageStore
    compressors: CompressionOrchestrator | None
    dispatch_policy: DispatchPolicy
    budget: ContextBudget
    thread_id: str
    submission_id: str
    emit: Any  # Callable[[EventMsg], Awaitable[None]] —— 业务侧注入
    cancel: CancellationToken
    image_input_policy: ImageInputPolicy = DISABLED_IMAGE_POLICY
    input_cost_estimator: InputCostEstimator | None = None
    audit_state: AuditedSessionState | None = None
    """strict audit state；None 时保持 legacy ModelClient session 路径。"""
    hooks: Any = None  # HookRunner | None —— 可选钩子
    permission_policy: Any = None
    """可选 PermissionPolicy；非 None 时 call_skill 派发会经过 check()
    （详见 permission-gate-completeness/specs/skill-dispatch/spec.md）。"""
    request_metadata: dict[str, Any] = field(default_factory=dict)
    """业务侧透传的不透明上下文，原样合并进 PermissionRequest.metadata / 透传到
    HookContext.extras 与 InstructionContext.metadata（taifeng 不解析 keys）。"""
    turn_index: int = 0
    """父 turn 的迭代序号；用于 PermissionRequest.turn_index 透传。"""
    script_executors: dict[str, Any] = field(default_factory=dict)
    """``ScriptLanguage → ScriptExecutor`` 映射；run_script 工具按 descriptor.language
    在此查找执行器。业务侧通过 ``EnginePool(script_executors=...)`` 注入。"""
    # 当前 turn 的初始调用栈（默认仅 entry skill）
    call_stack: CallStack = field(default_factory=CallStack)
    # cache anchor —— 业务层在每轮后更新
    cache_anchor_index: int = -1
    # 单 turn 内最大循环（LLM ↔ tool 配对次数）；超过强制 max_iterations 结束
    max_iterations: int = DEFAULT_MAX_INNER_ITERATIONS
    # turn-resource-guards：迭代预算（None → run() 按 max_iterations 自建）。
    # run_sub_skill 派生子 turn 时传 budget.child()（独立实例，不回写父）。
    iteration_budget: IterationBudget | None = None
    # turn-resource-guards：denial 断路器配置（None=不启用，零行为变化）。
    # 实例为 turn 级生命周期（run() 新建、turn 结束即弃），见 denial_breaker.py。
    denial_breaker_config: DenialBreakerConfig | None = None
    # turn-resource-guards：doom-loop 检测配置（None=不启用）。重复同 (tool,args)
    # 成功调用空转的先警后断守卫，turn 级生命周期，见 doom_loop.py。
    doom_loop_config: DoomLoopConfig | None = None
    # failure-suspension-policy：失败处置裁决 policy（挂起 vs 终态）。
    # None → 模块默认 ConservativeFailurePolicy（复刻历史判据，零行为变化）；
    # 子 runner（call_skill / spawn）继承父实例。业务侧经 EnginePool 注入。
    failure_policy: FailureDispositionPolicy | None = None
    # suspension-ttl:内核自产挂起(SYSTEM_RETRY / RESOURCE_LIMIT)的存活期声明。
    # None(默认)= 永不过期;无人值守部署可配 ttl + on_expire="retry" 实现
    # 「限流/触顶到期自动续跑」。业务挂起(request_user_input)的 ttl 在工具工厂声明。
    failure_suspend_ttl_seconds: int | None = None
    failure_suspend_on_expire: Literal["abort", "retry"] = "abort"
    # resource-limit-retry-semantics:本次 run 来自第 N 次「TTL 到期自动 retry」
    # (engine 据 ResolvePlan.expired_retry 谱系递增注入;人工 Resume 不计数)。
    # 再次挂起时落进新 pending detail 供 fire 时与上限比对。
    auto_retry_count: int = 0
    # 单 turn 内一批 tool call 的最大并发数；默认 1 = 严格串行（等同历史行为，零回归）
    max_parallel_tool_calls: int = 1
    sample_scope_id: str | None = None
    """Responses 逻辑采样作用域；与对外事件的 submission 归因解耦。

    None 时沿用 ``submission_id``。detached child 的 Resume/Rewind 会保持事件归因
    为 child thread，同时注入本次操作 id，避免新采样复用旧原子批次标识。
    """
    # reasoning-content-passback:thinking 模型 reasoning 回传开关(prompt 重建时把
    # 落史的 reasoning item 附回相邻 assistant 消息)。默认开——回传天然自限:
    # history 无 reasoning item 即不回传,非 thinking 模型零变化。落史本身无旋钮
    # (R5 数据完整性;只在 provider 实际吐过 reasoning_delta 时才有内容)。
    reasoning_passback: bool = True
    # 审计可观测 层1:LLM request 全文留痕开关(默认关,零泄漏面)。开启后每次实际
    # 构建发送的 request 在发送 provider 前 emit 一条 LlmRequestRecorded。
    enable_request_capture: bool = False
    # turn-级累积 usage
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    history_buffer: list[ResponseItem] = field(default_factory=list)
    """In-memory 视图，与 store 同步追加。"""
    # budget-awareness（ADR 0017 规则②）：是否已对当前「超 soft episode」注过预算提示。
    # 穿越一次注一次；用量回落到 soft 以下时复位（见 _maybe_inject_budget_hint）。
    _budget_notified: bool = False

    # turn-rewind：本 turn 执行轨迹的回访节点侧录（只 root turn 的会回写 engine）
    rewind_log: RewindLog = field(default_factory=RewindLog)

    # T3: 已 resolve 的指令列表（engine.run_turn 前 resolve 好传入；按 priority 升序）
    instructions: list[Any] = field(default_factory=list)
    """list[ResolvedInstruction]；用 Any 避免循环 import 顶层负担。"""

    # G4a：业务注入的运行时能力快照；None=不做资格过滤（默认，行为不变）
    capabilities: RuntimeCapabilities | None = None
    # K1：spawn 配额（engine 注入，贯穿整棵 turn 树）；None=不限广度（默认，行为不变）
    spawn_registry: Any = None  # SpawnSlotRegistry | None
    # K2：会话级累计 token 上限（OOM-killer）。session_tokens_used 是本 turn 开始前的
    # 累计基线（engine 注入）；max_session_tokens=None → 不强制（默认，行为不变）。
    session_tokens_used: int = 0
    max_session_tokens: int | None = None
    # K3：长期记忆 swap 接口（engine 注入）；None=无内存层级（默认，行为不变）。
    memory_store: Any = None  # MemoryStore | None
    # memory-integration-ergonomics：prefetch 检索 query 的业务侧构造器
    # （同步 (history: list[ResponseItem]) -> str）。None=默认构造（最后一条
    # 用户消息文本）。builder 崩溃 → 记日志回退默认（best-effort 域）。
    memory_query_builder: Any = None
    # postcompact-state-reinjection：pinned 状态注册表（engine 注入共享实例）。
    # 压缩成功后按注册序把各 source 渲染结果以 system_injection 钉回 history 尾。
    # None=未启用（默认，零行为变化）。与 K3 正交：memory 是换出抢救，这里是钉回保活。
    pinned_states: PinnedStateRegistry | None = None
    # detached-spawn：spawn 协调器（engine 注入 self）；让 spawn_skill / await_skills /
    # join_skill / kill_skill 四工具经 ctx.extras['spawn_coordinator'] 拿到 engine 的
    # spawn API。None=无 engine 上下文（裸 TurnRunner 单测），工具返回 spawn_unavailable。
    spawn_coordinator: Any = None  # AgentEngine | None
    # 本 turn page-in 的记忆文本（run 开始 prefetch 一次，注入每轮 prompt 尾部）
    _prefetched_memory: str = ""

    # G1c：单一 thread 累计的成功压缩次数（Engine 注入持久值 → 跨 turn 累积）。
    # 达到 compaction_degradation_threshold 后每次压缩 emit 降级告警。
    compaction_count: int = 0
    compaction_degradation_threshold: int = 3
    # Cache 统计（跨多轮 LLM 调用累积）；Engine 注入持久实例 → 跨 turn 累积
    cache_stats: PromptCacheStats = field(default_factory=PromptCacheStats)
    # prompt 结构指纹：既作为「上一轮」种子传入（Engine 注入），run 结束后由
    # Engine 读回作为下一轮的种子，从而跨 turn 归因 cache 失效的结构性原因。
    last_prompt_fingerprint: dict[str, str] | None = None
    # 标记下一次 LLM 调用是否预期会破坏 cache（最近一次 pre_turn/manual 压缩后置为 True）
    _next_cache_break_expected: bool = False
    _next_cache_break_reason: str | None = None
    # G3：最近一次 LLM 调用回流的服务端 request-id（completed 事件携带）
    _last_request_id: str | None = None
    # turn-rewind：本 runner 是否 root turn（run() 入口判定后写入，门控节点记录/发事件）
    _is_root: bool = False
    # turn-rewind retry_tool：非 None 时,采样前先补跑该悬空 call(末尾留 fc、无 fco)
    _seed_pending_call_id: str | None = None
    # A1 reactive-compaction-recovery：本 turn 是否已发生过 overflow 自愈（有界一次）。
    # run() 入口 reset，防实例复用残留。二次 overflow 即硬失败。
    _overflow_recovered: bool = False
    # B1 midturn-input-steering：与 engine._PendingTurn 共享的注入队列。engine 处理
    # InjectUserInput Op 时 append，run() 迭代边界 _drain_pending_input 并入 history。
    pending_input: list[ResponseItem] = field(default_factory=list)

    # 挂起 id / 时间戳工厂（R1：src 内不取系统时钟 / 随机；业务侧注入，测试可固定）。
    # None → __post_init__ 兜底默认（secrets / time）。
    suspend_id_factory: Any = None
    now_factory: Any = None

    # 认知回路 ⑦ 沉淀：单次 skill 执行的战绩判定器（业务可注入覆盖默认结构性判定）
    outcome_judge: Any = None  # OutcomeJudge | None；None → 用 StructuralOutcomeJudge

    # T6 deferred 暴露：auto 模式下「可见 child 数 > 此值」切 deferred 召回（pool 注入，
    # 业务可配）。同时驱动 system prompt 文本形状 + per-turn search_skills 工具裁剪
    # （二者经 effective_child_recall 同一判定，保证一致）。默认 50。
    recall_threshold: int = 50
    # 是否注入了 SkillRecall 召回后端（pool 据 skill_recall 是否为 None 透传）。
    # 默认 False = 无后端 = inline（LLM 自己找）：不暴露 search_skills、不走 deferred。
    # 与 recall_threshold 同走 pool→engine→TurnRunner 透传路径。
    has_recall_backend: bool = False

    def __post_init__(self) -> None:
        """兜底注入挂起 id / 时间戳工厂，并初始化当前迭代序号。

        R1:src 内不直接取系统时钟 / 随机;业务侧可注入固定工厂(测试用)。
        ``_current_iteration`` 在 run() 主循环每次 ``iterations += 1`` 后同步,
        用于落 SuspensionRecord.turn_index。
        """
        import secrets as _secrets
        import time as _time

        self._suspend_id_factory = self.suspend_id_factory or (
            lambda: f"sr_{_secrets.token_hex(6)}"
        )
        self._now_factory = self.now_factory or (lambda: int(_time.time()))
        # 战绩判定器默认用内核结构性实现（"系统自带一套自己的"）
        from taifeng.skill.outcome import StructuralOutcomeJudge

        self._outcome_judge = self.outcome_judge or StructuralOutcomeJudge()
        self._current_iteration = 0
        # 召回溯源映射（T7 相位 2 连回 v1 战绩沉淀）：skill_id → (origin, confidence)。
        # 本 turn 内每次 search_skills 工具完成时按返回候选登记，供 _spawn_sub_runner
        # 构造 SkillExecutionRecord 时判定 selection_origin/selection_confidence。
        # **C5 语义**：这是 TurnRunner turn 内态、**不持久化**——跨 turn / 冷 resume
        # （新 TurnRunner 实例）后该映射为空，相应 call 退化为 v1 的 whitelist/None
        # （明确语义、非 bug）。origin 恒为 "discovered"（C1：发现来源用既有 Literal
        # 的 "discovered"，严禁写入 "search"，会撞 SelectionOrigin 校验）。
        self._selection_trace: dict[str, tuple[Literal["discovered"], float]] = {}
        # DenialBreaker 实例在 run() 起点按 config 新建（turn 级生命周期）
        self._denial_breaker: DenialBreaker | None = None
        # DoomLoopDetector 实例同样在 run() 起点按 config 新建（turn 级生命周期）
        self._doom_loop: DoomLoopDetector | None = None

    async def _emit(self, msg: Any) -> None:
        try:
            await self.emit(EventMsg(submission_id=self.submission_id, msg=msg))
        except Exception:
            logger.exception("emit failed")

    def _deferred_exposure_active(self) -> bool:
        """本 entry 是否处于 deferred 召回模式（决定是否暴露 search_skills 工具）。

        与 ``render_system_prompt`` 共用 ``effective_child_recall`` 同一判定（同源），
        保证「prompt 文本是否列 child」与「per-turn 是否给 search_skills」严格一致。
        可见 child 数经 ``visible_child_skills`` G4 过滤后统计（与 prompt 同口径）。

        Returns:
            True → deferred（保留 search_skills 工具）；False → inline（剔除）。
        """
        from taifeng.skill.visibility import (
            effective_child_recall,
            visible_child_skills,
        )

        visible = visible_child_skills(
            self.entry_skill, self.snapshot, self.capabilities
        )
        mode = effective_child_recall(
            self.entry_skill,
            child_count=len(visible),
            threshold=self.recall_threshold,
            has_recall_backend=self.has_recall_backend,
        )
        return mode == "deferred"

    def _system_retry_pending(self, e: Exception) -> Any:
        """构造 LLM 失败挂起的 SYSTEM_RETRY PendingRequest(policy 裁决 SUSPEND 后用)。

        detail 携带 failure_class / retry_after / 异常类名(业务 UI 引导裁决用);
        到期自动 retry 谱系计数(auto_retry_count)非零时一并落入,fire 时与
        failure_suspend_max_auto_retries 比对熔断。
        """
        from taifeng.suspend.reason import PendingRequest, SuspendReason

        detail: dict[str, Any] = {
            "failure_class": getattr(e, "failure_class", None),
            "retry_after_seconds": getattr(e, "retry_after_seconds", None),
            "kind": type(e).__name__,
        }
        if self.auto_retry_count:
            detail["auto_retry_count"] = self.auto_retry_count
        return PendingRequest(
            request_id=self._suspend_id_factory(),
            reason=SuspendReason.SYSTEM_RETRY,
            # suspension-ttl:内核挂起按构造期声明的存活期(默认永不过期)
            ttl_seconds=self.failure_suspend_ttl_seconds,
            on_expire=self.failure_suspend_on_expire,
            payload_schema={
                "type": "object",
                "properties": {"action": {"enum": ["retry", "abort"]}},
            },
            related_call_id=None,
            detail=detail,
        )

    def _maybe_suspend_on_guard_trip(
        self, end_reason: str, guard_snapshot: dict[str, Any] | None = None
    ) -> None:
        """护栏触顶时问失败处置 policy:SUSPEND → 抛 RESOURCE_LIMIT 挂起;TERMINAL → 返回。

        三个护栏(max_iterations / resource_limit_exceeded / denial_circuit_open)
        的 break 点都在迭代边界——当轮 fc/output 已配对(K5),此处抛 SuspendSignal
        不产生孤儿;信号被 run() 的 ``except SuspendSignal`` 捕获后落盘挂起。
        resume retry = 重建 runner 续跑采样循环(预算 / 断路器随重建按原 cap 重置);
        abort = 在挂起点落失败终态。默认 policy(保守)对 guard_trip 恒 TERMINAL,
        本方法直接返回 → 调用方走既有 break,零行为变化。

        Args:
            end_reason: 触顶的护栏 end_reason(进 FailureContext 与挂起 detail)。
            guard_snapshot: 护栏快照(如断路器 consecutive/recent,R3 可观测);
                None 时 detail 只携带 end_reason。

        Raises:
            SuspendSignal: policy 裁决 SUSPEND 时,携带 RESOURCE_LIMIT pending。
        """
        from taifeng.suspend.reason import PendingRequest, SuspendReason

        policy = self.failure_policy or DEFAULT_FAILURE_POLICY
        disposition = policy.decide(FailureContext(
            origin="guard_trip",
            failure_class=None,
            end_reason=end_reason,
            error_kind=None,
            retryable=False,
            is_root=self._is_root,
            iteration=self._current_iteration,
        ))
        if disposition is not FailureDisposition.SUSPEND:
            return
        detail: dict[str, Any] = {"end_reason": end_reason}
        if guard_snapshot:
            detail["guard_snapshot"] = guard_snapshot
        if self.auto_retry_count:
            detail["auto_retry_count"] = self.auto_retry_count
        # K2(session_tokens)到期自动 retry 无人携带预算增额、必然无效 →
        # 恒 abort(覆写 failure_suspend_on_expire 配置);其余护栏照配置
        on_expire: Literal["abort", "retry"] = (
            "abort" if end_reason == "resource_limit_exceeded"
            else self.failure_suspend_on_expire)
        raise SuspendSignal(PendingRequest(
            request_id=self._suspend_id_factory(),
            reason=SuspendReason.RESOURCE_LIMIT,
            # suspension-ttl:内核挂起按构造期声明的存活期(默认永不过期)
            ttl_seconds=self.failure_suspend_ttl_seconds,
            on_expire=on_expire,
            payload_schema={
                "type": "object",
                "properties": {"action": {"enum": ["retry", "abort"]}},
            },
            related_call_id=None,
            detail=detail,
        ))

    async def _prefetch_memory(self) -> None:
        """K3 page-in：按最近用户消息 prefetch 长期记忆 → ``_prefetched_memory``。

        best-effort：``memory_store`` 抛异常被吞掉（内存层不得打断 turn）。
        """
        if self.memory_store is None:
            return
        # 默认检索 query = 最近一条用户消息文本（与 current_task 注入同源）
        query = _latest_user_text(self.history_buffer)
        # 业务侧 query 构造器：拿 history 拷贝自由组装检索语境（如近 N 轮拼接）。
        # 崩溃回退默认构造——prefetch 全链 best-effort，builder 故障不应使
        # 长期记忆整体失效（有日志，非静默）。
        if self.memory_query_builder is not None:
            try:
                query = str(self.memory_query_builder(list(self.history_buffer)))
            except Exception:
                logger.exception(
                    "memory_query_builder failed (fallback to default query)")
        try:
            text = await self.memory_store.prefetch(query, thread_id=self.thread_id)
        except Exception:
            logger.exception("memory prefetch failed (ignored)")
            return
        self._prefetched_memory = text or ""

    async def _writeback_memory(self, new_items: list[ResponseItem]) -> None:
        """K3 dirty-page 写回：本 turn 新增 items 异步写回长期存储。best-effort。"""
        if self.memory_store is None or not new_items:
            return
        try:
            await self.memory_store.writeback(
                thread_id=self.thread_id, items=list(new_items)
            )
        except Exception:
            logger.exception("memory writeback failed (ignored)")

    async def _apply_pre_evict_salvage(
        self,
        before: list[ResponseItem],
        after: list[ResponseItem],
        summary_item_id: str | None,
    ) -> list[ResponseItem]:
        """K3 swap-out：把 before 中被换出（不在 after）的 items 交给 memory
        持久化；返回 digest 作为 system_injection 插在 summary 之后。

        无 memory_store / 无换出 / digest 为空 → 原样返回 after。best-effort。
        """
        if self.memory_store is None:
            return after
        after_ids = {it.id for it in after}
        evicted = [it for it in before if it.id not in after_ids]
        if not evicted:
            return after
        try:
            digest = (await self.memory_store.on_pre_evict(evicted)) or ""
        except Exception:
            logger.exception("memory on_pre_evict failed (ignored)")
            return after
        if not digest:
            return after
        from taifeng.conversation.models import system_injection

        note = system_injection(
            digest, thread_id=self.thread_id, source="memory_pre_evict"
        )
        new_history = list(after)
        insert_at = len(new_history)
        if summary_item_id:
            for i, it in enumerate(new_history):
                if it.id == summary_item_id:
                    insert_at = i + 1
                    break
        new_history.insert(insert_at, note)
        await self.store.append(note)
        return new_history

    async def _reinject_pinned_state(
        self, history: list[ResponseItem], phase: str
    ) -> list[ResponseItem]:
        """postcompact re-injection：压缩成功后把 pinned 状态钉回 history 尾。

        紧随 K3 salvage 之后调用（两个「压缩瞬间钩子」相邻）。按注册序渲染
        全部 source（双层护栏在 registry 内完成），每条以 ``system_injection``
        （source="pinned:<name>"）追加尾部并经 store 持久化（R5）。

        渲染异常 → EngineLog 告警后跳过该 source（壳层隔离业务渲染崩溃，
        有事件、非 silent fallback）。无注入且无丢弃 → 不 emit（零噪声）。
        """
        if self.pinned_states is None or len(self.pinned_states) == 0:
            return history
        rendered = self.pinned_states.render_all()
        for name, err in rendered.errors:
            await self._emit(EngineLog(data={
                "level": "warning",
                "message": f"pinned state source {name!r} 渲染失败，已跳过: {err}",
                "extra": {"source": name},
            }))
        if not rendered.entries and not rendered.dropped:
            return history
        from taifeng.conversation.models import system_injection

        new_history = list(history)
        for entry in rendered.entries:
            note = system_injection(
                entry.text, thread_id=self.thread_id,
                source=f"pinned:{entry.name}",
            )
            new_history.append(note)
            await self.store.append(note)
        await self._emit(PinnedStateReinjected(data={
            "sources": [
                {"name": e.name, "chars": len(e.text)} for e in rendered.entries
            ],
            "total_chars": rendered.total_chars,
            "dropped": rendered.dropped,
            "phase": phase,
        }))
        return new_history

    def _history_token_estimate(self) -> int:
        """按本 turn 的图片策略与业务估算器计算完整历史成本。"""
        return estimate_history_tokens(
            self.history_buffer,
            image_input_policy=self.image_input_policy,
            input_cost_estimator=self.input_cost_estimator,
            model=self.entry_skill.model or "",
        )

    async def _maybe_inject_budget_hint(self) -> None:
        """预算自知（budget-awareness，ADR 0017 规则②）：pre-turn 估算用量，穿越
        ``soft_limit`` 时往 history 尾追一条**中性预算事实**（用了百分之几 / 距 hard
        还剩多少 token）+ emit ``BudgetHintInjected``；**穿越一次注一次**，用量回落
        到 soft 以下时复位（见 ``evaluate_budget_hint``）。

        R2：仅尾追加（不动已缓存前缀，不破 anchor），且在 pre-turn 边界；一次性
        语义把每个超限 episode 的额外 system 消息限到 1 条，避免反复刷新打断 cache。
        R1：只陈述客观事实，不含「该不该收敛」的产品意见——怎么做交给模型/业务侧。
        """
        tokens = self._history_token_estimate()
        inject, self._budget_notified = evaluate_budget_hint(
            tokens, self.budget, was_notified=self._budget_notified)
        if not inject:
            return
        from taifeng.conversation.models import system_injection

        note = system_injection(
            render_budget_hint(tokens, self.budget),
            thread_id=self.thread_id, source="budget_hint")
        self.history_buffer.append(note)
        await self.store.append(note)
        window = self.budget.context_window
        await self._emit(BudgetHintInjected(data={
            "used": tokens,
            "context_window": window,
            "ratio": round(tokens / window, 2) if window > 0 else 0.0,
            "remaining_to_hard": max(0, self.budget.hard_limit - tokens),
        }))

    async def run(self) -> TurnOutcome:
        start = time.monotonic()
        # 判定是否为根 turn —— 关键事实：根 turn 由 Engine 直接派发，构造时
        # call_stack 为空；子 turn 由 call_skill.run_sub_skill 派发，构造时
        # parent_stack 已被 push 过当前 sub skill frame（depth ≥ 1）。该值贯穿
        # 整个 run() 生命周期，最终写入 TurnCompleted / TurnFailed 的 data。
        is_root = not self.call_stack.frames
        # turn-rewind：只 root turn 记录回访节点 / 发事件（子 turn 节点 v1 不入表）
        self._is_root = is_root
        await self._emit(
            TurnStarted(
                data={
                    "entry_skill_id": self.entry_skill.id,
                    "thread_id": self.thread_id,
                    "model": self.entry_skill.model or "auto",
                }
            )
        )

        # 确保栈底是 entry skill
        if not self.call_stack.frames:
            self.call_stack = self.call_stack.push(
                skill_id=self.entry_skill.id,
                call_id=f"entry_{secrets.token_hex(4)}",
            )

        # K3 prefetch（page-in）：本 turn 开始前按最近用户消息取回长期记忆一次
        # （hoist 出 per-tool 循环，避免 N× 延迟）。记下基线长度供 writeback 切片。
        writeback_baseline = len(self.history_buffer)
        await self._prefetch_memory()

        # turn-resource-guards：迭代预算（默认 cap=max_iterations，行为等价）+
        # denial 断路器（config=None 时不启用，零变化）
        if self.iteration_budget is None:
            self.iteration_budget = IterationBudget(cap=self.max_iterations)
        iter_budget = self.iteration_budget
        if self.denial_breaker_config is not None:
            self._denial_breaker = DenialBreaker(self.denial_breaker_config)
        if self.doom_loop_config is not None:
            self._doom_loop = DoomLoopDetector(self.doom_loop_config)
        iterations = 0
        rounds = 0  # 圈序号（单调，refund 不回退）—— 供挂起/rewind 节点定位
        final_text = ""
        end_reason = "completed"
        error_msg: str | None = None
        # A1：每 turn 重置 overflow 自愈标志（防实例复用残留）
        self._overflow_recovered = False
        suspension: Any = None  # SuspensionRecord | None;命中挂起点时落盘后赋值
        try:
            # B 声明式编排：entry 声明了 orchestration → 纯编排器路径（不采样 LLM）
            if self.entry_skill.orchestration is not None:
                from taifeng.loop.orchestration_exec import run_orchestrated_turn

                final_text = await run_orchestrated_turn(self)
                iterations = 1
                end_reason = "completed"
            else:
                # turn-rewind retry_tool：先补跑被保留的悬空 call,再进采样循环。
                if self._seed_pending_call_id is not None:
                    await self._complete_seed_call(self._seed_pending_call_id)
                while True:
                    self.cancel.raise_if_cancelled()
                    if not iter_budget.consume():
                        # failure-suspension-policy:裁决 SUSPEND 时此处抛挂起信号
                        self._maybe_suspend_on_guard_trip("max_iterations")
                        end_reason = "max_iterations"
                        break
                    rounds += 1
                    # 报告值 = 预算净消费（refund 后回落）；无 refund 时与 rounds 恒等
                    iterations = iter_budget.spent
                    # 同步当前迭代序号 → 挂起时落 SuspensionRecord.turn_index
                    self._current_iteration = rounds

                    # B1 midturn-input-steering：迭代边界排空注入队列（成对 fc/output
                    # 已闭合的安全点），把运行中收到的用户输入并入 history 再采样。
                    await self._drain_pending_input()

                    # budget-awareness：压缩前按高水位用量判定是否注预算提示
                    # （穿越 soft 一次注一次）。放在压缩前，使提示反映承压瞬间。
                    await self._maybe_inject_budget_hint()

                    # pre-turn 压缩判断
                    await self._maybe_compress(phase="pre_turn")

                    # 单轮采样
                    round_text, had_tool_calls = await self._sample_once(rounds)
                    if round_text:
                        final_text += round_text

                    # K2：累计 token 触顶且仍有后续工作（tool calls）→ 强制中止本 turn，
                    # 不再继续采样（OOM-killer，防 runaway turn 无界吃 token）。
                    if had_tool_calls and self._session_limit_exceeded():
                        used = self.session_tokens_used + self.total_usage.total_tokens
                        rl_data = {
                            "limit_kind": "session_tokens",
                            "used": used,
                            "limit": self.max_session_tokens or 0,
                        }
                        # R3 scope 如实:先问 policy 再 emit —— 裁决挂起时 turn 并未
                        # abort,scope 报 turn_suspended(resource-limit-retry-semantics)
                        try:
                            self._maybe_suspend_on_guard_trip(
                                "resource_limit_exceeded",
                                {"used": used, "limit": self.max_session_tokens or 0},
                            )
                        except SuspendSignal:
                            await self._emit(ResourceLimitExceeded(
                                data={**rl_data, "scope": "turn_suspended"}))
                            raise
                        await self._emit(ResourceLimitExceeded(
                            data={**rl_data, "scope": "turn_aborted"}))
                        end_reason = "resource_limit_exceeded"
                        break

                    # refund 可能在本圈 dispatch 后发生 → 报告值取净消费
                    iterations = iter_budget.spent

                    if not had_tool_calls:
                        # 无后续 tool call → 本 turn 自然终止。空内容也视为正常终止：
                        # 决策——只有 LLM **显式报错**（如 provider 上报的 content_filter）
                        # 才是错误；模型「没产出内容」本身不是错误，按空结果继续即可，
                        # 不在 loop 层臆断成异常（空可能源于 prompt/skill，归因交业务侧）。
                        end_reason = "completed"
                        break

                    # turn-resource-guards：断路器闩锁已置（本圈 deny 记账触发）→
                    # 迭代边界提前终止——当轮 fc/output 已配对落史，无孤儿（K5 一致）。
                    if self._denial_breaker is not None and self._denial_breaker.opened:
                        # failure-suspension-policy:裁决 SUSPEND 时此处抛挂起信号
                        # (当轮 fc/output 已配对落史,K5 一致,无孤儿)
                        self._maybe_suspend_on_guard_trip(
                            "denial_circuit_open", self._denial_breaker.snapshot()
                        )
                        end_reason = "denial_circuit_open"
                        break

                    # turn-resource-guards：doom-loop 断路闩锁已置（警后仍重复到 2N）
                    # → 迭代边界提前终止（与 denial 同语义，当轮 fc/output 已配对落史）。
                    if self._doom_loop is not None and self._doom_loop.opened:
                        self._maybe_suspend_on_guard_trip(
                            "doom_loop_circuit_open", self._doom_loop.snapshot()
                        )
                        end_reason = "doom_loop_circuit_open"
                        break

                    # mid-turn 压缩判断
                    await self._maybe_compress(phase="mid_turn")
                # B1：turn 收尾补一次 drain —— 最后一轮采样期间晚到、未及在迭代起始
                # 消费的注入也要落历史（R5 不丢用户输入；它们没影响本 turn 后续采样，
                # 但已并入 history、下个 turn 可见）。仅正常退出路径；异常路径走 except。
                await self._drain_pending_input()
        except _BatchSuspend as bs:
            # 工具批次命中挂起点：落 SuspensionRecord，turn 终结于 suspended（非失败）。
            # 必须在宽 except Exception 之前捕获，否则会被分类成 TurnFailed（吞错类问题）。
            end_reason = "suspended"
            suspension = await self._persist_suspension(bs.pending)
        except SuspendSignal as sig:
            # 单点挂起（如 system_retry 在 sample 阶段抛出）：同样落盘挂起。
            suspension = await self._persist_suspension((sig.pending,))
            end_reason = "suspended"
        except asyncio.CancelledError:
            end_reason = "cancelled"
            error_msg = "cancelled"
        except Exception as e:
            end_reason = "error"
            error_msg = str(e)
            # 已分类的 LLMError（如 provider 上报的 content_filter）是**预期内、已被结构化
            # 处理**的终止条件（下方 emit TurnFailed + recovery 配方），不是未捕获的崩溃。
            # 只记一行 WARNING（带 kind + failure_class），不打整条 traceback —— 避免把
            # 「模型拒答 / 被拦截」误报成 ERROR 崩溃（拒答 ≠ 程序崩溃）。未分类异常才打堆栈。
            if isinstance(e, LLMError):
                logger.warning(
                    "turn ended via classified LLM error: kind=%s failure_class=%s",
                    e.kind, e.failure_class,
                )
            else:
                logger.exception("turn failed")
            # G3：归类到稳定 failure_class + 处置建议 + 结构化恢复配方，
            # 供 telemetry 聚合 / HITL 展示 / 业务编排层自动恢复决策。
            failure_class, suggested_action = classify_failure(e)
            recovery = recommend_recovery(failure_class)
            # G3：优先用异常自带的 request_id（失败路径 provider 回填），
            # 否则回退到本 turn 最近一次成功调用的 request_id。
            request_id = getattr(e, "request_id", None) or self._last_request_id
            await self._emit(
                TurnFailed(
                    data={
                        "error": error_msg,
                        "kind": type(e).__name__,
                        "failure_class": failure_class,
                        "suggested_action": suggested_action,
                        "recovery": recovery.to_dict(),
                        "request_id": request_id,
                        "iterations": iterations,
                        "is_root": is_root,
                    }
                )
            )

        # ADR 0029：取消 / 异常 / 挂起路径上 pending 队列可能仍有未消费注入——在终态
        # 事件之前落 buffer + store（R5 不丢），事件 delivered:false + reason=turn_ended，
        # 让停在终态的订阅者也能看到。正常路径上方已 drain 完、这里见空即返回。
        await self._drain_pending_input(residual=True)

        # K3 writeback（dirty-page）：把本 turn 新增 items 异步写回长期存储。
        await self._writeback_memory(self.history_buffer[writeback_baseline:])

        duration_ms = int((time.monotonic() - start) * 1000)
        outcome = TurnOutcome(
            success=error_msg is None and end_reason != "error",
            iterations=iterations,
            duration_ms=duration_ms,
            usage=self.total_usage,
            final_text=final_text,
            end_reason=end_reason,
            error=error_msg,
            suspension=suspension,
        )

        # R3：挂起是独立终结态，发专门的 turn_suspended（而非 turn_completed）。
        # 业务桥接层据此区分「turn 真正完成」与「turn 暂停待 Resume」两种结局，
        # 凭 thread_id + record_id 后续提交 Resume 续跑。
        if end_reason == "suspended" and suspension is not None:
            await self._emit(
                TurnSuspended(
                    data={
                        "thread_id": self.thread_id,
                        "record_id": suspension.record_id,
                        # 复用落盘用的序列化 pending（与 SuspensionRecord 持久化形态一致）
                        "pending": suspension.to_item().payload["pending"],
                        # 挂起本身不动 head / 不压缩 → 同进程续跑可保 anchor；但跨进程
                        # （tier-2）resume 必失 provider cache。此字段对业务是保守警示，取 True。
                        "cache_invalidated": True,
                        # suspension-ttl:record 级到期时刻(None=永不过期)。engine 据此
                        # 武装到期定时器;事件流经 engine._emit,所有层级 turn 统一覆盖。
                        "expires_at": suspension.expires_at,
                    }
                )
            )
        else:
            await self._emit(
                TurnCompleted(
                    data={
                        "iterations": iterations,
                        "duration_ms": duration_ms,
                        "usage": self.total_usage.model_dump(),
                        "end_reason": end_reason,
                        "success": outcome.success,
                        # is_root 区分主/子 turn —— 业务桥接层（如 Web SSE 桥）只在
                        # 根 turn completed 时认为 submission 真正结束。
                        "is_root": is_root,
                    }
                )
            )
        return outcome

    def _compute_prompt_fingerprint(self, tools: list[Any]) -> dict[str, str]:
        """计算 prompt 结构指纹 —— 用于归因 cache 失效的结构性原因（G-CACHE）。

        仅指纹影响 cached prefix 的三类结构：可见 skill 列表 / tool 集合 /
        system 段（entry skill id + body + 注入指令文本）。history 增长属正常
        tail，不入指纹（否则每轮都判为变更）。
        """
        snapshot_key = ",".join(
            sorted(self.snapshot.reachable_from(self.entry_skill.id))
        )
        tools_key = ",".join(sorted(getattr(t, "name", "") for t in tools))
        instr_text = "\x01".join(getattr(i, "text", "") for i in self.instructions)
        system_src = (
            f"{self.entry_skill.id}\x00{self.entry_skill.body}\x00{instr_text}"
        )
        return {
            "snapshot": _sha1_short(snapshot_key),
            "tools": _sha1_short(tools_key),
            "system": _sha1_short(system_src),
        }

    def _detect_structural_break_reason(
        self, current: dict[str, str]
    ) -> str | None:
        """对比上一轮指纹，判定本轮 cache 失效的结构性原因（无变更 → None）。"""
        prev = self.last_prompt_fingerprint
        if prev is None:
            return None
        if current.get("snapshot") != prev.get("snapshot"):
            return "skill_snapshot_changed"
        if current.get("tools") != prev.get("tools"):
            return "tool_spec_changed"
        if current.get("system") != prev.get("system"):
            return "system_prompt_changed"
        return None

    async def _sample_once(self, iteration: int) -> tuple[str, bool]:
        """一次 LLM 采样 + 工具调度，返回 (本轮 assistant text, 是否有 tool call)。"""

        # turn-rewind：记本圈 iteration 回访节点(采样前的 history 长度 = re_reason 截点)。
        # 同一长度供本圈所有 dispatch 节点复用为 re_reason 切点(assistant 消息原子)。
        # 仅 root turn 入表；子 turn 节点 v1 不可寻址。
        iteration_history_len = len(self.history_buffer)
        if self._is_root:
            cp = self.rewind_log.record_iteration(
                turn_index=count_turns(self.history_buffer),
                iteration_index=iteration,
                history_len=iteration_history_len,
                cache_anchor=self.cache_anchor_index,
            )
            await self._emit(RewindCheckpointRecorded(data={
                "node_id": cp.node_id, "kind": cp.kind,
                "iteration_index": cp.iteration_index,
                "history_len": cp.history_len, "target_id": None,
            }))

        # 取可用 tool 集合：声明层可见集（单一真相，含 scripts 自动并入 run_script，
        # 见 SkillDefinition.visible_tool_names）∩ registry 已注册（未注册静默不可见，现状保留）
        tools = []
        for name in sorted(self.entry_skill.visible_tool_names()):
            spec = self.tool_runtime._registry.get(name)  # noqa: SLF001
            if spec is not None:
                tools.append(spec.to_ref())

        # T6 C3 per-turn 工具裁剪：search_skills 是全局注册的（pool.create），
        # 但只在 deferred 模式下对本 entry 暴露——inline entry（小白名单 / 显式
        # inline）不暴露搜索工具（向后兼容：原本就没有 search_skills）。判定走
        # effective_child_recall（与 system prompt 文本同一真相，保证一致）。
        if self._deferred_exposure_active():
            search_spec = self.tool_runtime._registry.get(  # noqa: SLF001
                "search_skills"
            )
            # M1 去重：若作者在 SKILL.md tool_names 已显式声明 search_skills，
            # 上面的可见工具循环已把它加进来，这里不能再无条件 append（否则同名
            # 工具在 per-turn 清单里出现两次）。按已加入的工具名集合去重。
            already_added = {ref.name for ref in tools}
            if search_spec is not None and "search_skills" not in already_added:
                tools.append(search_spec.to_ref())

        # G-CACHE：算本轮 prompt 结构指纹 + 归因结构性 cache 失效原因，再更新指纹
        prompt_fingerprint = self._compute_prompt_fingerprint(tools)
        structural_break_reason = self._detect_structural_break_reason(
            prompt_fingerprint
        )
        self.last_prompt_fingerprint = prompt_fingerprint

        input_capabilities = model_capabilities(self.model_client)
        is_responses = input_capabilities.protocol == "responses"
        request = build_api_request(
            entry=self.entry_skill,
            snapshot=self.snapshot,
            history=self.history_buffer,
            tools=tools,
            # 空字符串 → 让 provider 用其自身配置的 default_model（避免业务覆盖）
            model=self.entry_skill.model or "",
            cache_anchor_index=self.cache_anchor_index,
            # T3: 已 resolve 的指令；空 list 时 render 不出现 <system_instructions>
            instructions=self.instructions if self.instructions else None,
            # G4a: 运行时能力快照（None → 不做资格过滤）
            capabilities=self.capabilities,
            # K3: page-in 的长期记忆（注入 prompt 尾部，cache-aware）
            prefetched_memory=self._prefetched_memory or None,
            # reasoning-content-passback:thinking 模型 reasoning 回传开关
            reasoning_passback=self.reasoning_passback,
            # T6: deferred 暴露阈值（驱动 child 列表 inline / deferred 文本）
            recall_threshold=self.recall_threshold,
            # 是否有召回后端：无后端恒 inline（与工具裁剪同口径）
            has_recall_backend=self.has_recall_backend,
            image_input_policy=self.image_input_policy,
            model_input_capabilities=input_capabilities,
        )

        max_bytes = self.budget.max_request_bytes
        if max_bytes is not None:
            request.metadata[MAX_REQUEST_BYTES_METADATA_KEY] = max_bytes

        # 审计可观测 层1:request 全文留痕。注入点选在「build 之后、发送 provider
        # 之前」——即便 provider 超时/失败,request 仍已留痕;mid-turn 压缩重建会走到
        # 新一轮 _run_sample 再次 build → 再 emit 一条,故「每次实发各一条」自然成立。
        # 默认关(enable_request_capture=False),零泄漏面 + 零行为变化。
        if self.enable_request_capture:
            await self._emit(
                LlmRequestRecorded(
                    data=redact_sensitive_request_data(
                        request.model_dump(mode="json")
                    )
                )
            )

        # G2b：发送前预检 —— 即便经过压缩，估算 token 仍超 hard limit 时 emit
        # 非阻塞告警（估算偏粗，不据此拒发；供业务侧主动限流 / 排查 provider 400）
        preflight_tokens = self._history_token_estimate()
        if self.budget.is_hard_exceeded(preflight_tokens):
            await self._emit(
                ContextBudgetExceeded(
                    data={
                        "token_estimate": preflight_tokens,
                        "hard_limit": self.budget.hard_limit,
                        "context_window": self.budget.context_window,
                    }
                )
            )

        # G2b body-size 硬护栏：max_request_bytes 启用时，请求体超限在发送前
        # 直接抛 RequestTooLargeError（确定性字节数，无误判；比等 provider 4xx 快）。
        if max_bytes is not None:
            request_bytes = estimate_history_bytes(self.history_buffer)
            if request_bytes > max_bytes:
                err = RequestTooLargeError(
                    f"request body ~{request_bytes}B 超出上限 {max_bytes}B",
                    estimated_bytes=request_bytes,
                    max_bytes=max_bytes,
                )
                # limit 类失败进 policy(resource-limit-retry-semantics):SUSPEND →
                # SYSTEM_RETRY 挂起(业务 CompactNow / 改参后 retry 可过);
                # Conservative 对确定性失败仍 TERMINAL → 原样抛,零行为变化
                policy = self.failure_policy or DEFAULT_FAILURE_POLICY
                if policy.decide(_llm_failure_context(
                        err, is_root=self._is_root, iteration=iteration,
                )) is FailureDisposition.SUSPEND:
                    raise SuspendSignal(self._system_retry_pending(err))
                raise err

        sess = model_session_for_turn(self, iteration)
        assistant_text = ""
        # 累积本轮 reasoning 全文(thinking 模型;非 thinking 恒为空)
        reasoning_text = ""
        # 累积 tool calls
        tool_calls: list[dict[str, Any]] = []
        normalized_items: list[dict[str, Any]] | None = None
        responses_completed = False
        # retry 已由 provider 内 retry_async 兜底(≤3 次);走到这里的 LLMError 即重试耗尽。
        # 可恢复 / 等外部介入类 → 转 SYSTEM_RETRY 挂起(等业务侧 resume 重跑同次 sample);
        # 确定性失败照旧上抛硬失败。CancelledError 走 asyncio 路径,不在此 except 内。
        try:
            async with sess as s:
                async for ev in s.stream(request):
                    self.cancel.raise_if_cancelled()
                    if is_responses and responses_completed:
                        raise InvalidResponseError(
                            "Responses emitted an event after completed"
                        )
                    if ev.kind == "text_delta":
                        delta = ev.data.get("text", "")
                        assistant_text += delta
                        await self._emit(AssistantText(data={"delta": delta}))
                    elif ev.kind == "reasoning_delta":
                        r_delta = ev.data.get("delta", "")
                        # 累积落史(reasoning-content-passback):thinking 模型要求
                        # 带 tool_calls 的 assistant 消息续传时回传 reasoning_content
                        reasoning_text += r_delta
                        await self._emit(AssistantReasoning(data={"delta": r_delta}))
                    elif ev.kind == "tool_call_done":
                        if not is_responses:
                            tool_calls.append(
                                {
                                    "call_id": ev.data["call_id"],
                                    "name": ev.data["name"],
                                    "arguments": ev.data.get("arguments", "{}"),
                                    **(
                                        {"extra_content": ev.data["extra_content"]}
                                        if "extra_content" in ev.data
                                        else {}
                                    ),
                                }
                            )
                    elif ev.kind == "normalized_output":
                        if (
                            not is_responses
                            or normalized_items is not None
                            or responses_completed
                        ):
                            raise InvalidResponseError(
                                "unexpected or duplicate normalized output"
                            )
                        raw_items = ev.data.get("items")
                        if not isinstance(raw_items, list):
                            raise InvalidResponseError(
                                "normalized output items must be a list"
                            )
                        normalized_items = raw_items
                    elif ev.kind == "completed":
                        if is_responses:
                            if normalized_items is None:
                                raise InvalidResponseError(
                                    "Responses completed before normalized output"
                                )
                            responses_completed = True
                        usage_dict = ev.data.get("usage") or {}
                        self._accumulate_usage(usage_dict)
                        # G3：回流的服务端 request-id（供失败 / telemetry 关联）
                        rid = ev.data.get("request_id")
                        if rid:
                            self._last_request_id = rid
                    elif ev.kind == "prompt_cache":
                        cache_read = int(ev.data.get("cache_read_input_tokens") or 0)
                        cache_creation = int(ev.data.get("cache_creation_input_tokens") or 0)
                        expected = self._next_cache_break_expected
                        expected_reason = self._next_cache_break_reason
                        # 消费一次预期标记
                        self._next_cache_break_expected = False
                        self._next_cache_break_reason = None
                        # G-CACHE：压缩未标记预期，但结构（snapshot/tool/system）变了 →
                        # 这是可解释的预期失效，归因到具体结构原因，避免误记 unknown_drop
                        if not expected and structural_break_reason is not None:
                            expected = True
                            expected_reason = structural_break_reason
                        break_event = self.cache_stats.record_turn(
                            cache_read=cache_read,
                            cache_creation=cache_creation,
                            anchor_expected=expected,
                            anchor_expected_reason=expected_reason,  # type: ignore[arg-type]
                        )
                        # 同步给 client 用于下一轮 previous_cache_read 比较
                        record_model_cache_read(self.model_client, cache_read)
                        if break_event is not None:
                            await self._emit(
                                CacheBreakDetected(
                                    data={
                                        "unexpected": break_event.unexpected,
                                        "token_drop": break_event.token_drop,
                                        "reason": break_event.reason,
                                        "previous": break_event.previous_cache_read_input_tokens,
                                        "current": break_event.current_cache_read_input_tokens,
                                    }
                                )
                            )
        except LLMError as e:
            # A1 reactive-compaction-recovery：provider 判「上下文超长」→ 有界自愈
            # （强制压缩一次 + 重采样一次）。本 turn 至多一次；无压缩器则不浪费重采样，
            # 直接落到下方硬失败。二次 overflow 说明压缩无效（确定性超长），快速暴露。
            from taifeng.llm.errors import ContextOverflowError

            if (
                isinstance(e, ContextOverflowError)
                and not self._overflow_recovered
                and self.compressors is not None
            ):
                self._overflow_recovered = True
                await self._emit(
                    ProviderRetry(
                        data={"reason": "context_overflow", "iteration": iteration}
                    )
                )
                await self._maybe_compress(
                    phase="overflow", force=True, bypass_trigger=True
                )
                # 单次重采样：递归深度恒为 1（标志已置 True，二次必走硬失败）
                return await self._sample_once(iteration)
            # 取消不是失败:按 ModelClient 协议字面实现的 provider 会抛
            # llm.errors.CancelledError(LLMError 子类)——直接上抛走取消链,
            # 不咨询 policy(否则 SuspendByDefault 会把用户取消转成挂起)。
            if getattr(e, "failure_class", None) == "cancelled":
                raise
            # failure-suspension-policy:裁决权交注入的 policy(None → 保守默认,
            # 零行为变化)。policy 只回答挂还是不挂;真正的裁决人是 Resume 提交者。
            policy = self.failure_policy or DEFAULT_FAILURE_POLICY
            disposition = policy.decide(_llm_failure_context(
                e, is_root=self._is_root, iteration=iteration,
            ))
            if disposition is FailureDisposition.SUSPEND:
                # 裁决挂起且重试已耗尽 → 转 SYSTEM_RETRY 挂起,等业务侧 resume 重跑同次 sample。
                # 该 SuspendSignal 穿透回 run_turn 的 except SuspendSignal(Task 7 已加),落盘挂起。
                raise SuspendSignal(self._system_retry_pending(e)) from e
            raise  # 确定性失败:照旧上抛硬失败(走 run_turn 宽 except → TurnFailed)

        if is_responses:
            if normalized_items is None or not responses_completed:
                raise InvalidResponseError(
                    "Responses requires one normalized output and one completed event"
                )
            sample_id = _responses_sample_id(
                thread_id=self.thread_id,
                submission_id=self.sample_scope_id or self.submission_id,
                turn_index=self.turn_index,
                iteration=iteration,
            )
            response_items, assistant_text, tool_calls = _responses_conversation_items(
                normalized_items,
                thread_id=self.thread_id,
                model=self.entry_skill.model or "auto",
                sample_id=sample_id,
            )
        else:
            sample_id = None
            response_items = []

        # 组装本轮 provider 顺序的会话项：reasoning → assistant →（audit 模式下）
        # function_call*。reasoning-content-passback:本轮有 reasoning 且有产出时,先落
        # reasoning item(紧邻配对 assistant message 之前,与 provider 产出顺序一致;
        # 无产出轮不落——没有可关联的 assistant 消息,回传无意义)。
        if not is_responses:
            if reasoning_text and (assistant_text or tool_calls):
                response_items.append(reasoning(reasoning_text, thread_id=self.thread_id))
            # assistant message（即使为空也记下，因为 tool calls 也挂在这条消息上）
            if assistant_text or tool_calls:
                response_items.append(assistant_message(
                    assistant_text,
                    thread_id=self.thread_id,
                    model=self.entry_skill.model or "auto",
                ))

        audit_state = self.audit_state
        if audit_state is not None:
            # audit：function_call 会话项必须与最终响应同批 durable（先于任何 Tool
            # intent/effect）；随后 dispatch 阶段跳过 legacy 逐 call 的 fc 落库。
            if not is_responses:
                for tc in tool_calls:
                    response_items.append(function_call(
                        call_id=tc["call_id"],
                        name=tc["name"],
                        arguments=tc["arguments"],
                        thread_id=self.thread_id,
                        extra_content=tc.get("extra_content"),
                    ))
            # 观测 session 在 checkpoint definite ack 后暴露 lineage；缺失即 attempt
            # 未收敛，fail closed 冻结（不得在无 durable 最终响应时继续产生效果）。
            checkpoint = getattr(sess, "last_attempt_checkpoint", None)
            if checkpoint is None:
                raise audit_state.coordinator.freeze(
                    RuntimeError("audited turn produced no attempt checkpoint")
                ) from None
            await commit_audited_llm_response(
                state=audit_state,
                submission_id=self.submission_id,
                turn_index=self.turn_index,
                iteration=iteration,
                checkpoint=checkpoint,
                items=response_items,
                cancel=self.cancel,
            )
            # 仅在 durable ack + projection 推进后才把逐字一致的会话项应用到 hot history
            self.history_buffer.extend(response_items)
        elif is_responses:
            if not isinstance(self.store, AtomicBatchMessageStore):
                raise UnsupportedPersistenceCapabilityError(
                    "Responses requires atomic terminal output persistence"
                )
            await self.store.append_atomic_batch(response_items, batch_id=sample_id)
            self.history_buffer.extend(response_items)
        else:
            for item in response_items:
                self.history_buffer.append(item)
                await self.store.append(item)

        if not tool_calls:
            return assistant_text, False

        # === 阶段 1：按发起序 emit Started + 解析参数 + 建请求（暂不写历史）===
        requests: list[ToolCallRequest] = []
        origin_samples = {
            tc["call_id"]: tc["origin_sample_id"]
            for tc in tool_calls
            if tc.get("origin_sample_id")
        }
        for idx, tc in enumerate(tool_calls):
            call_id = tc["call_id"]
            name = tc["name"]
            arguments_str = tc["arguments"]
            await self._emit(
                ToolCallStarted(
                    data={"call_id": call_id, "name": name, "arguments": arguments_str}
                )
            )
            # 解析参数（坏 JSON → 空 dict，与历史行为一致）
            try:
                arguments = json.loads(arguments_str) if arguments_str else {}
            except json.JSONDecodeError:
                arguments = {}
            tool_spec = self.tool_runtime._registry.get(name)  # noqa: SLF001
            parallel_safe = bool(tool_spec.parallel_safe) if tool_spec else False
            requests.append(
                ToolCallRequest(
                    index=idx,
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    arguments_raw=arguments_str,
                    parallel_safe=parallel_safe,
                    extra_content=tc.get("extra_content"),
                )
            )

        # === 阶段 2：并发派发（RwLock 兜底；call_skill 跳锁真并行）===
        # max_parallel_tool_calls=1 → Semaphore(1) → 退化为严格串行（零回归）
        await self._emit(
            ToolBatchDispatched(
                data={"count": len(requests), "max_parallel": self.max_parallel_tool_calls}
            )
        )
        semaphore = asyncio.Semaphore(self.max_parallel_tool_calls)

        # ctx 工厂闭包：捕获本轮 iteration（用于 turn_index 兜底，保持与历史等价）
        def _ctx_for(call_id: str) -> ToolContext:
            return self._build_tool_context(call_id, iteration)

        # tool-whitelist：与本轮请求严格同源的名集（registry 过滤后）——可见才可执行
        visible_tools = frozenset(t.name for t in tools)

        def _run_dispatch() -> Any:
            """派发本批 tool call（audit 与 legacy 复用同一并发派发器）。"""
            return dispatch_batch(
                requests,
                runtime=self.tool_runtime,
                ctx_for=_ctx_for,
                hooks=self.hooks,
                emit=self._emit,
                semaphore=semaphore,
                thread_id=self.thread_id,
                submission_id=self.submission_id,
                entry_skill_id=self.entry_skill.id,
                visible_tools=visible_tools,
            )

        # audit：整批意图先于派发 durable，取消无关地把每个意图收敛为唯一终态
        # + 唯一 function_call_output 会话项；任一 UNKNOWN 记录后冻结。fc 会话项已在
        # §7.6 最终响应批中 durable，此处只补 fco。
        if self.audit_state is not None:
            fco_items = await audited_tool_batch(
                state=self.audit_state,
                submission_id=self.submission_id,
                turn_index=self.turn_index,
                iteration=iteration,
                requests=requests,
                registry=self.tool_runtime._registry,  # noqa: SLF001
                run_dispatch=_run_dispatch,
                cancel=self.cancel,
                finalization_timeout=(
                    self.audit_state.coordinator.finalization_timeout
                ),
                origin_sample_ids=origin_samples,
            )
            self.history_buffer.extend(fco_items)
            return assistant_text, True

        outcomes = await _run_dispatch()

        # === 阶段 3：按发起序以 (call, output) 配对写历史 ===
        # 配对追加复现今天的交错 transcript 结构（history_to_api_messages 1:1 保序），
        # 并发度=1 时与历史字节级一致。
        # 挂起的 outcome 只落 function_call（留 history-gap、无 output），收集 pending 上抛。
        suspended_pending: list[Any] = []
        for req, outcome in zip(requests, outcomes, strict=True):
            fc_item = function_call(
                call_id=req.call_id,
                name=req.name,
                arguments=req.arguments_raw,
                thread_id=self.thread_id,
                extra_content=req.extra_content,
            )
            # audit：function_call 已在最终响应批中 durable + 应用到 hot history，
            # 此处不得重复入史 / 直写 projection store（transcript 唯一写者是
            # projector）。legacy：保持逐 call 配对写 fc。
            if self.audit_state is None and not is_responses:
                self.history_buffer.append(fc_item)
                await self.store.append(fc_item)
            # turn-rewind：记本次派发的 dispatch 回访节点（仅 root turn）。
            # inner_history_len = fc 追加后长度 = retry_tool 切点(fc 与 fco 之间);
            # history_len 复用本圈 iteration 采样前长度 = re_reason 切点。
            if self._is_root:
                dcp = self.rewind_log.record_dispatch(
                    turn_index=count_turns(self.history_buffer),
                    iteration_index=iteration,
                    iteration_history_len=iteration_history_len,
                    cache_anchor=self.cache_anchor_index,
                    call_id=req.call_id,
                    target_id=req.name,
                    inner_history_len=len(self.history_buffer),
                    args_digest=req.arguments_raw[:200],
                )
                await self._emit(RewindCheckpointRecorded(data={
                    "node_id": dcp.node_id, "kind": dcp.kind,
                    "iteration_index": dcp.iteration_index,
                    "history_len": dcp.history_len, "target_id": dcp.target_id,
                }))
            if outcome.suspend is not None:
                # 挂起点：留无 output 的 function_call；不回填，收集 pending。
                # resume 时由 SuspensionRecord 重导，再补回对应 function_call_output。
                suspended_pending.append(outcome.suspend)
                continue
            fco_item = self._settle_tool_output(req.call_id, outcome.result)
            origin_sample_id = origin_samples.get(req.call_id)
            if origin_sample_id:
                fco_item = fco_item.model_copy(
                    update={
                        "metadata": {
                            **fco_item.metadata,
                            "origin_llm_sample_id": origin_sample_id,
                        }
                    }
                )
            self.history_buffer.append(fco_item)
            # audit：function_call_output 的 durable + projection 归 §8 Tool 收敛；
            # 此处只入 hot history，不直写 projection store（避免与 projector 竞写）。
            if self.audit_state is None:
                await self.store.append(fco_item)
            # turn-resource-guards：单点观察 deny/success 记账 + refunds_iteration 退还
            await self._note_tool_outcome(req.name, outcome.result, req.arguments_raw)

        if suspended_pending:
            # 整批挂起 pending 上抛给 run()/run_turn 聚合落一条 SuspensionRecord。
            raise _BatchSuspend(tuple(suspended_pending))
        return assistant_text, True

    async def _note_tool_outcome(
        self, name: str, result: Any, arguments_raw: str = ""
    ) -> None:
        """配对回填后的单点记账（turn-resource-guards）。

        - DenialBreaker：``ToolResult.data["reason"] ∈ {hook_denied,
          permission_denied}`` 计 deny（含 HITL ask 超时产生的 deny —— 它就是
          deny 结果）；成功结果重置 consecutive。其他 error 中性（不计任何一边）。
          恰好越阈值那次 emit ``denial_circuit_open``（单次闩锁）。
        - DoomLoopDetector：仅在**成功**结果上记 (tool, arguments_raw)。连续 N 次
          同签名 → ``warn``（注中性事实让模型自改 + emit）；警后到 2N → ``open``
          （emit + 置闩锁，迭代边界终止）。deny/error 不计（交 DenialBreaker）。
        - refund：spec 静态声明 ``refunds_iteration`` 且本次**成功** → 外层迭代
          预算退还一步（失败轮照常计费；不暴露为 LLM 可触发语义）。
        """
        # T7 召回溯源：search_skills 成功完成 → 把返回候选登记进 turn 内溯源映射，
        # 供后续 call_skill 派发时判定 discovered。只对 search_skills 做，别误伤其他工具。
        if name == "search_skills" and not result.is_error:
            self._register_selection_trace(result.output)
        deny_reason = result.data.get("reason") if result.is_error else None
        if self._denial_breaker is not None:
            if deny_reason in ("hook_denied", "permission_denied"):
                if self._denial_breaker.record_denial(name):
                    await self._emit(
                        DenialCircuitOpen(data=self._denial_breaker.snapshot())
                    )
            elif not result.is_error:
                self._denial_breaker.record_success()
        # doom-loop：只观察成功调用的 (tool, args) 重复空转
        if self._doom_loop is not None and not result.is_error:
            action = self._doom_loop.record(name, arguments_raw)
            if action == "warn":
                await self._inject_doom_loop_notice(self._doom_loop.snapshot())
                await self._emit(DoomLoopWarned(data=self._doom_loop.snapshot()))
            elif action == "open":
                await self._emit(
                    DoomLoopCircuitOpen(data=self._doom_loop.snapshot())
                )
        if not result.is_error and self.iteration_budget is not None:
            spec = self.tool_runtime.spec_for(name)
            if spec is not None and spec.refunds_iteration:
                self.iteration_budget.refund(1)

    def _register_selection_trace(self, search_output: str) -> None:
        """解析 search_skills 返回的候选 JSON，登记进 turn 内召回溯源映射（T7）。

        把每个候选 ``(skill_id → ("discovered", confidence))`` 写入
        ``self._selection_trace``，供 ``_spawn_sub_runner`` 据派发目标 id 判定
        ``selection_origin``/``selection_confidence``。

        覆盖语义：同一 skill_id 后到的召回**覆盖**先到的——最近一次召回的 confidence
        与「LLM 当前据以决定 call_skill」最相关，故取最新。

        容错：search_skills handler 产出的是 ``json.dumps(payload)``（list[dict]，每项
        含 ``skill_id``/``confidence``）。这里只在结构符合预期时登记；结构异常（坏 JSON /
        非 list / 缺字段）跳过该项即可——溯源是 best-effort 增益，缺失只是退化为
        whitelist/None，不影响派发主流程（非 silent fallback：不伪造默认 confidence，
        缺字段就不登记，让其走 v1 行为）。

        Args:
            search_output: search_skills 工具的成功 ``ToolResult.output``（候选 JSON 串）。
        """
        try:
            candidates = json.loads(search_output)
        except json.JSONDecodeError:
            # 坏 JSON：search_skills 正常路径不会产生，但工具被业务替换 / mock 时可能；
            # 溯源跳过即可（退化 whitelist），不阻断派发
            logger.warning("search_skills output not valid JSON, skip trace")
            return
        if not isinstance(candidates, list):
            return
        for cand in candidates:
            # 候选项必须是含 skill_id(str) + confidence(数值) 的 dict，否则跳过该项
            if not isinstance(cand, dict):
                continue
            skill_id = cand.get("skill_id")
            confidence = cand.get("confidence")
            if not isinstance(skill_id, str) or not isinstance(
                confidence, (int, float)
            ):
                continue
            # 同 skill_id 覆盖：最近一次召回的 confidence 更贴合 LLM 当前决策
            self._selection_trace[skill_id] = ("discovered", float(confidence))

    async def _inject_doom_loop_notice(self, snap: dict[str, Any]) -> None:
        """doom-loop 先警：往 history 尾追一条**中性事实**（不含产品意见，R1）。

        只陈述「同工具同参数已连续 N 次、结果一致」的客观事实，是否换路交模型自判。
        尾追加（cache anchor 之后，R2 无额外 break）+ 持久化（R5）。
        """
        from taifeng.conversation.models import system_injection

        note = system_injection(
            f"Notice: tool {snap.get('tool')!r} has been called "
            f"{snap.get('consecutive')} times in a row with identical arguments, "
            f"each returning an identical result.",
            thread_id=self.thread_id, source="doom_loop")
        self.history_buffer.append(note)
        await self.store.append(note)

    def _build_tool_context(self, call_id: str, iteration: int) -> ToolContext:
        """为单条 tool call 构造 ToolContext（独立 cancel.child）。

        供 ``tool_batch.dispatch_batch`` 在并发派发时按 call_id 取上下文。extras 内容
        与历史串行实现完全一致（call_skill 仍能通过 dispatcher 找到本 runner），包括
        ``iteration`` 与 ``turn_index`` 的 ``self.turn_index or iteration`` 兜底语义。
        """
        return ToolContext(
            call_id=call_id,
            cancel=self.cancel.child(f"tool:{call_id}"),
            thread_id=self.thread_id,
            extras={
                "skill_snapshot": self.snapshot,
                "visible_skills": self.snapshot.reachable_from(self.entry_skill.id),
                # search_skills 据此施加 G4a requires 过滤（与 inline 列表同源同过滤）
                "capabilities": self.capabilities,
                # 当前 turn 的原始用户任务：供 search_skills 验证门判输入适配（详情五）。
                # 召回用关键词 query（词面匹配），验证用原始任务（含输入上下文），两者不共用。
                "current_task": _latest_user_text(self.history_buffer),
                "dispatch_policy": self.dispatch_policy,
                "call_stack": self.call_stack,
                "current_skill": self.entry_skill,
                "dispatcher": self,  # 让 call_skill 找到自己
                "iteration": iteration,
                "submission_id": self.submission_id,
                "entry_skill_id": self.entry_skill.id,
                # === call_skill 走 PermissionPolicy + Hook 所需的上下文 ===
                "permission_policy": self.permission_policy,
                "hook_runner": self.hooks,
                "request_metadata": self.request_metadata,
                "turn_index": self.turn_index or iteration,
                # === run_script 工具按 language 查 executor ===
                "script_executors": self.script_executors,
                # === detached-spawn 四工具据此拿到 engine 的 spawn API ===
                "spawn_coordinator": self.spawn_coordinator,
            },
        )

    async def _complete_seed_call(self, call_id: str) -> None:
        """retry_tool：补跑一个悬空 function_call(history 末尾留 fc、无 fco)→ 追加 fco。

        复用 ``dispatch_batch`` + ``_build_tool_context``(含 ``dispatcher``),故
        ``call_skill`` 子 skill 也能正确重跑。args 取 history 中该 fc 的当前 arguments
        (engine 已按 new_args 改写过)。若重跑又挂起(子 skill HITL),照常上抛 ``_BatchSuspend``。

        Raises:
            RuntimeError: history 中找不到该 call_id 的 function_call(断点不一致)。
        """
        # 找末条匹配的 function_call(即被保留的悬空 fc)
        fc = None
        for item in self.history_buffer:
            if item.kind == "function_call" and item.payload.get("call_id") == call_id:
                fc = item
        if fc is None:
            raise RuntimeError(f"seed_call_not_found: {call_id}")
        name = fc.payload["name"]
        raw = fc.payload.get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            args = {}
        tool_spec = self.tool_runtime._registry.get(name)  # noqa: SLF001
        parallel_safe = bool(tool_spec.parallel_safe) if tool_spec else False
        req = ToolCallRequest(
            index=0, call_id=call_id, name=name,
            arguments=args, arguments_raw=raw, parallel_safe=parallel_safe,
        )
        outcomes = await dispatch_batch(
            [req], runtime=self.tool_runtime,
            ctx_for=lambda cid: self._build_tool_context(cid, 0),
            hooks=self.hooks, emit=self._emit,
            semaphore=asyncio.Semaphore(1),
            thread_id=self.thread_id, submission_id=self.submission_id,
            entry_skill_id=self.entry_skill.id,
            # retry 重跑仍受声明层可见集约束（原始派发已过校验；热重载移除声明则如实拒）
            visible_tools=self.entry_skill.visible_tool_names(),
        )
        outcome = outcomes[0]
        if outcome.suspend is not None:
            raise _BatchSuspend((outcome.suspend,))
        fco = self._settle_tool_output(call_id, outcome.result)
        self.history_buffer.append(fco)
        await self.store.append(fco)

    def _settle_tool_output(self, call_id: str, result: ToolResult) -> ResponseItem:
        """把 ToolResult 结算成 function_call_output item（两处结算点共用）。

        图片附件在此完成 **durable append 之前**的 admission。违规**不上抛出批**：
        抛出会让本次派发只剩一条无 output 的悬空 ``function_call``，fc/fco 配对
        断裂 → OpenAI-compat 直接 400。转成该次调用的错误结果，既保住配对，又把
        原因如实送进模型视野让它自行纠正。

        Args:
            call_id: 与 function_call 配对的调用 id。
            result: 工具返回的 ``ToolResult``。

        Returns:
            可直接入史的 ``function_call_output`` item。
        """
        try:
            attachments = admit_tool_attachments(
                result.attachments, self.image_input_policy
            )
        except (
            AttachmentTooLargeError,
            ImageCountExceededError,
            InvalidImageError,
            UnsupportedModalityError,
        ) as exc:
            return function_call_output(
                call_id=call_id,
                output=f"tool_attachment_rejected: {exc}",
                thread_id=self.thread_id,
                is_error=True,
            )
        return function_call_output(
            call_id=call_id,
            output=result.output,
            thread_id=self.thread_id,
            is_error=result.is_error,
            attachments=attachments,
        )

    async def _persist_suspension(self, pending: tuple[Any, ...]) -> Any:
        """把本次挂起落 store 并返回 SuspensionRecord（R5 跨进程 resume 的真相）。

        record_id / created_at 由注入工厂提供（R1：src 内不取系统时钟 / 随机）；
        pending 为本次挂起的全部 PendingRequest。

        Args:
            pending: 本 turn 全部挂起点的 PendingRequest（不可变 tuple）。

        Returns:
            落盘后的 SuspensionRecord（同时已追加进 history_buffer 与 store）。
        """
        from taifeng.suspend.record import SuspensionRecord

        record = SuspensionRecord(
            record_id=self._suspend_id_factory(),
            thread_id=self.thread_id,
            submission_id=self.submission_id,
            turn_index=self._current_iteration,
            pending=pending,
            created_at=self._now_factory(),
        )
        item = record.to_item()
        self.history_buffer.append(item)
        await self.store.append(item)
        return record

    def _session_limit_exceeded(self) -> bool:
        """K2：会话累计 token（基线 + 本 turn 已用）是否达到上限。

        None → 不强制（默认）。total_tokens 取累计；上限值由业务注入。
        """
        if self.max_session_tokens is None:
            return False
        used = self.session_tokens_used + self.total_usage.total_tokens
        return used >= self.max_session_tokens

    def _accumulate_usage(self, usage_dict: dict[str, Any]) -> None:
        u = TokenUsage(**usage_dict) if usage_dict else TokenUsage()
        self.total_usage = TokenUsage(
            input_tokens=self.total_usage.input_tokens + u.input_tokens,
            output_tokens=self.total_usage.output_tokens + u.output_tokens,
            total_tokens=self.total_usage.total_tokens
            + (u.total_tokens or u.input_tokens + u.output_tokens),
            cache_creation_input_tokens=self.total_usage.cache_creation_input_tokens
            + u.cache_creation_input_tokens,
            cache_read_input_tokens=self.total_usage.cache_read_input_tokens
            + u.cache_read_input_tokens,
            reasoning_tokens=self.total_usage.reasoning_tokens + u.reasoning_tokens,
            raw=u.raw,
        )

    async def _drain_pending_input(self, *, residual: bool = False) -> None:
        """B1：把 pending_input 队列并入 history。

        迭代边界调用（``residual=False``）：取出全部 pending（保留提交顺序），逐条追加
        history_buffer + store.append + emit 对应注入事件（user_message →
        ``user_input_injected``、system_injection → ``system_message_injected``，
        delivered:true）；turn 已取消则不并入。

        turn 退出路径调用（``residual=True``，ADR 0029）：不看取消位，把残留全部落史，
        事件 delivered:false + reason="turn_ended"——文本未进入本 turn 的 prompt 但没丢。

        副作用：history_buffer / store 追加；pending_input 清空；emit 事件。
        """
        if not self.pending_input:
            return
        if not residual and self.cancel.is_cancelled:
            return
        # 同 event loop 协作式调度：取出 + 清空在无 await 的同步段完成，避免与
        # engine 主循环 append 竞态（无需锁）。
        drained = list(self.pending_input)
        self.pending_input.clear()
        for item in drained:
            self.history_buffer.append(item)
            await self.store.append(item)
            await self._emit(
                injection_event(
                    item, self.submission_id,
                    delivered=not residual,
                    reason="turn_ended" if residual else None,
                )
            )

    async def _maybe_compress(
        self, *, phase: str, force: bool = False, bypass_trigger: bool = False
    ) -> None:
        """触发压缩判断。

        Args:
            phase: pre_turn / mid_turn / manual / overflow
            force: 跳过 budget 阈值预检（manual / overflow 路径为 True）
            bypass_trigger: 走 orchestrator.force_compress 绕过各策略 should_trigger
                （A1 overflow 自愈：本地估算偏低、provider 已判超长，should_trigger
                必返回 None，必须强制压缩）。
        """
        if self.compressors is None:
            return
        tokens = self._history_token_estimate()
        if not force:
            if phase == "pre_turn" and not self.budget.is_soft_exceeded(tokens):
                return
            if phase == "mid_turn" and not self.budget.is_soft_exceeded(tokens):
                return

        # === pre_compact hook ===
        # 业务侧拦截点：在 strategy 执行前可拒绝本轮压缩。
        # spec hooks/Requirement "pre_compact hook 调用点" 强约束：
        #   - deny → 跳过本轮压缩，history / cache_anchor / _next_cache_break_expected 不动
        #   - allow → 进入 CompactionStarted 既有路径
        #   - manual (force=True) 路径也走 hook，业务可拒绝管理员触发
        if self.hooks is not None:
            from taifeng.hooks.types import HookContext, PreCompactHook
            pre_decision = await self.hooks.run(
                "pre_compact",
                PreCompactHook(
                    phase=phase,  # type: ignore[arg-type]
                    token_estimate=tokens,
                    history_length=len(self.history_buffer),
                ),
                HookContext(
                    thread_id=self.thread_id,
                    submission_id=self.submission_id,
                    entry_skill_id=self.entry_skill.id,
                ),
            )
            if not pre_decision.allow:
                await self._emit(PreCompactHookSkipped(data={
                    "phase": phase,
                    "reason": pre_decision.reason or "",
                    "token_estimate": tokens,
                    "history_length": len(self.history_buffer),
                }))
                return

        injection = (
            InitialContextInjection.DO_NOT_INJECT
            if phase in ("mid_turn", "overflow")
            else InitialContextInjection.BEFORE_LAST_USER_MESSAGE
        )

        ctx = CompressionContext(
            history=list(self.history_buffer),
            token_estimate=tokens,
            budget=self.budget,
            cache_anchor_index=self.cache_anchor_index,
            phase=phase,  # type: ignore[arg-type]
            available_injections=frozenset({injection}),
        )
        await self._emit(
            CompactionStarted(
                data={"phase": phase, "strategy": "auto", "token_estimate": tokens}
            )
        )
        # bypass_trigger：overflow 自愈走强制路径（绕过 should_trigger）
        if bypass_trigger:
            result = await self.compressors.force_compress(ctx, injection)
        else:
            result = await self.compressors.maybe_compress(ctx, injection)
        if result is None:
            return
        # G1b：压缩成功，但若产物相对原 history 引入了新的 tool 配对孤儿 → 回滚
        # （不应用），保留原 history。保留历史优于把损坏会话喂给 provider。
        if result.success:
            new_orphans = _history_orphan_call_ids(
                result.new_history
            ) - _history_orphan_call_ids(ctx.history)
            if new_orphans:
                await self._emit(
                    CompactionIntegrityRolledBack(
                        data={"issues": sorted(new_orphans), "phase": phase}
                    )
                )
                await self._emit(
                    CompactionCompleted(
                        data={
                            "success": False,
                            "cache_invalidated": False,
                            "removed_count": 0,
                            "reason": "integrity_rolled_back",
                            "detail": {},
                        }
                    )
                )
                return
        # 应用压缩结果
        if result.success:
            # K3 on_pre_evict（swap-out 抢救）：把将被换出的 items 交给 memory
            # 持久化，取回「必须留在上下文」的 digest，作为 system_injection 折进
            # 保留段（紧随 summary）。digest 在 anchor 之后，不影响 cache anchor。
            new_history = await self._apply_pre_evict_salvage(
                ctx.history, result.new_history, result.summary_item_id
            )
            # postcompact re-injection：pinned 状态钉回 tail（K3 salvage 之后、
            # 写回 buffer 之前——任何成功压缩路径含 overflow 自愈均覆盖）。
            new_history = await self._reinject_pinned_state(new_history, phase)
            self.history_buffer[:] = new_history
            self.cache_anchor_index = result.anchor_preserved_until
            # 持久化新的 compacted item
            if result.summary_item_id:
                for it in new_history:
                    if it.id == result.summary_item_id:
                        await self.store.append(it)
                        break
            # 如果压缩破坏了 cache，标记下一次 LLM 调用的 break 为预期内
            if result.cache_invalidated:
                self._next_cache_break_expected = True
                self._next_cache_break_reason = (
                    "compaction_pre_turn"
                    if phase == "pre_turn"
                    else "compaction_manual"
                    if phase == "manual"
                    else "compaction_mid_turn_anchor_lost"
                )
            # G1c：成功压缩计数；达阈值后每次压缩 emit 降级告警
            self.compaction_count += 1
            if self.compaction_count >= self.compaction_degradation_threshold:
                await self._emit(
                    CompactionDegradationWarning(
                        data={
                            "compaction_count": self.compaction_count,
                            "threshold": self.compaction_degradation_threshold,
                        }
                    )
                )
        await self._emit(
            CompactionCompleted(
                data={
                    "success": result.success,
                    "cache_invalidated": result.cache_invalidated,
                    "removed_count": result.removed_item_count,
                    "reason": result.reason,
                    # 策略自报明细（如 surgical_trim 的 deduped/soft/hard 计数）；
                    # 既有策略为空 dict —— R3 机读透传，不编码进 reason
                    "detail": result.detail,
                }
            )
        )

    # ---- dispatcher 接口（供 call_skill tool 调用）----

    async def run_sub_skill(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
    ) -> ToolResult:
        """派发子 skill —— 启动一个嵌套 TurnRunner 处理。

        子 skill 共享 store / snapshot / tool_runtime / compressors，
        但 history_buffer 是独立的（子 skill 是隔离上下文）。
        """
        # 通知事件 —— call-skill-reason-field: 从 ctx.extras 取 LLM 自陈
        # reason（call_skill handler 写入），缺省空串
        dispatch_reason = str(ctx.extras.get("dispatch_reason") or "")
        await self._emit(
            SkillDispatched(
                data={
                    "skill_id": target.id,
                    "call_id": parent_stack.current.call_id if parent_stack.current else "",
                    "depth": parent_stack.depth,
                    "stack_path": parent_stack.path(),
                    "reason": dispatch_reason,
                }
            )
        )

        # audit：在外层 call_skill Tool 意图之后先 durable skill_selected（完整
        # definition/body 快照）；随后配额拒绝走 rejected 终态，接受走 child 谱系。
        audit_dispatch: AuditedSkillDispatch | None = None
        if self.audit_state is not None:
            parent_call_id = str(
                ctx.extras.get("parent_call_skill_call_id") or ctx.call_id
            )
            audit_dispatch = AuditedSkillDispatch(
                state=self.audit_state,
                parent_turn_index=self.turn_index,
                parent_call_id=parent_call_id,
                submission_id=self.submission_id,
                cancel=ctx.cancel,
            )
            await audit_dispatch.commit_selected(
                target=target, arguments=arguments
            )

        # K1 广度准入：spawn 配额（engine 注入 registry 时生效）。超限→拒绝派发，
        # 不创建 thread / 不跑子 turn，返回 error 让 LLM 自行调整（fork-bomb 防护）。
        if self.spawn_registry is not None:
            from taifeng.loop.spawn import SpawnLimitError
            try:
                async with self.spawn_registry.reserve():
                    return await self._spawn_sub_runner(
                        target=target, arguments=arguments,
                        parent_stack=parent_stack, ctx=ctx,
                        audit_dispatch=audit_dispatch,
                    )
            except SpawnLimitError as e:
                await self._emit(SkillSpawnRejected(data={
                    "skill_id": target.id,
                    "call_id": ctx.call_id,
                    "limit_kind": e.kind,
                    "limit": e.limit,
                }))
                # audit：配额拒绝 → skill_dispatch_finished(rejected)，无 child lineage
                if audit_dispatch is not None:
                    await audit_dispatch.reject(
                        reason=f"spawn_limit:{e.kind}"
                    )
                return ToolResult.error(
                    f"spawn_limit_exceeded: {e.kind} >= {e.limit}",
                    reason="spawn_limit",
                )
        return await self._spawn_sub_runner(
            target=target, arguments=arguments,
            parent_stack=parent_stack, ctx=ctx,
            audit_dispatch=audit_dispatch,
        )

    async def _spawn_sub_runner(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
        audit_dispatch: AuditedSkillDispatch | None = None,
    ) -> ToolResult:
        """实际派发子 TurnRunner（已通过 K1 spawn 准入）。"""
        # G3 subagent-isolation-policy: 根据 dispatch_policy.subagent_approval_mode
        # 决定子 TurnRunner 收到的 permission_policy。inherit + 父=None → None；
        # auto_* + 父=None → 仍 None（无 policy 可包装，不引入额外门控）
        sub_permission_policy = self.permission_policy
        mode = self.dispatch_policy.subagent_approval_mode
        if mode != "inherit" and self.permission_policy is not None:
            from taifeng.skill.dispatch import _SubagentAutoDecisionPolicy

            sub_permission_policy = _SubagentAutoDecisionPolicy(
                inner=self.permission_policy,
                fallback="deny" if mode == "auto_deny" else "allow",
            )
            await self._emit(
                SubagentPolicyOverridden(
                    data={
                        "target_skill_id": target.id,
                        "mode": mode,
                        "depth": parent_stack.depth + 1,
                    }
                )
            )

        # 子 turn：把 sub_args 序列化为 user_message 作为种子输入。
        # audit：child thread/seed 走 Journal 原子 started 批（thread_created/bound/
        # child-seed）+ child projection；child_state 让子 turn 递归走同一审计路径。
        # legacy：K7 把谱系持久化进 ThreadMetadata.extra，seed 直写 store。
        child_ctx = None
        child_audit_state = None
        if audit_dispatch is not None:
            child_ctx = await audit_dispatch.start_child(
                target=target,
                arguments=arguments,
                call_stack_path=tuple(parent_stack.path()),
            )
            sub_thread_id = child_ctx.child_thread_id
            seed = child_ctx.seed_item
            child_audit_state = child_ctx.child_state
        else:
            sub_thread_id = await self.store.create_thread(
                cwd=None,
                entry_skill_id=target.id,
                source=f"subskill:{self.entry_skill.id}",
                extra={
                    "parent_thread_id": self.thread_id,
                    "spawn_depth": parent_stack.depth,
                    "stack_path": parent_stack.path(),
                },
            )
            from taifeng.conversation.models import user_message
            seed = user_message(
                json.dumps(arguments, ensure_ascii=False),
                thread_id=sub_thread_id,
            )
            await self.store.append(seed)

        sub_runner = TurnRunner(
            entry_skill=target,
            snapshot=self.snapshot,
            model_client=self.model_client,
            tool_runtime=self.tool_runtime,
            store=self.store,
            compressors=self.compressors,
            dispatch_policy=self.dispatch_policy,
            budget=self.budget,
            thread_id=sub_thread_id,
            submission_id=self.submission_id,
            emit=self.emit,
            cancel=ctx.cancel,
            image_input_policy=self.image_input_policy,
            input_cost_estimator=self.input_cost_estimator,
            hooks=self.hooks,
            # G3: 透传或包装后的 permission_policy（auto_deny/auto_allow 时已包装）
            permission_policy=sub_permission_policy,
            request_metadata=self.request_metadata,
            turn_index=self.turn_index,
            script_executors=self.script_executors,
            max_iterations=self.max_iterations,
            # turn-resource-guards：子 turn 独立迭代预算（默认 cap=父初始 cap，
            # 不回写父——hermes 对标的有意语义）+ 断路器配置继承（子自建实例）
            iteration_budget=(
                self.iteration_budget.child()
                if self.iteration_budget is not None
                else None
            ),
            denial_breaker_config=self.denial_breaker_config,
            # failure-suspension-policy: 子 turn 继承父的失败处置裁决 policy
            failure_policy=self.failure_policy,
            failure_suspend_ttl_seconds=self.failure_suspend_ttl_seconds,
            failure_suspend_on_expire=self.failure_suspend_on_expire,
            auto_retry_count=self.auto_retry_count,
            max_parallel_tool_calls=self.max_parallel_tool_calls,
            # reasoning-content-passback: 子 turn 继承回传开关
            reasoning_passback=self.reasoning_passback,
            # G4a: 子 turn 继承同一运行时能力快照
            capabilities=self.capabilities,
            # K1: 子 turn 共享同一 spawn registry（配额贯穿整棵 turn 树）
            spawn_registry=self.spawn_registry,
            # K2: 子 turn 继承会话 token 上限（基线 = 父基线 + 父本 turn 已用）
            session_tokens_used=self.session_tokens_used
            + self.total_usage.total_tokens,
            max_session_tokens=self.max_session_tokens,
            # K3: 子 turn 共享同一 memory store
            memory_store=self.memory_store,
            # 认知回路 ⑦：子 turn 继承同一战绩判定器（嵌套派发也按统一判据沉淀）
            outcome_judge=self.outcome_judge,
            # T6: 子 turn 继承同一 deferred 暴露阈值（整棵 turn 树一致）
            recall_threshold=self.recall_threshold,
            # 子 turn 继承同一召回后端存在性（整棵 turn 树一致）
            has_recall_backend=self.has_recall_backend,
            call_stack=parent_stack,
            history_buffer=[seed],
            # audit：子 turn 携带 child audit_state（共享根 coordinator/lease、同一
            # projector 的 child thread）→ 子的 LLM/Tool/Skill 效果递归走同一审计路径。
            audit_state=child_audit_state,
        )
        outcome = await sub_runner.run()
        # audit：strict 能力面不接受挂起（skill_suspension/HITL/failure_policy 均已被
        # 静态拒）；子 turn 若仍挂起属 capability 契约违约 → fail closed 冻结。
        if child_ctx is not None and outcome.end_reason == "suspended":
            assert self.audit_state is not None
            raise self.audit_state.coordinator.freeze(
                RuntimeError("audited child skill suspended (capability violation)")
            ) from None
        # 子 turn 挂起：子 thread 内已落 SuspensionRecord 并 emit turn_suspended。
        # 父的 call_skill 必须随之挂起（而非把挂起误当成功/失败回填），否则父 turn
        # 会带着占位结果继续/完成，子挂起永远无法回传父调用栈（item d 的根因）。
        # 抛 SuspendSignal(reason=CHILD_SKILL) → 父 _dispatch_one 捕获为 outcome.suspend
        # → 父 _BatchSuspend → 父落自己的 SuspensionRecord（pending 携带 sub_thread_id），
        # 逐层上抛至根 → 根 emit turn_suspended。resume 时 engine 续跑链据 sub_thread_id
        # 先续跑子 thread 拿结果，再回填本 call_skill 的 function_call_output。
        if outcome.end_reason == "suspended":
            from taifeng.suspend.reason import PendingRequest, SuspendReason
            from taifeng.suspend.signal import SuspendSignal as _SuspendSignal

            raise _SuspendSignal(
                PendingRequest(
                    request_id=self._suspend_id_factory(),
                    reason=SuspendReason.CHILD_SKILL,
                    # 父 resume 时据 related_call_id 定位要回填 output 的 call_skill；
                    # 据 detail.sub_thread_id 定位要先续跑的子 thread。
                    # 必须用【父 call_skill 的 call_id】（call_skill builtin 经 extras 透传，
                    # = LLM 给的 tool_call id，父 function_call 落盘用它），而非 ctx.call_id
                    # （= 子帧 sub_call_id "sk_*"）。否则回填 output 与父 function_call 失配 →
                    # OpenAI-compat orphan tool → 400。extras 缺失时退回 ctx.call_id（仅
                    # 防御性：call_skill 派发路径必设此 key）。
                    related_call_id=ctx.extras.get(
                        "parent_call_skill_call_id"
                    ) or ctx.call_id,
                    detail={"sub_thread_id": sub_thread_id, "skill_id": target.id},
                )
            )

        await self._emit(
            SkillReturned(
                data={
                    "skill_id": target.id,
                    "call_id": ctx.call_id,
                    "success": outcome.success,
                    "summary": outcome.final_text[:200],
                }
            )
        )

        # —— 认知回路 ⑦ 沉淀：判战绩 → 落旁路记录 → emit 事件 ——
        # 注：suspended 已在上方提前 raise SuspendSignal 返回，到此 outcome 必为终态。
        from taifeng.conversation.models import skill_outcome_item
        from taifeng.loop.event import SkillOutcomeRecorded
        from taifeng.skill.outcome import (
            SelectionOrigin,
            SkillExecutionContext,
            SkillExecutionRecord,
        )

        _verdict = self._outcome_judge.judge(
            SkillExecutionContext(
                success=outcome.success,
                end_reason=outcome.end_reason,
                error=outcome.error,
            )
        )
        # parent_stack.current 是【目标自身】的栈帧（call_skill 已 push target）；
        # 其 .call_id == ctx.call_id（本次执行），其 .parent_call_id 才是【调用方】的
        # call_id（root 派发时为 None）——这正是记录要的 parent_call_id。
        _self_frame = parent_stack.current
        # T7 召回溯源：若本 turn 内 target 曾被 search_skills 召回选中（命中 turn 内
        # 溯源映射）→ 记 discovered + 该候选 confidence；否则保持 v1 行为 whitelist/None。
        # C5：映射是 turn 内态、不持久化，跨 turn / 冷 resume 后退化为 whitelist/None。
        _traced = self._selection_trace.get(target.id)
        _selection_origin: SelectionOrigin = (
            _traced[0] if _traced is not None else "whitelist"
        )
        _selection_confidence = _traced[1] if _traced is not None else None
        _record = SkillExecutionRecord(
            skill_id=target.id,
            call_id=ctx.call_id,
            parent_call_id=_self_frame.parent_call_id if _self_frame else None,
            depth=parent_stack.depth,
            source=target.source,
            trust_tier=None,  # v1 留空；来源信任分层在后续相位填
            # 经 search_skills 召回派发 → discovered + confidence；否则 v1 的 whitelist/None
            selection_origin=_selection_origin,
            selection_confidence=_selection_confidence,
            outcome=_verdict.status,
            outcome_signal_source=_verdict.signal_source,
            end_reason=outcome.end_reason,
            error_detail=(outcome.error[:200] if outcome.error else None),
            cost_tokens=outcome.usage.total_tokens,
            cost_duration_ms=outcome.duration_ms,
            cost_iterations=outcome.iterations,
            ts_unix=self._now_factory(),
        )
        if child_ctx is not None and audit_dispatch is not None:
            # audit：原子 finished + thread_terminal + skill_outcome（先于外层 Tool
            # outcome）；skill_outcome 会话项由 finish_child 落 durable + 投影。
            from taifeng.conversation.journal.records import (
                SkillStatus as _SkillStatus,
            )
            from taifeng.conversation.journal.records import (
                StableErrorV1 as _StableErrorV1,
            )
            if outcome.end_reason == "cancelled":
                _status = _SkillStatus.CANCELLED
            elif outcome.success:
                _status = _SkillStatus.SUCCESS
            else:
                _status = _SkillStatus.ERROR
            _child_error = (
                None
                if _status is _SkillStatus.SUCCESS
                else _StableErrorV1(
                    code="skill_child_terminal_error",
                    class_name="ChildSkillError",
                    failure_class="skill_error",
                    retryable=False,
                )
            )
            await audit_dispatch.finish_child(
                child=child_ctx,
                status=_status,
                end_reason=outcome.end_reason,
                final_text=outcome.final_text,
                outcome_payload=_record.as_payload(),
                error=_child_error,
            )
        else:
            await self.store.append(
                skill_outcome_item(_record.as_payload(), thread_id=sub_thread_id)
            )
        await self._emit(SkillOutcomeRecorded(data=_record.as_payload()))

        if outcome.success:
            return ToolResult.ok(outcome.final_text, sub_thread_id=sub_thread_id)
        return ToolResult.error(
            f"sub_skill_failed: {outcome.error or outcome.end_reason}",
            sub_thread_id=sub_thread_id,
        )
