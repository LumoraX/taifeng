"""TurnRunner —— 单 turn 执行：采样 → 处理事件 → 工具调度 → mid-turn 压缩 → 决定是否继续。

参照：codex codex-rs/core/src/session/turn.rs::run_turn
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from taifeng.context.budget import (
    ContextBudget,
)
from taifeng.context.cache_stats import PromptCacheStats
from taifeng.context.compressor import (
    CompressionOrchestrator,
)
from taifeng.conversation.models import (
    ResponseItem,
)
from taifeng.llm.errors import (
    LLMError,
    classify_failure,
)
from taifeng.llm.image_input import (
    DISABLED_IMAGE_POLICY,
    ImageInputPolicy,
    InputCostEstimator,
)
from taifeng.llm.recovery import recommend_recovery
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit_skill import AuditedSkillDispatch
from taifeng.loop.denial_breaker import DenialBreaker, DenialBreakerConfig
from taifeng.loop.doom_loop import DoomLoopConfig, DoomLoopDetector
from taifeng.loop.event import (
    EventMsg,
    ResourceLimitExceeded,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    TurnSuspended,
)
from taifeng.loop.failure_policy import (
    FailureDispositionPolicy,
)
from taifeng.loop.iteration_budget import IterationBudget
from taifeng.loop.rewind import RewindLog
from taifeng.loop.turn_compaction import TurnCompaction
from taifeng.loop.turn_context import TurnContextLoad
from taifeng.loop.turn_dispatch import TurnDispatch
from taifeng.loop.turn_guards import TurnGuards
from taifeng.loop.turn_persist import TurnPersist
from taifeng.loop.turn_sample import TurnSample
from taifeng.loop.turn_tooling import TurnTooling
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
# 模块级纯函数 helper 已下沉 turn_helpers.py（Wave 4）。此处原样再导入，
# turn.py 内既有引用与「打 turn 模块级符号」的注入点均不受影响。
from taifeng.loop.turn_helpers import (  # noqa: E402, F401
    _history_orphan_call_ids,  # noqa: F401 —— 兼容再导出：测试按 turn 模块寻址
    _latest_user_text,
    _llm_failure_context,  # noqa: F401 —— 兼容再导出：测试按 turn 模块寻址
    _responses_conversation_items,
    _responses_sample_id,
    _sha1_short,
)


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
    # cache anchor(含语义,cache-anchor 契约):history 中最后一条已被 provider 缓存的
    # 条目下标,-1 = 无缓存。采样成功后 _sample_once 推进到「发出时末项」;压缩回写
    # anchor_preserved_until;rewind 回退 cut-1;跨进程重载置 -1
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
    # 最后一次采样的 provider 原生终止原因（llm-provider-native 契约，不跨家归一）
    last_stop_reason: str | None = field(default=None)
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
    # 本轮已流式输出的 assistant 文本（取消时落 partial，正常落史后清空；非持久状态）
    _streamed_text: str = field(default="", init=False, repr=False)

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
        # Wave 4 协作器装配：均无自有状态，运行态仍由本 TurnRunner 持有
        self._dispatch = TurnDispatch(self)
        self._compaction = TurnCompaction(self)
        self._tooling = TurnTooling(self)
        self._sample = TurnSample(self)
        self._ctxload = TurnContextLoad(self)
        self._persist = TurnPersist(self)
        self._guards = TurnGuards(self)

    async def _emit(self, msg: Any) -> None:
        try:
            await self.emit(EventMsg(submission_id=self.submission_id, msg=msg))
        except Exception:
            logger.exception("emit failed")

    # -----------------------------------------------------------------
    # 实现已下沉 turn_guards.py（Wave 4）。以下为薄委托：TurnRunner 是唯一白盒
    # 寻址面，兄弟模块与测试按这些原名调用/打桩，签名逐字保留。
    # -----------------------------------------------------------------

    def _deferred_exposure_active(self) -> bool:
        """本 entry 是否处于 deferred 召回模式（决定是否暴露 search_skills 工具）。"""
        return self._guards.deferred_exposure_active()

    def _system_retry_pending(self, e: Exception) -> Any:
        """构造 LLM 失败挂起的 SYSTEM_RETRY PendingRequest(policy 裁决 SUSPEND 后用)。"""
        return self._guards.system_retry_pending(e)

    def _maybe_suspend_on_guard_trip(
        self, end_reason: str, guard_snapshot: dict[str, Any] | None = None
    ) -> None:
        """护栏触顶时问失败处置 policy:SUSPEND → 抛 RESOURCE_LIMIT 挂起;TERMINAL → 返回。"""
        self._guards.maybe_suspend_on_guard_trip(end_reason, guard_snapshot)


    async def _prefetch_memory(self) -> None:
        """K3 page-in：按最近用户消息 prefetch 长期记忆 → ``_prefetched_memory``。"""
        await self._ctxload.prefetch_memory()

    async def _writeback_memory(self, new_items: list[ResponseItem]) -> None:
        """K3 dirty-page 写回：本 turn 新增 items 异步写回长期存储。best-effort。"""
        await self._ctxload.writeback_memory(new_items)

    async def _apply_pre_evict_salvage(
        self,
        before: list[ResponseItem],
        after: list[ResponseItem],
        summary_item_id: str | None,
    ) -> list[ResponseItem]:
        """K3 swap-out：把 before 中被换出（不在 after）的 items 交给 memory"""
        return await self._ctxload.apply_pre_evict_salvage(before, after, summary_item_id)

    async def _reinject_pinned_state(
        self, history: list[ResponseItem], phase: str
    ) -> list[ResponseItem]:
        """postcompact re-injection：压缩成功后把 pinned 状态钉回 history 尾。"""
        return await self._ctxload.reinject_pinned_state(history, phase)

    def _history_token_estimate(self) -> int:
        """按本 turn 的图片策略与业务估算器计算完整历史成本。"""
        return self._ctxload.history_token_estimate()

    async def _maybe_inject_budget_hint(self) -> None:
        """预算自知（budget-awareness，ADR 0017 规则②）：pre-turn 估算用量，穿越"""
        await self._ctxload.maybe_inject_budget_hint()


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
            await self._persist_partial_assistant()
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
                        # provider 原生终止原因（最后一次采样），不跨家归一
                        "stop_reason": self.last_stop_reason,
                        # is_root 区分主/子 turn —— 业务桥接层（如 Web SSE 桥）只在
                        # 根 turn completed 时认为 submission 真正结束。
                        "is_root": is_root,
                    }
                )
            )
        return outcome

    # -----------------------------------------------------------------
    # 实现已下沉 turn_sample.py（Wave 4）。以下为薄委托：TurnRunner 是唯一白盒
    # 寻址面，兄弟模块与测试按这些原名调用/打桩，签名逐字保留。
    # -----------------------------------------------------------------

    def _compute_prompt_fingerprint(self, tools: list[Any]) -> dict[str, str]:
        """计算 prompt 结构指纹 —— 用于归因 cache 失效的结构性原因（G-CACHE）。"""
        return self._sample.compute_prompt_fingerprint(tools)

    def _detect_structural_break_reason(
        self, current: dict[str, str]
    ) -> str | None:
        """对比上一轮指纹，判定本轮 cache 失效的结构性原因（无变更 → None）。"""
        return self._sample.detect_structural_break_reason(current)

    async def _sample_once(self, iteration: int) -> tuple[str, bool]:
        """一次 LLM 采样 + 工具调度，返回 (本轮 assistant text, 是否有 tool call)。"""
        return await self._sample.sample_once(iteration)


    async def _note_tool_outcome(
        self, name: str, result: Any, arguments_raw: str = ""
    ) -> None:
        """配对回填后的单点记账（turn-resource-guards）。"""
        await self._tooling.note_tool_outcome(name, result, arguments_raw)

    def _register_selection_trace(self, search_output: str) -> None:
        """解析 search_skills 返回的候选 JSON，登记进 turn 内召回溯源映射（T7）。"""
        self._tooling.register_selection_trace(search_output)

    async def _inject_doom_loop_notice(self, snap: dict[str, Any]) -> None:
        """doom-loop 先警：往 history 尾追一条**中性事实**（不含产品意见，R1）。"""
        await self._tooling.inject_doom_loop_notice(snap)

    def _build_tool_context(self, call_id: str, iteration: int) -> ToolContext:
        """为单条 tool call 构造 ToolContext（独立 cancel.child）。"""
        return self._tooling.build_tool_context(call_id, iteration)

    async def _complete_seed_call(self, call_id: str) -> None:
        """retry_tool：补跑一个悬空 function_call(history 末尾留 fc、无 fco)→ 追加 fco。"""
        await self._tooling.complete_seed_call(call_id)

    def _settle_tool_output(self, call_id: str, result: ToolResult) -> ResponseItem:
        """把 ToolResult 结算成 function_call_output item（两处结算点共用）。"""
        return self._tooling.settle_tool_output(call_id, result)


    # -----------------------------------------------------------------
    # 实现已下沉 turn_persist.py（Wave 4）。以下为薄委托：TurnRunner 是唯一白盒
    # 寻址面，兄弟模块与测试按这些原名调用/打桩，签名逐字保留。
    # -----------------------------------------------------------------

    async def _persist_suspension(self, pending: tuple[Any, ...]) -> Any:
        """把本次挂起落 store 并返回 SuspensionRecord（R5 跨进程 resume 的真相）。"""
        return await self._persist.persist_suspension(pending)

    def _session_limit_exceeded(self) -> bool:
        """K2：会话累计 token（基线 + 本 turn 已用）是否达到上限。"""
        return self._persist.session_limit_exceeded()

    def _accumulate_usage(self, usage_dict: dict[str, Any]) -> None:
        """_accumulate_usage"""
        self._persist.accumulate_usage(usage_dict)

    async def _persist_partial_assistant(self) -> None:
        """取消时把已流式输出、尚未落史的 assistant 文本以 truncated 标记落史（R5）。"""
        await self._persist.persist_partial_assistant()

    async def _drain_pending_input(self, *, residual: bool = False) -> None:
        """B1：把 pending_input 队列并入 history。"""
        await self._persist.drain_pending_input(residual=residual)


    async def _maybe_compress(
        self,
        *,
        phase: str,
        force: bool = False,
        bypass_trigger: bool = False,
        allow_head: bool = False,
    ) -> bool:
        """触发压缩判断。"""
        return await self._compaction.maybe_compress(
            phase=phase,
            force=force,
            bypass_trigger=bypass_trigger,
            allow_head=allow_head,
        )


    async def run_sub_skill(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
    ) -> ToolResult:
        """派发子 skill —— 启动一个嵌套 TurnRunner 处理。"""
        return await self._dispatch.run_sub_skill(
            target=target,
            arguments=arguments,
            parent_stack=parent_stack,
            ctx=ctx,
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
        return await self._dispatch.spawn_sub_runner(
            target=target,
            arguments=arguments,
            parent_stack=parent_stack,
            ctx=ctx,
            audit_dispatch=audit_dispatch,
        )

