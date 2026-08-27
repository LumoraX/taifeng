"""AgentEngine —— 主 actor + Submission / EventMsg 双向消息总线。

参照：codex codex-rs/core/src/session/mod.rs::Codex
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from taifeng.context.budget import ContextBudget
from taifeng.context.cache_stats import PromptCacheStats
from taifeng.conversation.models import (
    ResponseItem,
    function_call,
    function_call_output,
    system_injection,
    user_message,
)
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.instructions.resolver import InstructionResolver
from taifeng.instructions.source import InstructionFetchError
from taifeng.instructions.types import (
    InstructionContext,
    InstructionLayer,
    ResolvedInstruction,
)
from taifeng.llm.errors import LLMError
from taifeng.loop.audit_admission import (
    AcceptedUserMessage,
    AuditedUserMessageSubmission,
    InvalidAuditedSubmissionError,
    UnsupportedAuditedOperationError,
    admit_user_message,
    prepare_user_message,
    reject_invalid_user_message,
    reject_unsupported_audited_op,
    user_message_input_descriptor_hash,
)
from taifeng.loop.audit_cancel import (
    AuditedCancelTurnSubmission,
    apply_cancel_turn,
    finalize_cancelled_target,
)
from taifeng.loop.audit_history import (
    AuditedHistoryConflictError,
    audited_history_conflict_failure,
    merge_audited_history,
)
from taifeng.loop.audit_lifecycle import SessionLifecycle
from taifeng.loop.audit_llm import AuditedTurnInput, audited_turn_index
from taifeng.loop.audit_mailbox import (
    AuditedApplicationCheckpoint,
    AuditedSubmissionMailbox,
    finalize_audited_mailbox,
    handoff_accepted_user_message,
    retire_started_audited_token,
)
from taifeng.loop.audit_shutdown import shutdown_submission, submit_audited_shutdown
from taifeng.loop.audit_support import AuditHealth
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import (
    EngineLog,
    EventMsg,
    InstructionCacheHit,
    InstructionFetched,
    InstructionFetchFailed,
    InstructionUpdated,
    InstructionUpdateRejected,
    PostTurnHookFired,
    PreTurnHookDenied,
    ResourceLimitExceeded,
    RewindRejected,
    RewindTableRebuilt,
    SuspensionExpired,
    SuspensionPartiallyResolved,
    SuspensionResolved,
    SuspensionResolveRejected,
    TurnFailed,
    TurnRewound,
    TurnSuspended,
    UserInputInjected,
)
from taifeng.loop.event import Shutdown as ShutdownMsg
from taifeng.loop.rewind import RewindCheckpoint, count_turns, derive_rewind_log
from taifeng.loop.spawn_driver import SpawnDriver
from taifeng.loop.submission import (
    CancelTurn,
    CompactNow,
    InjectSystemMessage,
    InjectUserInput,
    Op,
    RefreshSnapshot,
    Resume,
    Rewind,
    SendToPeer,
    Shutdown,
    Submission,
    ThreadRollback,
    UpdateBudget,
    UpdateInstructions,
    UserMessage,
)
from taifeng.loop.turn import TurnOutcome, TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.suspend.record import SuspensionRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

    from taifeng.context.compressor import CompressionOrchestrator
    from taifeng.conversation.store import MessageStore
    from taifeng.llm.client import ModelClient
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.spawn_handle import SpawnHandle, SpawnHandleRegistry
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.registry import SkillSnapshot
    from taifeng.tool.runtime import ToolCallRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveredEvent:
    """投递给某个订阅者的事件信封（审计可观测 层1）。

    携带 per-subscriber 的投递序号 ``delivery_seq``：每个订阅各自从 0 起连续；
    队列满被丢弃（QueueFull）时该序号仍被「烧掉」（计入但不投递），故订阅者收到的
    ``delivery_seq`` 一旦跳号 = **它自己**漏了事件——与全局 ``event.seq`` 跳号
    （那是过滤订阅天然只收子集所致，非丢弃）互不混淆。

    属性：
        event: 原始事件（其 ``seq`` 是该 engine 总线的全局序号）。
        delivery_seq: 本订阅者的连续投递序号（从 0 起，含丢弃烧号）。
    """

    event: EventMsg
    delivery_seq: int


class _Subscriber:
    """单个订阅的队列 + 投递簿记（审计可观测 层1）。

    封装三件 per-subscriber 状态：
    1. ``queue``：进程内 asyncio 队列（maxsize<=0 表示无界）。
    2. ``next_delivery``：下一个投递序号（每次投递尝试 +1，含丢弃烧号）。
    3. 高/低水位告警迟滞：``warned`` 标记 + ``last_warn`` 时戳，避免阈值附近刷屏。

    参数：
        maxsize: 队列容量（<=0 无界）。
        high_ratio / low_ratio: 高/低水位占容量的比例（仅有界队列有意义）。
    """

    def __init__(self, *, maxsize: int, high_ratio: float, low_ratio: float) -> None:
        self.queue: asyncio.Queue[DeliveredEvent] = asyncio.Queue(
            maxsize=maxsize if maxsize > 0 else 0
        )
        self.next_delivery: int = 0
        # 高/低水位绝对阈值：仅有界（maxsize>0）时可换算；无界时为 None → 不告警
        self.high_water: int | None = int(maxsize * high_ratio) if maxsize > 0 else None
        self.low_water: int | None = int(maxsize * low_ratio) if maxsize > 0 else None
        self.warned: bool = False
        self.last_warn: int | None = None


@dataclass
class _PendingTurn:
    submission_id: str
    cancel: CancellationToken
    turn_index: int | None = None
    # B1 midturn-input-steering：注入队列。engine 处理 InjectUserInput Op 时 append，
    # 与对应活跃 TurnRunner.pending_input 共享同一 list 引用，runner 迭代边界 drain。
    pending_input: list[ResponseItem] = field(default_factory=list)


class AgentEngine:
    """主 actor。

    生命周期：
        1. 业务构造 `engine = AgentEngine(...)`
        2. `task = asyncio.create_task(engine.run(root_cancel))`
        3. `sub_id = await engine.submit(UserMessage(...))`
        4. `async for ev in engine.subscribe(sub_id): ...`
        5. `await engine.submit(Shutdown())` + `await task`
    """

    def __init__(
        self,
        *,
        entry_skill: SkillDefinition,
        skill_snapshot: SkillSnapshot,
        tool_runtime: ToolCallRuntime,
        model_client: ModelClient,
        store: MessageStore,
        thread_id: str,
        session_id: str | None = None,
        compressors: CompressionOrchestrator | None = None,
        dispatch_policy: DispatchPolicy | None = None,
        outcome_judge: Any = None,
        budget: ContextBudget | None = None,
        hooks: Any = None,
        max_iterations: int | None = None,
        denial_breaker_config: Any = None,
        doom_loop_config: Any = None,
        failure_policy: Any = None,
        failure_suspend_ttl_seconds: int | None = None,
        failure_suspend_max_auto_retries: int | None = None,
        failure_suspend_on_expire: Literal["abort", "retry"] = "abort",
        now_factory: Any = None,
        max_parallel_tool_calls: int = 1,
        reasoning_passback: bool = True,
        enable_request_capture: bool = False,
        event_queue_size: int = 65536,
        event_high_water_ratio: float = 0.75,
        event_low_water_ratio: float = 0.5,
        event_warn_cooldown_sec: int = 5,
        submission_queue_size: int = 256,
        instruction_layers: list[InstructionLayer] | None = None,
        script_executors: dict[str, Any] | None = None,
        initial_history: list[ResponseItem] | None = None,
        permission_policy: Any = None,
        request_metadata: dict[str, Any] | None = None,
        compaction_degradation_threshold: int = 3,
        capabilities: Any = None,
        max_concurrent_spawns: int = 16,
        max_total_spawns: int = 1000,
        max_session_tokens: int | None = None,
        memory_store: Any = None,
        memory_query_builder: Any = None,
        pinned_state_sources: list[Any] | None = None,
        pinned_total_max_chars: int = 8000,
        recall_threshold: int = 50,
        has_recall_backend: bool = False,
        image_input_policy: Any = None,
        input_cost_estimator: Any = None,
    ) -> None:
        """
        Args:
            initial_history: 可选预填充 history（resume 场景用）。engine 自身
                **不**调 store.load_thread —— 加载责任在 pool 层。传入列表会被
                **拷贝**到 self._history，外部后续修改不影响 engine 状态。
                ``_cache_anchor_index`` 保持 -1（跨进程不可信任 provider cache）。
                详见 spec ``jsonl-transcript`` / change ``engine-resume-by-thread-id``。
        """
        if not entry_skill.entry:
            raise ValueError(
                f"skill {entry_skill.id!r} is not entry-eligible (entry=false)"
            )
        self._entry_skill = entry_skill
        self._snapshot = skill_snapshot
        self._tool_runtime = tool_runtime
        self._model_client = model_client
        from taifeng.llm.image_input import DISABLED_IMAGE_POLICY

        self._image_input_policy = image_input_policy or DISABLED_IMAGE_POLICY
        self._input_cost_estimator = input_cost_estimator
        self._store = store
        self._thread_id = thread_id
        # session_id 主要用于 InstructionContext.session_id 缓存键；若未传，
        # 退回到 thread_id（保持单 engine 单 session 的一对一）
        self._session_id = session_id or thread_id
        self._compressors = compressors
        self._dispatch_policy = dispatch_policy or DispatchPolicy()
        # R1 业务注入点：战绩判官（None → TurnRunner.__post_init__ 兜底 StructuralOutcomeJudge）
        self._outcome_judge = outcome_judge
        self._budget = budget or ContextBudget()
        self._hooks = hooks
        # T5: ScriptLanguage → ScriptExecutor 映射；为空时 run_script 工具失败
        self._script_executors: dict[str, Any] = dict(script_executors or {})
        # 单 turn 内最大循环；None → 用 TurnRunner 默认（32）
        from taifeng.loop.turn import DEFAULT_MAX_INNER_ITERATIONS

        self._max_iterations = (
            max_iterations if max_iterations is not None else DEFAULT_MAX_INNER_ITERATIONS
        )
        # 单 turn 内一批 tool call 的最大并发；默认 1=串行。透传到每个 TurnRunner。
        self._max_parallel_tool_calls = max_parallel_tool_calls
        # reasoning-content-passback：thinking 模型 reasoning 回传开关。透传到每个
        # TurnRunner；默认开（history 无 reasoning item 即不回传，非 thinking 零变化）。
        self._reasoning_passback = reasoning_passback
        # 审计可观测 层1：LLM request 全文留痕开关，透传到每个 TurnRunner。
        # 默认关 → 零泄漏面 + 零行为变化；开启后 request 在发送 provider 前留痕。
        self._enable_request_capture = enable_request_capture
        # turn-resource-guards：denial 断路器配置（DenialBreakerConfig | None；
        # None=不启用零变化）。实例由各 TurnRunner 每 turn 新建（turn 级生命周期）。
        self._denial_breaker_config = denial_breaker_config
        self._doom_loop_config = doom_loop_config
        # failure-suspension-policy：失败处置裁决 policy
        # （FailureDispositionPolicy | None;None → turn 层保守默认,零行为变化）
        self._failure_policy = failure_policy
        # suspension-ttl:内核自产挂起的存活期声明(透传 TurnRunner)
        # 构造期校验(报错点贴近配置点;否则首次护栏挂起时才在 PendingRequest
        # __post_init__ 炸出、被宽 except 吞为 turn_failed,远离根因)
        if (failure_suspend_ttl_seconds is not None
                and failure_suspend_ttl_seconds <= 0):
            raise ValueError(
                f"failure_suspend_ttl_seconds must be positive or None, "
                f"got {failure_suspend_ttl_seconds}")
        self._failure_suspend_ttl_seconds = failure_suspend_ttl_seconds
        # resource-limit-retry-semantics:TTL 自动 retry 谱系上限(None=不限,
        # 配 on_expire="retry" 时强烈建议设置——否则确定性失败会无界自动循环)
        if (failure_suspend_max_auto_retries is not None
                and failure_suspend_max_auto_retries <= 0):
            raise ValueError(
                f"failure_suspend_max_auto_retries must be positive or None, "
                f"got {failure_suspend_max_auto_retries}")
        self._failure_suspend_max_auto_retries = failure_suspend_max_auto_retries
        self._failure_suspend_on_expire = failure_suspend_on_expire
        # suspension-ttl：壁钟工厂(注入可固定,测试用;默认与 TurnRunner 兜底一致)
        import time as _time

        self._now_factory = now_factory or (lambda: int(_time.time()))
        # suspension-ttl：record_id → 到期定时任务。挂起落盘(turn_suspended)武装,
        # 人工 Resume 核销(suspension_resolved)/ shutdown 取消;先核销者胜。
        self._ttl_timers: dict[str, asyncio.Task[None]] = {}
        # actor 派发出的 turn/resume/rewind 与 TTL 均属于 Engine 生命周期；
        # run() 任何退出路径必须取消并等待它们收敛，才能交还持久化 ownership。
        self._operation_tasks: set[asyncio.Task[None]] = set()
        # multi-pending-partial-resume:per-record 结算锁——并发续跑链(双子同时
        # Resume/到期)对同一父 record 的「判定剩余 → 落 marker → 续跑」必须串行,
        # 否则可能双双判 partial(无人续跑)或双双 settle(双重续跑)
        self._settle_locks: dict[str, asyncio.Lock] = {}
        # suspension-ttl-hardening:在飞 Resume 守卫——record 命中后立即占位,
        # 闭合「人工 Resume 处理中(marker 未落)时定时器 fire」的双裁决窗口
        self._resolving_records: set[str] = set()
        # config-consistency-fixes C2: 把 event_queue_size kwarg 真正生效
        # 之前此 kwarg 收下后未存到 self，subscribe / subscribe_all 内仍硬编码 1024
        # 审计可观测 层1：默认放大到 65536（按最慢逐条 fsync 落盘消费者 ~6500 events/s
        # × 10 估算）——有界 ⇒ 内存天花板可预测、绝不 OOM；<=0 为无界 opt-in（自负 OOM）。
        self._event_queue_size = event_queue_size
        # 高/低水位比例 + 告警限频秒数（仅有界队列生效）。穿高水位告一条 warning，
        # 回落到低水位以下才重新武装（迟滞，防阈值附近刷屏）。
        self._event_high_water_ratio = event_high_water_ratio
        self._event_low_water_ratio = event_low_water_ratio
        self._event_warn_cooldown_sec = event_warn_cooldown_sec
        # 审计可观测 层1：全局事件序号计数器（单 engine = 单 session 内单调）。
        # 在 _emit 入口同步分配，asyncio 单线程无 await 让出点 → 原子不重不漏。
        self._seq: int = 0
        # permission_policy / request_metadata 透传链：
        # EnginePool → AgentEngine → TurnRunner。request_metadata 是业务侧不透明
        # 上下文（无业务命名字段，R1），合并进 PermissionRequest.metadata /
        # InstructionContext.metadata；taifeng 不解析其 keys。
        self._permission_policy = permission_policy
        self._request_metadata: dict[str, Any] = dict(request_metadata or {})
        # G4a: 业务注入的运行时能力快照（用于 skill 资格过滤）；None=不过滤
        self._capabilities = capabilities
        # T6 deferred 暴露：auto 模式下「可见 child 数 > 此值」切 deferred 召回。
        # 透传到每个 TurnRunner，驱动 system prompt 文本 + search_skills 工具裁剪。
        self._recall_threshold = recall_threshold
        # 是否注入了 SkillRecall 召回后端（pool 据 skill_recall 是否为 None 透传）。
        # 默认 False = 无后端 = inline（LLM 自己找）；与 recall_threshold 同走透传链。
        self._has_recall_backend = has_recall_backend
        # K1：spawn 配额 registry —— engine 持有一份，贯穿整棵 turn 树（含跨 turn）。
        from taifeng.loop.spawn import SpawnSlotRegistry

        self._spawn_registry = SpawnSlotRegistry(
            max_concurrent=max_concurrent_spawns,
            max_total=max_total_spawns,
        )
        # K2：会话级累计 token 上限（OOM-killer）。_session_tokens 跨 turn 累计；
        # None → 不强制（默认，行为不变）。
        # K2 上限构造期校验:0/负值无意义(0 会使首条消息即触顶且增额判定歧义)
        if max_session_tokens is not None and max_session_tokens <= 0:
            raise ValueError(
                f"max_session_tokens must be positive or None, "
                f"got {max_session_tokens}")
        self._max_session_tokens = max_session_tokens
        # K3：长期记忆 swap 接口（None=无内存层级，默认行为不变）
        self._memory_store = memory_store
        # prefetch 检索 query 构造器（None=默认：最后一条用户消息）
        self._memory_query_builder = memory_query_builder
        # postcompact-state-reinjection：pinned 注册表（engine 级共享实例，贯穿
        # 所有 TurnRunner）。构造期注入 list + 运行时 register/unregister 增删；
        # 空注册表在 turn 层短路（零行为变化）。同名注册 ValueError 由 registry 保证。
        from taifeng.context.pinned_state import PinnedStateRegistry

        self._pinned_states = PinnedStateRegistry(
            total_max_chars=pinned_total_max_chars
        )
        for _src in pinned_state_sources or []:
            self._pinned_states.register(_src)
        self._session_tokens: int = 0
        # detached-spawn：协调器（句柄表 / 分离驱动 / 错峰 resume / join-barrier / 冷恢复）
        # 抽到 SpawnDriver（见 spawn_driver.py），engine 仅留薄转发器（公共 API + 调用点）。
        # SpawnDriver 复用 engine 的 _spawn_registry(K1) / _root_cancel / _build_child_runner /
        # store / emit / snapshot / lock / history 等共享内部，不复制这些状态。
        self._spawn = SpawnDriver(self)
        # 根取消 token —— 由 run() 入口捕获；spawn 的分离 task 据此派生子 token（R4）。
        # run() 启动前为 None（spawn_skill 在 engine.run 已起的前提下被调用）。
        self._root_cancel: CancellationToken | None = None
        # strict audit ownership 由 EnginePool 在 actor 启动前注入；None 保持 legacy。
        self._audit_state: AuditedSessionState | None = None
        self._audit_finish_owner: Callable[[], Awaitable[None]] | None = None
        # K4 入站背压：bounded submission 队列；submit() await put，满则业务侧阻塞
        # （flow control）。<=0 视为不限（保留逃生口）。
        self._submissions: asyncio.Queue[Submission | AcceptedUserMessage] = asyncio.Queue(
            maxsize=submission_queue_size if submission_queue_size > 0 else 0
        )
        self._audited_mailbox = AuditedSubmissionMailbox()
        # K4 出站丢弃计数：事件队列满时不再静默丢——累计 + 暴露（可观测）。
        self._events_dropped: int = 0
        # 审计可观测 层1：订阅者从裸 Queue 升级为 _Subscriber（队列 + per-subscriber
        # 投递序号 + 水位告警迟滞）。每 submission 至多一个过滤订阅；firehose 可多个。
        self._event_subs: dict[str, _Subscriber] = {}
        self._all_subs: list[_Subscriber] = []
        self._pending: dict[str, _PendingTurn] = {}
        self._running = False
        self._audited_shutdown_enqueued = False
        # 跨 turn 持久化的 history view（用于复用 + cache）
        # 冷加载（resume）场景：先把 raw transcript 重建为与热内存等价的逻辑 history
        # （折叠压缩区间、截断 rewind/rollback 被回滚的尾段），再推导 rewind 节点表。
        # 对无压缩/无 rewind 的干净 thread 是恒等映射（纯 CPU、不碰 IO）。
        raw_init: list[ResponseItem] = list(initial_history) if initial_history else []
        self._history: list[ResponseItem] = reconstruct_logical_history(raw_init)
        # cache anchor 保持 -1：resume 场景下 provider prompt cache 跨进程
        # 不可信任，下一次 turn 的 pre_turn 压缩会重新决定 anchor 位置
        self._cache_anchor_index: int = -1
        # turn-rewind 冷重建：从逻辑 history 现算全 turn 节点表（纯 CPU，不碰 IO）。
        # 新建 thread（initial_history 为空/None）→ 空节点表（既有行为不变）。
        self._rewind_checkpoints: list[RewindCheckpoint] = derive_rewind_log(self._history)
        # G-CACHE：cache 统计与 prompt 结构指纹由 engine 持有 → 跨 turn 累积/对比，
        # 使 cache 失效原因（snapshot/tool/system 变更）可被归因而非记为 unknown_drop。
        self._cache_stats = PromptCacheStats()
        self._last_prompt_fingerprint: dict[str, str] | None = None
        # G1c：单一 thread 累计成功压缩次数（跨 turn 持久）+ 降级告警阈值
        self._compaction_count: int = 0
        self._compaction_degradation_threshold = compaction_degradation_threshold
        self._lock = asyncio.Lock()
        # 单 engine 内 turn 序号累计（用于 InstructionContext.turn_index）
        self._turn_index: int = 0
        # 审计 UserMessage admission 独立排序；不占用 Session lifecycle lock。
        self._audited_admission_lock = asyncio.Lock()
        self._next_audited_turn_index = 0

        # === instructions-injection T4 ===
        # 构造 resolver；emit 桥接到 engine 自己的 _emit（适配 EventMsg pydantic）
        self._instruction_layers: list[InstructionLayer] = list(
            instruction_layers or []
        )
        self._instruction_resolver: InstructionResolver | None = None
        if self._instruction_layers:
            self._instruction_resolver = InstructionResolver(
                self._instruction_layers,
                emit=self._instruction_emit_bridge,
            )
        # 当前 turn 用的 submission_id（仅用于 resolver emit 关联事件流）
        self._current_emit_submission_id: str = "*"
        # engine scope 一次性 resolve 缓存
        self._engine_scope_resolved: list[ResolvedInstruction] = []
        # 最近一次完整 resolve（engine+session+turn）的快照
        self._last_resolved: list[ResolvedInstruction] = []

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def session_id(self) -> str:
        """本 engine 所属 session 标识（恒非空；未显式传入时退回 thread_id）。

        审计可观测 层1：sink 在 attach 时捕获它，与事件 ``seq`` 复合成全局唯一
        落库主键 ``(session_id, seq)``——故 ``session_id`` 不盖在每条事件上。
        """
        return self._session_id

    def register_pinned_state(self, source: Any) -> None:
        """运行时注册 pinned 状态源（生效于下一次成功压缩）。

        宿主装配动作（业务持 engine 引用直调，不走 Op）。同名已注册 →
        ``ValueError``（registry 保证，禁静默覆盖）。
        """
        self._pinned_states.register(source)

    def unregister_pinned_state(self, name: str) -> None:
        """运行时注销 pinned 状态源；不存在 → ``KeyError``（显式失败）。"""
        self._pinned_states.unregister(name)

    @property
    def entry_skill(self) -> SkillDefinition:
        return self._entry_skill

    @property
    def budget(self) -> ContextBudget:
        """当前 ContextBudget；运行时通过 ``submit(UpdateBudget(...))`` 调整。"""
        return self._budget

    @property
    def snapshot(self) -> SkillSnapshot:
        return self._snapshot

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    @property
    def max_parallel_tool_calls(self) -> int:
        """单 turn 内一批 tool call 的最大并发数（构造期注入；默认 1=串行）。"""
        return self._max_parallel_tool_calls

    @property
    def cache_stats(self) -> PromptCacheStats:
        """跨 turn 累积的 prompt cache 统计（命中/失效/非预期破坏次数等）。

        G-CACHE：业务侧据此观测 cache 健康度；``unexpected_cache_breaks``
        高即说明有未归因的 cache 失效，需排查 provider/transport。
        """
        return self._cache_stats

    def instructions_snapshot(self) -> list[ResolvedInstruction]:
        """返回最近一次 resolve 的 ResolvedInstruction 列表（按 priority 升序）。

        spec Requirement (外部读取):
            - 返回 frozen dataclass 列表副本（业务侧修改不影响内部状态）。
            - engine 尚未跑过任何 turn 时，仅含 engine scope 的层。
            - 跑过 turn 后，含 engine + session + 最近一次 turn 解析结果。
        """
        if self._last_resolved:
            return list(self._last_resolved)
        # 未跑过 turn → 退回 engine scope 缓存
        return list(self._engine_scope_resolved)

    def history_snapshot(self) -> list[ResponseItem]:
        """返回当前 in-memory history 的快照副本（业务侧只读）。"""
        return list(self._history)

    def rewind_nodes(self) -> list[RewindCheckpoint]:
        """返回最近一次 root turn 的回访节点表（业务侧只读，供 UI 渲染可点节点）。

        节点含 turn_root / iteration / dispatch 三类;业务侧据 node_id 提交
        ``Rewind`` Op 回退到任一节点。turn 结束随状态回写,新 turn 会刷新本表。
        """
        return list(self._rewind_checkpoints)

    async def rewind_nodes_for(self, thread_id: str) -> list[RewindCheckpoint]:
        """按 thread_id 查询 rewind 节点表(thread-addressable rewind 的只读入口)。

        - 根 thread:直接返回内存表(等价 ``rewind_nodes()``,零 IO);
        - 其他 thread(典型为 detached spawn 子 thread):load raw store 项 →
          ``reconstruct_logical_history`` 折叠压缩区间/重放 rewind marker 的
          cut_index → ``derive_rewind_log`` 派生节点表。**禁止对 raw 直接
          derive**——raw 含被折叠/被截断的废弃项,坐标会错位(design D3)。

        Args:
            thread_id: 目标 thread;不存在的 thread 自然得到空表(load 空)。

        Returns:
            该 thread 的可寻址节点列表(turn_root / iteration / dispatch)。
        """
        if thread_id == self._thread_id:
            return list(self._rewind_checkpoints)
        raw = await self._load_thread_items(thread_id)
        return derive_rewind_log(reconstruct_logical_history(raw))

    def estimate_tokens(self) -> int:
        """估算当前 history 的 token 占用 —— 业务侧可据此决定是否 CompactNow。"""
        from taifeng.context.budget import estimate_history_tokens

        return estimate_history_tokens(self._history)

    def usage_ratio(self) -> float:
        """当前 token 用量占 context_window 的比例（0.0 ~ 1.0+）。"""
        return self.estimate_tokens() / max(self._budget.context_window, 1)

    def introspect(self) -> dict[str, Any]:
        """K6：/proc 式只读快照 —— 在飞 turn / spawn 配额 / 资源总量一览。

        供业务侧/运维做"ps"式观测：哪些 submission 在飞（含逐条取消态）、并发 spawn 用了多少、
        会话累计 token、事件丢弃数、cache 健康度、上下文占用。纯读、无副作用。
        """
        return {
            "thread_id": self._thread_id,
            "entry_skill_id": self._entry_skill.id,
            "running": self._running,
            # 在飞 turn（_PendingTurn 的 submission_id 列表）——保留向后兼容的纯 ID 视图
            "pending_submissions": [p.submission_id for p in self._pending.values()],
            # 在飞 turn 的逐条状态：每个在飞 turn 暴露是否已被请求取消。
            # 这是参考实现（claw-code lane_board 的存活/阻塞看板）在内核侧可纯读暴露的那一半——
            # 「卡死/超时」的 staleness 阈值判定需要墙钟+策略，按 R1 留给宿主（宿主跨两次 introspect
            # 采样 + 自有时钟即可判定）；内核只负责把"取消已请求但 turn 尚未收尾"这一事实暴露出来。
            "pending": [
                {"submission_id": p.submission_id, "cancel_requested": p.cancel.is_cancelled}
                for p in self._pending.values()
            ],
            "turn_index": self._turn_index,
            # K1 spawn 配额快照（active/total/上限）
            "spawn": self._spawn_registry.snapshot(),
            # K2 会话累计 token + 上限
            "session_tokens": self._session_tokens,
            "max_session_tokens": self._max_session_tokens,
            # K4 出站事件丢弃计数
            "events_dropped": self._events_dropped,
            # 上下文占用
            "context_tokens": self.estimate_tokens(),
            "context_window": self._budget.context_window,
            # G-CACHE 健康度摘要
            "cache": {
                "hits": self._cache_stats.completion_cache_hits,
                "misses": self._cache_stats.completion_cache_misses,
                "unexpected_breaks": self._cache_stats.unexpected_cache_breaks,
            },
        }

    async def submit(self, op: Op) -> str:
        """业务侧入队接口。返回 submission_id。"""
        sub = Submission(op=op)
        if self._audit_state is not None and isinstance(sub.op, UserMessage):
            state = self._audit_state
            state.coordinator.ensure_intake_open()
            descriptor_hash = user_message_input_descriptor_hash(sub)
            prepared = None
            with suppress(TypeError, ValueError, LLMError):
                from taifeng.llm.client import model_capabilities

                prepared = prepare_user_message(
                    state,
                    sub,
                    image_input_policy=self._image_input_policy,
                    model_input_capabilities=model_capabilities(self._model_client),
                )
            if prepared is None:
                async with self._audited_admission_lock:
                    await reject_invalid_user_message(
                        state,
                        submission_id=sub.id,
                        descriptor_hash=descriptor_hash,
                    )
                raise InvalidAuditedSubmissionError(
                    sub.id,
                    descriptor_hash,
                ) from None
            async with self._audited_admission_lock:
                accepted = prepared.accept(self._next_audited_turn_index)
                return await self._submit_audited_user_message_locked(accepted)
        if self._audit_state is not None and isinstance(sub.op, CancelTurn):
            return await self._submit_audited_cancel_turn(sub)
        if self._audit_state is not None and isinstance(sub.op, Shutdown):
            return await submit_audited_shutdown(
                self._audit_state, sub, self._audited_admission_lock, self._audit_finish_owner)
        if self._audit_state is not None:
            # audit 动态门：仅 UserMessage/CancelTurn/Shutdown 允许；能力面外的 Op
            # 在执行前 durable 安全拒绝，不入队、不执行（spec 动态未支持操作）。
            async with self._audited_admission_lock:
                await reject_unsupported_audited_op(self._audit_state, sub)
            raise UnsupportedAuditedOperationError(sub.id, str(sub.op.kind))
        if isinstance(sub.op, UserMessage):
            # legacy path 在 enqueue 与 durable append 前完成图片准入。
            from taifeng.llm.client import model_capabilities
            from taifeng.loop.prompt import history_to_api_messages

            candidate = user_message(
                sub.op.text,
                thread_id=self._thread_id,
                attachments=sub.op.attachments,
            )
            history_to_api_messages(
                [candidate],
                image_input_policy=self._image_input_policy,
                model_capabilities=model_capabilities(self._model_client),
            )
        await self._submissions.put(sub)
        return sub.id

    async def _submit_audited_cancel_turn(self, sub: Submission) -> str:
        """healthy 时 durable 收敛 CancelTurn；frozen 时仅安全取消。"""
        assert self._audit_state is not None
        assert isinstance(sub.op, CancelTurn)
        state = self._audit_state
        if state.coordinator.health is AuditHealth.RECOVERY_REQUIRED:
            state.coordinator.cancel_target(sub.op.submission_id)
            await self._emit_cancel_turn_log(
                sub.id,
                sub.op.submission_id,
                result_status="safe_degraded",
            )
            return sub.id
        state.coordinator.ensure_intake_open()
        submission = AuditedCancelTurnSubmission(
            submission_id=sub.id,
            target_submission_id=sub.op.submission_id,
        )
        result = await apply_cancel_turn(
            state,
            submission,
            self._audited_admission_lock,
        )
        await self._emit_cancel_turn_log(
            sub.id,
            sub.op.submission_id,
            result_status=result.result_status,
        )
        return sub.id

    async def _emit_cancel_turn_log(
        self,
        cancel_submission_id: str,
        target_submission_id: str,
        *,
        result_status: str,
    ) -> None:
        """通过既有 EventMsg 通道投影 CancelTurn 结果。"""
        await self._emit(
            EventMsg(
                submission_id=cancel_submission_id,
                msg=EngineLog(
                    data={
                        "level": "info",
                        "message": f"cancel turn result: {result_status}",
                        "extra": {"target_submission_id": target_submission_id},
                    }
                ),
            )
        )

    async def _submit_audited_user_message(
        self,
        sub: AuditedUserMessageSubmission,
    ) -> str:
        """提交审计专用 frozen submission；historical receipt 不入队。"""
        async with self._audited_admission_lock:
            return await self._submit_audited_user_message_locked(sub)

    async def _submit_audited_user_message_locked(
        self,
        sub: AuditedUserMessageSubmission,
    ) -> str:
        """在单一 admission 顺序点 durable accept，并推进下一 index。"""
        assert self._audit_state is not None
        admission = await admit_user_message(self._audit_state, sub)
        if isinstance(admission, AcceptedUserMessage):
            self._next_audited_turn_index = max(
                self._next_audited_turn_index,
                sub.accepted_turn_index + 1,
            )
            await handoff_accepted_user_message(
                self._audit_state,
                self._audited_mailbox,
                self._submissions,
                admission,
            )
        return sub.id

    def _new_subscriber(self) -> _Subscriber:
        """按当前队列容量/水位配置新建一个订阅者。"""
        return _Subscriber(
            maxsize=self._event_queue_size,
            high_ratio=self._event_high_water_ratio,
            low_ratio=self._event_low_water_ratio,
        )

    async def subscribe_all_envelopes(self) -> AsyncIterator[DeliveredEvent]:
        """订阅本 engine 的全部事件（firehose），产出带 ``delivery_seq`` 的信封。

        审计可观测 层1：消费者凭 ``delivery_seq`` 从 0 起的连续性自检**自己**漏没漏
        （含「刚订阅就被丢弃」的窗口）；凭 ``event.seq`` 做全局连续性 + 组落库键。
        """
        sub = self._new_subscriber()
        self._all_subs.append(sub)
        try:
            while True:
                env = await sub.queue.get()
                yield env
                if env.event.msg.kind == "shutdown":
                    return
        finally:
            with suppress(ValueError):
                self._all_subs.remove(sub)

    async def subscribe_all(self) -> AsyncIterator[EventMsg]:
        """订阅本 engine 的全部事件（向后兼容：产出裸 ``EventMsg``）。

        需 per-subscriber 投递序号自检时改用 ``subscribe_all_envelopes``。
        """
        async for env in self.subscribe_all_envelopes():
            yield env.event

    async def subscribe_envelopes(
        self, submission_id: str
    ) -> AsyncIterator[DeliveredEvent]:
        """订阅指定 submission 的事件，产出带 ``delivery_seq`` 的信封。

        ⚠️ 过滤订阅只收一个 submission 的事件，全局 ``event.seq`` 天然跳号（=过滤，
        非丢弃）；要自检自己的丢弃**必须**看 ``delivery_seq`` 跳号。
        """
        sub = self._new_subscriber()
        self._event_subs[submission_id] = sub
        try:
            while True:
                env = await sub.queue.get()
                if env.event.submission_id != submission_id:
                    continue
                yield env
                # turn_suspended 是独立终结态(turn 已结束，等待 Resume)——必须纳入自动
                # 终止集合，否则 turn 挂起时消费者的 async for 永远拿不到终结信号、卡死，
                # 业务也无法释放实例并提交 Resume(Task 16 回归根因)。
                if env.event.msg.kind in (
                    "turn_completed",
                    "turn_failed",
                    "turn_suspended",
                    "shutdown",
                ):
                    return
        finally:
            self._event_subs.pop(submission_id, None)

    async def subscribe(self, submission_id: str) -> AsyncIterator[EventMsg]:
        """订阅指定 submission 的事件（向后兼容：产出裸 ``EventMsg``）。完成后自动结束。

        需 per-subscriber 投递序号自检时改用 ``subscribe_envelopes``。
        """
        async for env in self.subscribe_envelopes(submission_id):
            yield env.event

    async def shutdown(self) -> None:
        """请求 actor 收敛；audit path 先与 admission 串行关闭 intake。"""
        if self._audit_state is None:
            await self.submit(Shutdown())
            return
        async with self._audited_admission_lock:
            lifecycle = await self._audit_state.coordinator.close_intake()
            if lifecycle is SessionLifecycle.CLOSED or self._audited_shutdown_enqueued:
                return
            await self._submissions.put(shutdown_submission(self._audit_state))
            self._audited_shutdown_enqueued = True
    # -----------------------------------------------------------------
    # Instructions: emit bridge + engine scope warmup
    # -----------------------------------------------------------------
    # event kind → EventMsg 子类映射（resolver 用字符串 kind 触发；engine 包成 EventMsg）
    _INSTRUCTION_KIND_TO_MSG: dict[str, type] = {
        "instruction_fetched": InstructionFetched,
        "instruction_cache_hit": InstructionCacheHit,
        "instruction_fetch_failed": InstructionFetchFailed,
        "instruction_updated": InstructionUpdated,
        "instruction_update_rejected": InstructionUpdateRejected,
    }

    async def _instruction_emit_bridge(
        self, kind: str, data: dict[str, Any],
    ) -> None:
        """resolver 用的 emit 回调：把 (kind, data) 包成 EventMsg 投递。"""
        msg_cls = self._INSTRUCTION_KIND_TO_MSG.get(kind)
        if msg_cls is None:
            logger.warning("unknown instruction event kind: %s", kind)
            return
        ev = EventMsg(
            submission_id=self._current_emit_submission_id,
            msg=msg_cls(data=data),
        )
        await self._emit(ev)

    async def warmup_engine_scope(self) -> None:
        """启动期解析 engine scope 的层（EnginePool.create 之后业务侧调）。

        无 resolver 时 no-op。失败时 fail-fast（raise InstructionFetchError）。
        """
        if self._instruction_resolver is None:
            return
        if not self._instruction_resolver.has_scope("engine"):
            return
        ctx = InstructionContext(
            session_id=self._session_id,
            thread_id=self._thread_id,
            entry_skill_id=self._entry_skill.id,
            turn_index=0,
            metadata=self._request_metadata,
            cancel=None,
        )
        self._engine_scope_resolved = await self._instruction_resolver.resolve(
            "engine", ctx,
        )

    # -----------------------------------------------------------------
    # Internal: emit
    # -----------------------------------------------------------------

    async def _emit(self, ev: EventMsg) -> None:
        # suspension-ttl:借唯一事件总线做定时器簿记——所有层级 turn(根/子/spawn)的
        # 挂起与核销事件都流经此处,单点覆盖,无需在各续跑路径埋点。
        kind = ev.msg.kind
        if kind == "turn_suspended":
            self._arm_ttl_timer(ev.msg.data)
        elif kind == "suspension_resolved":
            # 人工(或上一轮自动)核销 → 撤销该 record 的定时器(先核销者胜)
            timer = self._ttl_timers.pop(ev.msg.data.get("record_id", ""), None)
            if timer is not None:
                timer.cancel()
        # 审计可观测 层1：全局 seq 在入口同步分配（asyncio 单线程、本函数无 await
        # 让出点 → 并发多 turn/spawn 下原子、不重不漏）。同一 ev 广播给所有订阅，
        # 全局 seq 对各订阅一致；per-subscriber 的 delivery_seq 由 _deliver 各自记。
        ev.seq = self._seq
        self._seq += 1
        # 广播给 all subs（firehose）
        for sub in list(self._all_subs):
            self._deliver(sub, ev)
        # 投递给 per-submission sub（过滤订阅）
        per = self._event_subs.get(ev.submission_id)
        if per is not None:
            self._deliver(per, ev)

    def _deliver(self, sub: _Subscriber, ev: EventMsg) -> None:
        """把事件投递给单个订阅者：分配 per-subscriber delivery_seq（含丢弃烧号）→
        入队 → 失败计数 → 高/低水位告警。永不阻塞主 actor（put_nowait，R4）。

        delivery_seq 即使 QueueFull 丢弃也照常 +1（烧号），使订阅者凭自己收到的
        delivery_seq 跳号即可精确自检「我漏了几条」，与全局 seq 跳号互不混淆。
        """
        n = sub.next_delivery
        sub.next_delivery += 1
        try:
            sub.queue.put_nowait(DeliveredEvent(event=ev, delivery_seq=n))
        except asyncio.QueueFull:
            # K4：不再静默丢——累计计数 + WARNING（consumer 另可凭 delivery_seq 跳号
            # 精确自检）。不阻塞 emit（慢/缺席 consumer 不得拖死主 actor，R4）。
            self._events_dropped += 1
            logger.warning("event queue full, drop event %s", ev.msg.kind)
        self._maybe_warn_water(sub)

    def _maybe_warn_water(self, sub: _Subscriber) -> None:
        """有界队列堆积告警：qsize 上穿高水位告一条 WARNING，回落到低水位以下才
        重新武装（迟滞）；告警另受 ``event_warn_cooldown_sec`` 限频。无界队列不告警。

        ⚠️ 告警走 logger 而非 emit 事件——告警事件本身也会进所有队列，堆积时会
        自我放大成告警风暴。
        """
        if sub.high_water is None:  # 无界队列：无容量百分比可言，不告警
            return
        qsize = sub.queue.qsize()
        if qsize >= sub.high_water and not sub.warned:
            now = self._now_factory()
            if sub.last_warn is None or now - sub.last_warn >= self._event_warn_cooldown_sec:
                logger.warning(
                    "event queue high-water: %d/%d (subscriber lagging)",
                    qsize,
                    self._event_queue_size,
                )
                sub.last_warn = now
            sub.warned = True
        elif sub.low_water is not None and qsize <= sub.low_water and sub.warned:
            sub.warned = False  # 回落到低水位以下 → 重新武装下次告警

    @property
    def events_dropped(self) -> int:
        """K4：累计因订阅队列满而丢弃的事件数（0 = 无丢弃）。

        业务侧观测：>0 说明某订阅消费过慢、漏了事件——应加大 ``event_queue_size``
        或更快 drain。lossy-but-accounted：内核绝不为慢 consumer 阻塞主 actor。
        """
        return self._events_dropped

    # -----------------------------------------------------------------
    # suspension-ttl：挂起到期自动裁决（热武装 / 到期触发 / 冷重武装）
    # -----------------------------------------------------------------

    def _start_operation(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        """创建并登记 Engine-owned operation，终态总会检索异常。"""
        task = asyncio.create_task(
            coroutine,
            name=f"engine-operation:{self._session_id}:{name}",
        )
        self._operation_tasks.add(task)
        task.add_done_callback(self._forget_operation)
        return task

    def _forget_operation(self, task: asyncio.Task[None]) -> None:
        """检索 operation 终态并释放 Engine 显式 ownership。"""
        self._operation_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException as exc:  # noqa: BLE001
            logger.error(
                "engine operation task failed: %s",
                task.get_name(),
                exc_info=exc,
            )

    async def _converge_operations(self) -> asyncio.CancelledError | None:
        """取消并等待所有 operation；actor 自身取消也不得截断收敛。"""
        actor_cancellation: asyncio.CancelledError | None = None
        while self._operation_tasks:
            tasks = tuple(self._operation_tasks)
            for task in tasks:
                task.cancel()
            waiter = asyncio.gather(*tasks, return_exceptions=True)
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError as exc:
                    actor_cancellation = actor_cancellation or exc
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    for task in tasks:
                        task.cancel()
            waiter.result()
            # Python 3.13 对全 done futures 的 gather 可 eager 完成，不保证让
            # _forget_operation callback 先运行；此处同步释放 ownership，避免
            # 因 done task 仍留在 set 中形成无 await 的 busy loop。
            for task in tasks:
                self._operation_tasks.discard(task)
        return actor_cancellation

    def _arm_ttl_timer(self, data: dict[str, Any]) -> None:
        """按 turn_suspended 事件武装到期定时器(expires_at 为 None 则不武装)。

        delay ≤ 0(装载时已过期)同样入队,任务体内立即触发裁决。重复武装同一
        record(冷重武装 + 热事件竞态)以先到者为准,后到 no-op。
        """
        record_id = data.get("record_id")
        expires_at = data.get("expires_at")
        thread_id = data.get("thread_id")
        if not record_id or expires_at is None or not thread_id:
            return
        if record_id in self._ttl_timers:
            return
        delay = max(0, int(expires_at) - int(self._now_factory()))
        self._ttl_timers[record_id] = self._start_operation(
            self._ttl_expire_after(delay, str(thread_id), str(record_id)),
            name=f"ttl:{record_id}",
        )

    async def _ttl_record_active(
        self, thread_id: str, record_id: str,
    ) -> SuspensionRecord | None:
        """到期任务的活跃性验证:record 仍活跃且不在飞 → 返回 record,否则 None。

        「先核销者胜」的单点判定:人工 Resume 已核销(marker 落盘)或正在处理
        (在飞占位)时,到期裁决让位。根 thread 读内存 history,其余 load store。
        """
        items = (
            list(self._history) if thread_id == self._thread_id
            else await self._load_thread_items(thread_id)
        )
        record = self._find_active_suspension_in(items)
        if record is None or record.record_id != record_id:
            return None
        if record_id in self._resolving_records:
            return None
        return record

    async def _ttl_expire_after(
        self, delay: float, thread_id: str, record_id: str
    ) -> None:
        """到期任务体:睡到 deadline → 验证 record 仍活跃 → emit + 内核签发 auto-Resume。

        先核销者胜:触发时重读该 thread 的活跃挂起,record 已被人工 Resume 核销
        (或不是同一条)→ no-op。auto-Resume 用 EXPIRE_SENTINEL payload 经公共
        Resume Op 提交 —— root / call_skill 嵌套 / spawn 三条续跑链零改动全复用。
        """
        from taifeng.suspend.resolver import EXPIRE_EXHAUSTED, EXPIRE_SENTINEL

        try:
            if delay > 0:
                await asyncio.sleep(delay)
            # 验证活跃性 + 在飞让位(先核销者胜)
            record = await self._ttl_record_active(thread_id, record_id)
            if record is None:
                return
            # 路由解析(fire 时):spawn 嵌套 leaf 的 thread_id 不可直接路由
            # (match_suspended_spawn 只认 spawn 子 tid)→ 解析可路由入口。
            # 挂起上浮(leaf → 上层 CHILD_SKILL / spawn 句柄置 suspended)有毫秒级
            # 窗口,fire 紧贴挂起时(短 ttl / 冷装载即过期)可能撞上 → 有界重试消化
            route_tid: str | None = None
            for _ in range(20):
                route_tid = await self._resolve_expiry_route(thread_id, record_id)
                if route_tid is not None:
                    break
                await asyncio.sleep(0.05)
            if route_tid is None:
                # 退避重武装(suspend-review-fixes D3):到期是事实,路由不可达只是
                # 时序(挂起上浮未完成 / 批内兄弟在跑)。放弃版的「冷装载补」对长
                # 生命进程是永不发生的承诺;2s 后重试,人工核销/Shutdown 经既有
                # 取消路径终止,纯内存检查无 token 成本。
                logger.warning(
                    "suspension ttl expiry unroutable, rearming in 2s: "
                    "record=%s thread=%s", record_id, thread_id)
                self._ttl_timers.pop(record_id, None)
                self._arm_ttl_timer({
                    "record_id": record_id,
                    "thread_id": thread_id,
                    "expires_at": int(self._now_factory()) + 2,
                })
                return
            # 重试等待期间可能已被人工 Resume 核销/占位 → 让位(先核销者胜)
            record = await self._ttl_record_active(thread_id, record_id)
            if record is None:
                return
            items = (
                list(self._history) if thread_id == self._thread_id
                else await self._load_thread_items(thread_id)
            )
            # 内核签发到期裁决:只对**未核销** pending 配哨兵(request 级核销下,
            # 已部分结算的 pending 再发哨兵会产生重复 fco)
            pending_left = self._unsettled_pendings(record, items)
            if not pending_left:
                return
            # 自动 retry 谱系熔断(resource-limit-retry-semantics):任一 retry 位
            # pending 的谱系计数已达上限 → 本次裁决强制 abort,终止无界自动循环
            max_auto = self._failure_suspend_max_auto_retries
            exhausted = max_auto is not None and any(
                p.on_expire == "retry"
                and int(p.detail.get("auto_retry_count", 0) or 0) >= max_auto
                for p in pending_left)
            expired_data: dict[str, Any] = {
                "record_id": record_id,
                "thread_id": thread_id,
                "on_expire": ",".join(sorted(
                    {p.on_expire for p in record.pending})),
                "reasons": sorted({p.reason.value for p in record.pending}),
            }
            if exhausted:
                expired_data["auto_retry_exhausted"] = True
            await self._emit(EventMsg(
                submission_id=record_id,
                msg=SuspensionExpired(data=expired_data),
            ))
            sentinel: dict[str, Any] = {EXPIRE_SENTINEL: True}
            if exhausted:
                sentinel[EXPIRE_EXHAUSTED] = True
            await self.submit(Resume(
                thread_id=route_tid,
                resolutions={p.request_id: dict(sentinel) for p in pending_left},
            ))
        except asyncio.CancelledError:
            # 人工 Resume 先到 / shutdown:正常撤销,非异常
            raise
        except Exception:
            logger.exception("suspension ttl expiry crashed: %s", record_id)
        finally:
            # 只弹自身:路由失败分支已重武装新 task,不可被旧 task 的收尾误弹
            if self._ttl_timers.get(record_id) is asyncio.current_task():
                self._ttl_timers.pop(record_id, None)

    async def _resolve_expiry_route(
        self, thread_id: str, record_id: str,
    ) -> str | None:
        """到期裁决的路由解析(fire 时):返回可路由的 Resume.thread_id。

        武装时只记原 thread_id;leaf 挂起事件先于上层 CHILD_SKILL/spawn 挂起,
        提前解析有时序窗口,故推迟到 fire 时全量判定:
        1. 根 thread → 直接可路由;
        2. call_skill 链(挂根):根链可下探到该 thread → 原 thread_id 可路由
           (_handle_child_resume 自根寻址);
        3. spawn 拓扑:该 thread 是 spawn 子自身,或埋在某挂起态 spawn 句柄的
           子链中 → 以 **spawn 子 tid** 提交(与人工 Resume 约定一致,
           resume_spawn_nested 自动下探);
        4. 均未命中 → None(调用方 log + no-op,冷装载重武装再试)。
        """
        if thread_id == self._thread_id:
            return thread_id
        if await self._build_resume_chain(thread_id) is not None:
            return thread_id
        for h in self._spawn.suspended_handles():
            if h.status != "suspended":
                continue
            if h.child_thread_id == thread_id:
                return thread_id  # spawn 直接挂起(既有可路由形态)
            if await self._chain_contains_thread(
                    h.child_thread_id, thread_id,
                    self._max_total_spawns_guard()):
                return h.child_thread_id
        return None

    async def _chain_contains_thread(
        self, root_tid: str, target_tid: str, depth: int,
    ) -> bool:
        """自 root_tid 沿活跃挂起的 CHILD_SKILL pending DFS,判定子链是否含 target。"""
        from taifeng.suspend.reason import SuspendReason

        if root_tid == target_tid:
            return True
        if depth <= 0:
            return False
        items = await self._load_thread_items(root_tid)
        record = self._find_active_suspension_in(items)
        if record is None:
            return False
        for pend in record.pending:
            if pend.reason is not SuspendReason.CHILD_SKILL:
                continue
            child_tid = pend.detail.get("sub_thread_id")
            if isinstance(child_tid, str) and await self._chain_contains_thread(
                    child_tid, target_tid, depth - 1):
                return True
        return False

    async def _rearm_ttl_timers_cold(self) -> None:
        """冷启动重武装(R5):扫根 history + 挂起态 spawn 子 thread 的活跃挂起。

        已过期 → delay=0 立即裁决;未过期 → 按剩余壁钟时长重武装。旧 JSONL 无
        ttl 字段 → expires_at 为 None,不武装(永不过期,前向兼容)。深层
        call_skill leaf 的 ttl 由其自身 turn_suspended 热路径覆盖;冷恢复场景
        覆盖根 + spawn 两类 engine 可枚举的 thread(v1 边界,见能力契约)。
        """
        record = self._find_active_suspension()
        if record is not None and record.expires_at is not None:
            self._arm_ttl_timer({
                "record_id": record.record_id,
                "thread_id": self._thread_id,
                "expires_at": record.expires_at,
            })
        # 挂起态 spawn 句柄:子 thread 的活跃挂起带 ttl 的一并重武装
        for h in self._spawn.suspended_handles():
            if h.status != "suspended":
                continue
            try:
                items = await self._load_thread_items(h.child_thread_id)
            except Exception:
                logger.exception(
                    "ttl cold rearm: load spawn thread failed: %s",
                    h.child_thread_id)
                continue
            rec = self._find_active_suspension_in(items)
            if rec is not None and rec.expires_at is not None:
                self._arm_ttl_timer({
                    "record_id": rec.record_id,
                    "thread_id": h.child_thread_id,
                    "expires_at": rec.expires_at,
                })

    def _cancel_ttl_timers(self) -> None:
        """shutdown:取消全部到期定时器(R4,不阻塞主 actor)。"""
        for timer in self._ttl_timers.values():
            timer.cancel()
        self._ttl_timers.clear()

    async def _memory_session_end(self) -> None:
        """K3 teardown：shutdown 时调 memory_store.on_session_end。best-effort。"""
        if self._memory_store is None:
            return
        try:
            await self._memory_store.on_session_end(
                thread_id=self._thread_id, items=list(self._history)
            )
        except Exception:
            logger.exception("memory on_session_end failed (ignored)")

    async def _finalize_run_lifecycle(
        self,
        cancel: CancellationToken,
        *,
        shutdown_requested: bool,
    ) -> None:
        """按原顺序收敛 actor、operation、持久化 flush 与订阅者终态。"""
        self._running = False
        if self._audit_state is not None:
            self._audit_state.coordinator.cancel_session_root()
            await finalize_audited_mailbox(
                self._audit_state,
                self._audited_mailbox,
            )
        cancel.cancel()
        self._cancel_ttl_timers()
        actor_cancellation = await self._converge_operations()
        spawn_cancellation = await self._spawn.converge_owned_tasks()
        actor_cancellation = actor_cancellation or spawn_cancellation
        if shutdown_requested:
            await self._memory_session_end()
        # 通知所有 subscriber 退出：经统一投递路径，shutdown 也获全局 seq +
        # per-subscriber delivery_seq（保持两个序号在退出事件上同样连续可自检）。
        shutdown_ev = EventMsg(submission_id="*", msg=ShutdownMsg())
        shutdown_ev.seq = self._seq
        self._seq += 1
        for subscriber in list(self._all_subs):
            self._deliver(subscriber, shutdown_ev)
        if actor_cancellation is not None:
            raise actor_cancellation

    # -----------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------

    async def run(self, cancel: CancellationToken) -> None:
        self._running = True
        shutdown_requested = False
        # detached-spawn：记下根取消 token，供 spawn 的分离 task 派生子 token（R4 可取消）。
        self._root_cancel = cancel
        # suspension-ttl 冷重武装(R5):装载的历史里有带 ttl 的活跃挂起 →
        # 已过期立即裁决、未过期按剩余时长武装。pool 冷恢复在 run 启动前已
        # rebuild spawn 句柄,此处可一并枚举挂起态 spawn 子 thread。
        try:
            await self._rearm_ttl_timers_cold()
            while self._running:
                if cancel.is_cancelled:
                    break
                try:
                    sub = await asyncio.wait_for(self._submissions.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(sub.op, Shutdown):
                    self._running = False
                    shutdown_requested = True
                    # suspension-ttl:取消全部到期定时器(R4,定时器挂 engine 生命周期)
                    self._cancel_ttl_timers()
                    await self._emit(
                        EventMsg(submission_id=sub.id, msg=ShutdownMsg())
                    )
                    break
                if isinstance(sub.op, CancelTurn):
                    target = self._pending.get(sub.op.submission_id)
                    if target is not None:
                        target.cancel.cancel()
                        await self._emit(
                            EventMsg(
                                submission_id=sub.id,
                                msg=EngineLog(
                                    data={
                                        "level": "info",
                                        "message": f"cancelled turn {sub.op.submission_id}",
                                        "extra": {},
                                    }
                                ),
                            )
                        )
                    else:
                        # R4：挂起态没有 live pending（turn 已退栈），CancelTurn 需
                        # 按 submission_id 匹配并清除活跃挂起 record（闭环可取消）。
                        await self._cancel_active_suspension(
                            sub.id, sub.op.submission_id
                        )
                    continue
                if isinstance(sub.op, InjectSystemMessage):
                    item = system_injection(
                        sub.op.text, thread_id=self._thread_id, source=sub.op.source
                    )
                    self._history.append(item)
                    await self._store.append(item)
                    continue
                if isinstance(sub.op, SendToPeer):
                    # peer-mailbox：与 send_message 工具收敛到同一投递路径。
                    # 寻址失败 / TriggerTurn 打 root → EngineLog 告警（显式，不静默）。
                    try:
                        await self.deliver_peer_message(
                            target=sub.op.target_thread_id,
                            text=sub.op.text,
                            mode=sub.op.mode,
                            from_thread_id=sub.op.from_thread_id,
                            submission_id=sub.id,
                        )
                    except ValueError as e:
                        await self._emit(
                            EventMsg(
                                submission_id=sub.id,
                                msg=EngineLog(data={
                                    "level": "warning",
                                    "message": f"send_to_peer 投递失败: {e}",
                                    "extra": {
                                        "target": sub.op.target_thread_id,
                                        "mode": sub.op.mode,
                                    },
                                }),
                            )
                        )
                    continue
                if isinstance(sub.op, InjectUserInput):
                    # B1 midturn-input-steering：投进活跃 turn 的 pending 队列（下一
                    # 迭代边界 drain 并入）；无活跃 turn → 落历史不起新 turn。
                    target = self._pending.get(sub.op.submission_id)
                    item = user_message(sub.op.text, thread_id=self._thread_id)
                    if target is not None:
                        # 活跃 turn：入共享队列，由 runner drain 时落 store + emit
                        target.pending_input.append(item)
                        delivered = True
                    else:
                        # 无活跃 turn：engine 落历史 + 持久化，不创建 TurnRunner
                        self._history.append(item)
                        await self._store.append(item)
                        delivered = False
                    await self._emit(
                        EventMsg(
                            submission_id=sub.id,
                            msg=UserInputInjected(
                                data={
                                    "submission_id": sub.op.submission_id,
                                    "delivered": delivered,
                                    "text_preview": sub.op.text[:80],
                                }
                            ),
                        )
                    )
                    continue
                if isinstance(sub.op, CompactNow):
                    # 走 manual 路径 —— 启动一个不带 user message 的特殊 turn
                    await self._run_compact_now(sub.id, sub.op, cancel)
                    continue
                if isinstance(sub.op, ThreadRollback):
                    await self._handle_rollback(sub.id, sub.op.num_turns)
                    continue
                if isinstance(sub.op, UpdateBudget):
                    self._handle_update_budget(sub.id, sub.op)
                    continue
                if isinstance(sub.op, RefreshSnapshot):
                    self._handle_refresh_snapshot(sub.id)
                    continue
                if isinstance(sub.op, UpdateInstructions):
                    await self._handle_update_instructions(sub.id, sub.op)
                    continue
                if isinstance(sub.op, Rewind):
                    # 与 Resume 同理用 create_task：重推会跑完整 turn(采样 + 派发),
                    # 不阻塞主 run 循环,且给 subscribe(submission_id) 留注册窗口。
                    self._start_operation(
                        self._handle_rewind(sub, cancel),
                        name=f"rewind:{sub.id}",
                    )
                    continue
                if isinstance(sub.op, Resume):
                    # detached spawn 续跑优先判定：Resume.thread_id 命中某个【挂起】的
                    # spawn 句柄 child_thread_id → 走 _resume_spawn（在该 child thread
                    # 自己的线上独立续跑，与父 turn 完全解耦；父 turn 早已结束）。
                    # 不命中（根 thread / call_skill 子链）→ 维持既有 _handle_resume。
                    spawn_handle = self._match_suspended_spawn(sub.op.thread_id)
                    if spawn_handle is not None:
                        self._start_operation(
                            self._resume_spawn(sub, spawn_handle),
                            name=f"resume-spawn:{sub.id}",
                        )
                        continue
                    # 与 UserMessage 一致用 create_task 异步派发：让续跑链（可能跨子/根
                    # 多个 turn）不阻塞主 run 循环，且给 subscribe(submission_id) 留出在
                    # 事件流出前注册队列的窗口（子 thread resume 续跑链 emit 多个事件，
                    # 内联执行会与"submit 后再 subscribe"的消费者抢跑导致丢首批事件→挂死）。
                    self._start_operation(
                        self._handle_resume(sub, cancel),
                        name=f"resume:{sub.id}",
                    )
                    continue
                if self._is_queued_user_message(sub):
                    await self._start_queued_user_message(sub, cancel)
                    continue
        finally:
            await self._finalize_run_lifecycle(
                cancel,
                shutdown_requested=shutdown_requested,
            )

    @staticmethod
    def _is_queued_user_message(
        sub: Submission | AcceptedUserMessage,
    ) -> bool:
        """统一识别 legacy UserMessage 与 durable accepted token。"""
        return isinstance(sub, AcceptedUserMessage) or isinstance(sub.op, UserMessage)

    async def _start_queued_user_message(
        self,
        sub: Submission | AcceptedUserMessage,
        root_cancel: CancellationToken,
    ) -> None:
        """按 queue item 类型选择 durable 或 legacy turn 入口。"""
        if not isinstance(sub, AcceptedUserMessage):
            self._start_operation(
                self._run_turn_for(sub, root_cancel),
                name=f"turn:{sub.id}",
            )
            return
        if not await self._audited_mailbox.claim(sub):
            return
        application_checkpoint = AuditedApplicationCheckpoint()
        self._start_operation(
            self._run_claimed_audited_turn(
                sub,
                root_cancel,
                application_checkpoint=application_checkpoint,
            ),
            name=f"turn:{sub.id}",
        )
        await application_checkpoint.wait()

    async def _run_claimed_audited_turn(
        self,
        token: AcceptedUserMessage,
        root_cancel: CancellationToken,
        *,
        application_checkpoint: AuditedApplicationCheckpoint | None = None,
    ) -> None:
        """handshake 后收敛 application；失败由 actor checkpoint 单点传播。"""
        failure: BaseException | None = None
        try:
            if not await self._audited_mailbox.start_claimed(token):
                return
            try:
                await self._run_audited_turn_for(
                    token,
                    root_cancel,
                    application_checkpoint=application_checkpoint,
                )
            except BaseException as error:  # noqa: BLE001
                failure = error
        finally:
            await retire_started_audited_token(
                self._audited_mailbox,
                token,
            )
        if failure is None:
            return
        if (
            application_checkpoint is not None
            and application_checkpoint.fail(failure)
        ):
            return
        raise failure

    async def _run_audited_turn_for(
        self,
        token: AcceptedUserMessage,
        root_cancel: CancellationToken,
        *,
        application_checkpoint: AuditedApplicationCheckpoint | None = None,
    ) -> None:
        """应用 ack conversation envelope；ownership 由外层 handshake/finally 管理。"""
        assert self._audit_state is not None
        await self._audit_state.coordinator.ensure_effect_allowed()
        try:
            item, conversation_envelopes = token.validated_application()
        except BaseException as error:
            raise self._audit_state.coordinator.freeze(error) from None
        async with self._lock:
            self._history.append(item)
        try:
            result = await self._audit_state.projector.project(
                conversation_envelopes, token.ack
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001  # 普通未分类异常必须 fail closed
            raise self._audit_state.coordinator.freeze(error) from None
        self._audit_state.coordinator.update_projection(result)
        if application_checkpoint is not None:
            application_checkpoint.succeed()
        target_cancel = self._audit_state.coordinator.register_target(
            token.submission_id
        )
        try:
            await self._run_turn_for(
                AuditedTurnInput(
                    id=token.submission_id,
                    text=str(item.payload["text"]),
                    accepted_turn_index=token.accepted_turn_index,
                ),
                target_cancel,
            )
            end_reason = self._audit_state.coordinator.target_outcome(
                token.submission_id,
                target_cancel,
            )
            if (
                end_reason == "cancelled"
                and self._audit_state.coordinator.target_cancel_requested(
                    token.submission_id,
                    target_cancel,
                )
            ):
                await finalize_cancelled_target(
                    self._audit_state,
                    submission_id=token.submission_id,
                    turn_index=token.accepted_turn_index,
                    target_token=target_cancel,
                )
        finally:
            self._audit_state.coordinator.unregister_target(
                token.submission_id,
                target_cancel,
            )

    async def _run_turn_for(
        self,
        sub: Submission | AuditedTurnInput,
        root_cancel: CancellationToken,
    ) -> None:
        if isinstance(sub, Submission):
            assert isinstance(sub.op, UserMessage)
            user_text = sub.op.text
            attachments = sub.op.attachments
        else:
            user_text = sub.text
            attachments = None

        # 挂起态守卫(suspend-review-fixes):根 thread 有活跃挂起 → 在落史**之前**
        # 显式拒绝新 UserMessage。挂起 = turn 停在待裁决,裁决(Resume retry/abort)
        # 是继续会话的唯一出口;放行会让新 turn 的同名编排 call_id fco 污染
        # request 级核销凭据(假核销 → TTL 静默 no-op / 幽灵续跑重放错轮),并使
        # engine 级 K2 record 可叠加成僵尸。拒绝不落史:被拒消息若入史会污染
        # 编排重放锚与续跑 seed——业务凭 thread_suspended 事件引导用户先裁决,
        # 结清后重发(消息文本在事件归因的 submission 里,未静默丢弃)。
        active_suspension = self._find_active_suspension()
        if active_suspension is not None:
            await self._emit(EventMsg(submission_id=sub.id, msg=TurnFailed(data={
                "error": "active_suspension",
                "kind": "thread_suspended",
                "record_id": active_suspension.record_id,
                "iterations": 0,
                "is_root": True,
            })))
            self._turn_index += 1
            return

        turn_cancel = root_cancel.child(f"sub:{sub.id}")
        self._pending[sub.id] = _PendingTurn(sub.id, turn_cancel, audited_turn_index(sub))

        # 把 user 消息落 buffer + 持久化
        if attachments is not None:
            item = user_message(
                user_text,
                thread_id=self._thread_id,
                attachments=attachments,
            )
            # resume/内部路径仍在 durable append 前执行 defense-in-depth 校验。
            from taifeng.llm.client import model_capabilities
            from taifeng.loop.prompt import history_to_api_messages

            history_to_api_messages(
                [item],
                image_input_policy=self._image_input_policy,
                model_capabilities=model_capabilities(self._model_client),
            )
            async with self._lock:
                self._history.append(item)
            await self._store.append(item)

        # === T4 instructions-injection ===
        # 在 turn 启动前 resolve 当前 turn 的 instructions（engine 已 warmup 过；
        # 这里 resolve engine+session+turn 三档并合并）。
        # 失败 fail-fast → 将 InstructionFetchError 转成 turn_failed 事件
        resolved_for_turn: list[ResolvedInstruction] = []
        if self._instruction_resolver is not None:
            self._current_emit_submission_id = sub.id
            ctx = InstructionContext(
                session_id=self._session_id,
                thread_id=self._thread_id,
                entry_skill_id=self._entry_skill.id,
                turn_index=self._turn_index,
                metadata=self._request_metadata,
                cancel=turn_cancel,
            )
            try:
                resolved_for_turn = await self._instruction_resolver.resolve(
                    ("engine", "session", "turn"), ctx,
                )
            except InstructionFetchError as e:
                # fail-fast: 发 turn_failed 后退出
                await self._emit(EventMsg(
                    submission_id=sub.id,
                    msg=TurnFailed(data={
                        "error": str(e),
                        "kind": "InstructionFetchError",
                        "iterations": 0,
                        # 引擎直接派发的 fail-fast 失败必属于根 turn。
                        "is_root": True,
                    }),
                ))
                self._pending.pop(sub.id, None)
                self._turn_index += 1
                return
            finally:
                self._current_emit_submission_id = "*"
            self._last_resolved = list(resolved_for_turn)

        # === pre_turn hook ===
        # 业务侧最后一道介入点：可基于 user_text + turn_index 拒绝 turn 启动。
        # 顺序约束（与 spec hooks/Requirement "pre_turn hook 调用点" 对齐）：
        #   1) user_message 已持久化（resume 友好）
        #   2) instruction resolve 已完成
        #   3) 此处 hook deny → 不创建 TurnRunner、emit turn_failed
        if self._hooks is not None:
            from taifeng.hooks.types import HookContext, PreTurnHook
            pre_decision = await self._hooks.run(
                "pre_turn",
                PreTurnHook(
                    user_text=user_text,
                    iteration=self._turn_index,
                ),
                HookContext(
                    thread_id=self._thread_id,
                    submission_id=sub.id,
                    entry_skill_id=self._entry_skill.id,
                ),
            )
            if not pre_decision.allow:
                # emit 两条事件：先 pre_turn_hook_denied（定位原因），
                # 再 turn_failed（消费 subscribe(sub_id) 的 break 条件）
                preview = user_text[:200]
                await self._emit(EventMsg(
                    submission_id=sub.id,
                    msg=PreTurnHookDenied(data={
                        "reason": pre_decision.reason or "",
                        "user_text_preview": preview,
                        "iteration": self._turn_index,
                    }),
                ))
                # 注：kind 字段约定为"真实抛出的异常类名"（如 InstructionFetchError）；
                # hook deny 不抛异常，故此处用与 event kind 一致的描述性 label
                # （详见 spec config-consistency-fixes A3）
                await self._emit(EventMsg(
                    submission_id=sub.id,
                    msg=TurnFailed(data={
                        "error": "pre_turn_hook_denied",
                        "kind": "pre_turn_hook_denied",
                        "iterations": 0,
                        # pre_turn hook 拒绝发生在 Engine 派发阶段（无 TurnRunner），
                        # 必属于根 turn。
                        "is_root": True,
                    }),
                ))
                self._pending.pop(sub.id, None)
                self._turn_index += 1
                return

        # K2 跨 turn 守卫：会话累计 token 已触顶 → 经 failure policy 裁决
        # (resource-limit-retry-semantics):TERMINAL → 拒绝开新 turn(现状);
        # SUSPEND → engine 级 RESOURCE_LIMIT 挂起,retry+extend_tokens 抬顶后续跑。
        if await self._gate_session_tokens(sub.id):
            self._pending.pop(sub.id, None)
            self._turn_index += 1
            return

        await self._build_and_run_runner(sub.id, turn_cancel, resolved_for_turn)

    async def _gate_session_tokens(self, submission_id: str) -> bool:
        """K2 引擎级闸门:触顶时按 policy 裁决终态 / 挂起。

        Returns:
            True = 本次 turn 被闸(已 emit 终态或挂起事件,调用方收尾返回);
            False = 未触顶,照常开跑。

        SUSPEND 路径:在 engine 级直接落 SuspensionRecord(user_message 已入史,
        Resume retry+extend_tokens 抬顶后经既有根续跑链跑该 turn)。
        """
        if (self._max_session_tokens is None
                or self._session_tokens < self._max_session_tokens):
            return False
        from taifeng.loop.failure_policy import (
            DEFAULT_FAILURE_POLICY,
            FailureContext,
            FailureDisposition,
        )
        policy = self._failure_policy or DEFAULT_FAILURE_POLICY
        disposition = policy.decide(FailureContext(
            origin="guard_trip",
            failure_class=None,
            end_reason="resource_limit_exceeded",
            error_kind=None,
            retryable=False,
            is_root=True,
            iteration=0,
        ))
        suspended = disposition is FailureDisposition.SUSPEND
        await self._emit(EventMsg(
            submission_id=submission_id, msg=ResourceLimitExceeded(
                data={
                    "limit_kind": "session_tokens",
                    "used": self._session_tokens,
                    "limit": self._max_session_tokens,
                    # R3 scope 如实:挂起时 turn 并未被拒杀,报 turn_suspended
                    "scope": "turn_suspended" if suspended else "turn_refused",
                })))
        if not suspended:
            await self._emit(EventMsg(submission_id=submission_id, msg=TurnFailed(
                data={
                    "error": "session_token_limit_exceeded",
                    "kind": "resource_limit_exceeded",
                    "iterations": 0,
                    "is_root": True,
                })))
            return True
        await self._suspend_engine_gate(submission_id)
        return True

    async def _suspend_engine_gate(self, submission_id: str) -> None:
        """落 engine 级 K2 挂起记录(turn 未开跑,record 直接挂根 history)。

        与 turn 内护栏挂起同形(RESOURCE_LIMIT / related_call_id=None /
        on_expire 恒 abort——自动 retry 无人携带增额必然无效);Resume
        retry+extend_tokens 经既有根续跑链(_handle_resume)直接跑该 turn。
        """
        import secrets as _secrets

        from taifeng.suspend.reason import PendingRequest, SuspendReason
        from taifeng.suspend.record import SuspensionRecord

        pending = PendingRequest(
            request_id=f"sr_{_secrets.token_hex(6)}",
            reason=SuspendReason.RESOURCE_LIMIT,
            ttl_seconds=self._failure_suspend_ttl_seconds,
            on_expire="abort",
            payload_schema={
                "type": "object",
                "properties": {"action": {"enum": ["retry", "abort"]}},
            },
            related_call_id=None,
            detail={
                "end_reason": "resource_limit_exceeded",
                "guard_snapshot": {
                    "used": self._session_tokens,
                    "limit": self._max_session_tokens,
                },
                "gate": "turn_refused",
            },
        )
        record = SuspensionRecord(
            record_id=f"sr_{_secrets.token_hex(6)}",
            thread_id=self._thread_id,
            submission_id=submission_id,
            turn_index=self._turn_index,
            pending=(pending,),
            created_at=int(self._now_factory()),
        )
        item = record.to_item()
        async with self._lock:
            self._history.append(item)
        await self._store.append(item)
        await self._emit(EventMsg(submission_id=submission_id, msg=TurnSuspended(
            data={
                "thread_id": self._thread_id,
                "record_id": record.record_id,
                "pending": item.payload["pending"],
                "cache_invalidated": True,
                "expires_at": record.expires_at,
            })))

    def _new_turn_runner(
        self,
        submission_id: str,
        turn_cancel: CancellationToken,
        resolved_for_turn: list[ResolvedInstruction],
        *,
        auto_retry_count: int = 0,
    ) -> TurnRunner:
        """从 Engine 当前快照构造单轮 runner。"""
        pending = self._pending.get(submission_id)
        pending_input = pending.pending_input if pending is not None else []
        turn_index = pending.turn_index if pending is not None else None
        return TurnRunner(
            entry_skill=self._entry_skill,
            snapshot=self._snapshot,
            model_client=self._model_client,
            tool_runtime=self._tool_runtime,
            store=self._store,
            compressors=self._compressors,
            dispatch_policy=self._dispatch_policy,
            outcome_judge=self._outcome_judge,
            budget=self._budget,
            thread_id=self._thread_id,
            submission_id=submission_id,
            emit=self._emit,
            cancel=turn_cancel,
            image_input_policy=self._image_input_policy,
            input_cost_estimator=self._input_cost_estimator,
            audit_state=self._audit_state,
            hooks=self._hooks,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            denial_breaker_config=self._denial_breaker_config,
            doom_loop_config=self._doom_loop_config,
            failure_policy=self._failure_policy,
            failure_suspend_ttl_seconds=self._failure_suspend_ttl_seconds,
            failure_suspend_on_expire=self._failure_suspend_on_expire,
            auto_retry_count=auto_retry_count,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            reasoning_passback=self._reasoning_passback,
            enable_request_capture=self._enable_request_capture,
            history_buffer=list(self._history),
            pending_input=pending_input,
            cache_anchor_index=self._cache_anchor_index,
            instructions=list(resolved_for_turn),
            permission_policy=self._permission_policy,
            request_metadata=self._request_metadata,
            turn_index=self._turn_index if turn_index is None else turn_index,
            capabilities=self._capabilities,
            recall_threshold=self._recall_threshold,
            has_recall_backend=self._has_recall_backend,
            spawn_registry=self._spawn_registry,
            cache_stats=self._cache_stats,
            last_prompt_fingerprint=self._last_prompt_fingerprint,
            compaction_count=self._compaction_count,
            compaction_degradation_threshold=self._compaction_degradation_threshold,
            session_tokens_used=self._session_tokens,
            max_session_tokens=self._max_session_tokens,
            memory_store=self._memory_store,
            memory_query_builder=self._memory_query_builder,
            pinned_states=self._pinned_states,
            spawn_coordinator=self,
        )

    async def _writeback_turn_runner(self, runner: TurnRunner) -> None:
        """完整验证 audited history 后原子回写 runner 派生状态。"""
        async with self._lock:
            runner_history = list(runner.history_buffer)
            if self._audit_state is None:
                self._history = runner_history
            else:
                try:
                    merged_history = merge_audited_history(
                        self._history,
                        runner_history,
                    )
                except AuditedHistoryConflictError:
                    raise self._audit_state.coordinator.freeze(
                        audited_history_conflict_failure()
                    ) from None
                self._history = merged_history
            self._cache_anchor_index = runner.cache_anchor_index
            self._rewind_checkpoints = derive_rewind_log(self._history)
            self._last_prompt_fingerprint = runner.last_prompt_fingerprint
            self._compaction_count = runner.compaction_count
            self._session_tokens += runner.total_usage.total_tokens

    async def _build_and_run_runner(
        self,
        submission_id: str,
        turn_cancel: CancellationToken,
        resolved_for_turn: list[ResolvedInstruction],
        *,
        seed_pending_call_id: str | None = None,
        cache_break_expected_reason: str | None = None,
        auto_retry_count: int = 0,
    ) -> None:
        """构造并运行一轮，最后一次性回写 Engine 状态。"""
        runner = self._new_turn_runner(
            submission_id,
            turn_cancel,
            resolved_for_turn,
            auto_retry_count=auto_retry_count,
        )
        # turn-rewind retry_tool：让 runner 采样前先补跑被保留的悬空 call
        runner._seed_pending_call_id = seed_pending_call_id  # noqa: SLF001
        # turn-rewind R2：rewind 蓄意回退 anchor → 首采样的 cache 失效记为 expected
        if cache_break_expected_reason is not None:
            runner._next_cache_break_expected = True  # noqa: SLF001
            runner._next_cache_break_reason = cache_break_expected_reason  # noqa: SLF001
        # post_turn 钩子需用「本 turn 的 index」(= +1 之前的值,与同 turn 的
        # pre_turn iteration 对齐),故在 finally 自增前先捕获。
        fired_iteration = runner.turn_index
        try:
            outcome = await runner.run()
            if self._audit_state is not None:
                self._audit_state.coordinator.record_target_outcome(
                    submission_id,
                    outcome.end_reason,
                )
        finally:
            self._pending.pop(submission_id, None)
            self._turn_index += 1

        await self._writeback_turn_runner(runner)
        await self._fire_post_turn_hook(
            submission_id, outcome, turn_cancel, fired_iteration,
        )

    async def _fire_post_turn_hook(
        self,
        submission_id: str,
        outcome: TurnOutcome,
        turn_cancel: CancellationToken,
        iteration: int,
    ) -> None:
        """root turn 真终态时同步触发 post_turn 钩子(审计型,不可否决)。

        触发点在 turn 状态回写之后、下一 turn 启动之前 —— 给宿主「下一轮前必须
        完成」的顺序保证(self-review / 记忆固化等认知回路落脚点)。仅当注册了
        post_turn 钩子时才执行(常见路径零开销)。

        门控:
          - 挂起(suspended)= 暂停等 Resume —— 续跑到真终态才触发,此刻不触发;
          - 取消(cancelled)= teardown —— 不触发(与 R4 可取消语义一致)。
        R4:经 ``ctx.extras["cancel"]`` 把本 turn 的 CancellationToken 交给钩子;
        审计型经 ``run_audit_only`` 触发(deny / 异常都不改变已终结的 turn)。
        """
        if self._hooks is None:
            return
        if outcome.end_reason in ("suspended", "cancelled"):
            return
        handlers = self._hooks.registry.handlers("post_turn")
        if not handlers:
            return
        from taifeng.hooks.types import HookContext, PostTurnHook
        await self._hooks.run_audit_only(
            "post_turn",
            PostTurnHook(
                end_reason=outcome.end_reason,
                success=outcome.success,
                final_text=outcome.final_text,
                iteration=iteration,
            ),
            HookContext(
                thread_id=self._thread_id,
                submission_id=submission_id,
                entry_skill_id=self._entry_skill.id,
                extras={"cancel": turn_cancel},
            ),
        )
        await self._emit(EventMsg(
            submission_id=submission_id,
            msg=PostTurnHookFired(data={
                "end_reason": outcome.end_reason,
                "iteration": iteration,
                "hook_count": len(handlers),
            }),
        ))

    # -----------------------------------------------------------------
    # detached-spawn：分离式发起子 skill（立即返回句柄，后台独立跑完）
    # -----------------------------------------------------------------

    @property
    def _spawn_handles(self) -> SpawnHandleRegistry:
        """detached spawn 句柄表（白盒访问转发到 SpawnDriver）。

        逻辑已抽到 SpawnDriver；保留本 property 是为白盒断言 / 旧调用点提供等价访问，
        语义与抽取前一致（同一个 SpawnHandleRegistry 实例）。
        """
        return self._spawn._spawn_handles  # noqa: SLF001

    @property
    def _fired_barriers(self) -> set[str]:
        """join-barrier 进程内幂等守卫集（白盒访问转发到 SpawnDriver）。"""
        return self._spawn._fired_barriers  # noqa: SLF001

    async def spawn_skill(
        self, *, skill_id: str, args: dict[str, Any], reason: str
    ) -> dict[str, str]:
        """转发到 SpawnDriver.spawn_skill —— 公共 API + tools 的 spawn_coordinator 入口。

        分离式发起子 skill：立即返回句柄，子 skill 在后台分离 task 跑完。门控 / K1
        配额 / detached task 启动均由 SpawnDriver 负责。详见 spawn_driver.py。

        Args:
            skill_id: 要分离发起的子 skill id（须在 entry skill 的 child_skills 白名单内）。
            args: 子 skill 的种子输入（序列化为子 thread 首条 user_message）。
            reason: LLM / 业务自陈的发起理由（透传到事件 / 审计，taifeng 不解析语义）。

        Returns:
            ``{"handle_id": ..., "child_thread_id": ...}`` —— 立即可用于 ``spawn_status``。
        """
        return await self._spawn.spawn_skill(
            skill_id=skill_id, args=args, reason=reason
        )

    def _build_child_runner(
        self,
        target: SkillDefinition,
        child_thread_id: str,
        seed: ResponseItem,
        cancel: CancellationToken,
        *,
        history: list[ResponseItem] | None = None,
        auto_retry_count: int = 0,
    ) -> TurnRunner:
        """构造 detached spawn 的子 TurnRunner（镜像 turn.py::_spawn_sub_runner 的 kwargs）。

        ``auto_retry_count``:TTL 到期自动 retry 的谱系计数(suspend-review-fixes:
        spawn 重跑透传 → failure_suspend_max_auto_retries 对 spawn 拓扑生效)。

        与阻塞式 call_skill 子 runner 的差异：``cancel`` 由 engine 根取消派生（而非
        父 turn 的 ctx.cancel），其余依赖（snapshot / model / runtime / store /
        compressors / dispatch_policy / budget / hooks / permission / 资源配额）一致。
        ``call_stack`` 留空 → 子 runner 自判为独立根 turn（detached 即独立上下文）。

        Args:
            history: 续跑场景传入【已补齐 gap 的子 thread 完整历史】（从 store load_thread
                读回）；首发场景为 None → 用 ``[seed]`` 起跑。两种场景都保持 call_stack 空，
                即 detached 子 turn 永远是独立根 turn（resume 后仍是独立根，不依附父）。
        """
        buffer = list(history) if history is not None else [seed]
        return TurnRunner(
            entry_skill=target,
            snapshot=self._snapshot,
            model_client=self._model_client,
            tool_runtime=self._tool_runtime,
            store=self._store,
            compressors=self._compressors,
            dispatch_policy=self._dispatch_policy,
            outcome_judge=self._outcome_judge,
            budget=self._budget,
            thread_id=child_thread_id,
            submission_id=child_thread_id,
            emit=self._emit,
            cancel=cancel,
            image_input_policy=self._image_input_policy,
            input_cost_estimator=self._input_cost_estimator,
            hooks=self._hooks,
            permission_policy=self._permission_policy,
            request_metadata=self._request_metadata,
            turn_index=self._turn_index,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            denial_breaker_config=self._denial_breaker_config,
            doom_loop_config=self._doom_loop_config,
            failure_policy=self._failure_policy,
            failure_suspend_ttl_seconds=self._failure_suspend_ttl_seconds,
            failure_suspend_on_expire=self._failure_suspend_on_expire,
            auto_retry_count=auto_retry_count,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            reasoning_passback=self._reasoning_passback,
            enable_request_capture=self._enable_request_capture,
            capabilities=self._capabilities,
            # T6: deferred 暴露阈值（驱动 child 列表 inline/deferred + 工具裁剪）
            recall_threshold=self._recall_threshold,
            # 召回后端存在性：无后端恒 inline（与阈值同口径透传）
            has_recall_backend=self._has_recall_backend,
            spawn_registry=self._spawn_registry,
            session_tokens_used=self._session_tokens,
            max_session_tokens=self._max_session_tokens,
            memory_store=self._memory_store,
            memory_query_builder=self._memory_query_builder,
            pinned_states=self._pinned_states,
            history_buffer=buffer,
            # detached-spawn：spawned 子 runner 也注入协调器 → 子 skill 可继续 spawn
            spawn_coordinator=self,
        )

    async def _resume_spawn(self, sub: Submission, handle: SpawnHandle) -> None:
        """转发到 SpawnDriver.resume_spawn —— 续跑挂起的 detached spawn 子 thread。

        调用点：主 run 循环的 Resume 分支（命中挂起 spawn 句柄时）。
        """
        await self._spawn.resume_spawn(sub, handle)

    def _match_suspended_spawn(self, thread_id: str) -> SpawnHandle | None:
        """转发到 SpawnDriver.match_suspended_spawn —— Resume 路由判定。

        调用点：主 run 循环的 Resume 分支（判 thread_id 是否命中挂起 spawn）。
        """
        return self._spawn.match_suspended_spawn(thread_id)

    def spawn_status(self, handle_ids: list[str]) -> dict[str, dict[str, Any]]:
        """转发到 SpawnDriver.spawn_status —— 公共 API（业务侧轮询 / join 检查）。"""
        return self._spawn.spawn_status(handle_ids)

    async def deliver_peer_message(
        self,
        *,
        target: str,
        text: str,
        mode: str = "queue_only",
        from_thread_id: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        """转发到 SpawnDriver.deliver_peer_message —— peer-mailbox 唯一投递路径。

        ``send_message`` 工具（经 spawn_coordinator 协议）与 ``SendToPeer`` Op
        都收敛到此。详见 spawn_driver.py 同名方法。
        """
        return await self._spawn.deliver_peer_message(
            target=target, text=text, mode=mode,
            from_thread_id=from_thread_id, submission_id=submission_id)

    async def wait_spawn_terminal(
        self,
        *,
        handle_id: str,
        timeout_seconds: float,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """转发到 SpawnDriver.wait_spawn_terminal —— ``wait_peer`` 工具实现体。"""
        return await self._spawn.wait_spawn_terminal(
            handle_id=handle_id, timeout_seconds=timeout_seconds, cancel=cancel)

    async def kill_spawn(self, handle_id: str) -> None:
        """转发到 SpawnDriver.kill_spawn —— 公共 API（主动终止单个 spawn 子树）。"""
        await self._spawn.kill_spawn(handle_id)

    def has_live_spawns(self) -> bool:
        """转发到 SpawnDriver.has_live_spawns —— 公共 API（pool 释放前的引用计数保活）。"""
        return self._spawn.has_live_spawns()

    async def set_join_barrier(
        self,
        handle_ids: list[str],
        then_skill_id: str,
        then_args_template: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """转发到 SpawnDriver.set_join_barrier —— 公共 API（登记 join-barrier）。"""
        return await self._spawn.set_join_barrier(
            handle_ids, then_skill_id, then_args_template
        )

    async def _rebuild_spawn_state_from_history(self) -> None:
        """转发到 SpawnDriver.rebuild_from_history —— 冷恢复重建句柄表 / barrier / 守卫集。

        调用点：pool 重载 engine 时（engine 持有 prior history 的 resume 场景）。
        """
        await self._spawn.rebuild_from_history()


    # -----------------------------------------------------------------
    # Resume：续跑挂起的 turn（配对 resolutions → 补齐 history gap → 续采样）
    # -----------------------------------------------------------------

    async def _handle_resume(self, sub: Submission, root_cancel: CancellationToken) -> None:
        """续跑一个挂起的 thread：配对 resolutions → 补齐 history gap → 续采样。

        步骤：
          1. 在 self._history 找"活跃挂起"（最后一条 kind=='suspension' 且其 record_id
             尚未被 resolved-marker 标记消费）。找不到 → SuspensionResolveRejected 返回。
          2. SuspensionRecord.from_item 还原；SuspensionResolver().plan(record, resolutions)。
             ResolveError → SuspensionResolveRejected(reason=str(e)) 返回（禁静默）。
          3. 应用 plan：回填 function_call_output(form/data/deny)、执行 tool(permission allow)。
          4. 落 resolved-marker（system_injection source='suspend_resolved'）标记消费（幂等）。
          5. emit SuspensionResolved。
          6. 非 abort → _build_and_run_runner 续采样；abort → 不续跑（turn 终止）。
        """
        assert isinstance(sub.op, Resume)
        op = sub.op

        # 子 thread resume：Resume.thread_id 指向 call_skill 派发的子 thread（≠ 根 thread）。
        # 挂起记录落在子 thread，根 self._history 找不到 → 走专门的续跑链（先续跑子 thread
        # 拿结果，再逐层回填父 call_skill 的 output，最终根 turn 续跑完成）。
        if op.thread_id != self._thread_id:
            await self._handle_child_resume(sub, op, root_cancel)
            return

        # 1. 找活跃挂起 record（扫 history：最后一条未被 resolved-marker 消费的 suspension）
        record = self._find_active_suspension()
        if record is None:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "no_active_suspension", "record_id": None, "detail": {}})))
            return

        # 1.5 在飞守卫:同 record 已有 Resume 在处理(marker 未落)→ 显式拒绝,
        # 防双裁决(同 call_id 双 fco、双 marker、双续跑)
        if record.record_id in self._resolving_records:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "resolve_in_flight",
                      "record_id": record.record_id, "detail": {}})))
            return
        self._resolving_records.add(record.record_id)
        try:
            await self._handle_resume_resolved(sub, op, record, root_cancel)
        finally:
            self._resolving_records.discard(record.record_id)

    async def _handle_resume_resolved(
        self, sub: Submission, op: Resume,
        record: SuspensionRecord, root_cancel: CancellationToken,
    ) -> None:
        """_handle_resume 的主体(在飞守卫占位后):配对 → 应用 → 结算 → 续跑。"""
        # 1.6 到期哨兵与未核销 pending 求交(陈旧快照不重复回填);空 → 让位
        resolutions = self._effective_resolutions(
            record, list(self._history), op.resolutions)
        if not resolutions:
            return
        # 2. 配对 + 计划（ResolveError 显式拒绝，不静默兜底）
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver
        try:
            plan = SuspensionResolver().plan(record, resolutions)
        except ResolveError as e:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": str(e), "record_id": record.record_id, "detail": {}})))
            return

        # 3. 应用 plan：补齐 history gap（挂起点的 function_call 缺 function_call_output）
        import json
        async with self._lock:
            # 3a. form/data 直接回填 output（payload 即工具结果，JSON 序列化）
            for call_id, payload in plan.direct_outputs.items():
                out = function_call_output(
                    call_id=call_id, output=json.dumps(payload, ensure_ascii=False),
                    thread_id=self._thread_id, is_error=False)
                self._history.append(out)
                await self._store.append(out)
            # 3b. deny / 到期 → error output(前缀按 pending reason 渲染)
            for call_id, reason in plan.deny_outputs.items():
                out = function_call_output(
                    call_id=call_id,
                    output=self._deny_output_text(record, call_id, reason),
                    thread_id=self._thread_id, is_error=True)
                self._history.append(out)
                await self._store.append(out)
        # 3c. permission allow → 真正执行 tool（复用 runtime，不绕 RwLock）
        for call_id in plan.execute_tool_call_ids:
            await self._execute_resumed_tool(call_id)

        # 3.5 + 4. record 级结算判定(per-record 锁串行化并发 Resume)+ 落 marker:
        # 仍有未核销 pending → 部分核销,不落 marker、不续跑(record 级 barrier)
        async with self._settle_lock(record.record_id):
            active = self._find_active_suspension()
            if active is None or active.record_id != record.record_id:
                # 并发 Resume 已抢先全量结算:补显式事件(消除观测空洞)
                await self._emit(EventMsg(
                    submission_id=sub.id, msg=SuspensionResolveRejected(data={
                        "reason": "superseded_by_concurrent_settlement",
                        "record_id": record.record_id, "detail": {}})))
                return
            remaining = [
                p for p in self._unsettled_pendings(record, list(self._history))
                if p.request_id not in resolutions]
            if remaining:
                await self._emit(EventMsg(
                    submission_id=sub.id,
                    msg=SuspensionPartiallyResolved(data={
                        "record_id": record.record_id, "thread_id": self._thread_id,
                        "resolved_request_ids": sorted(resolutions.keys()),
                        "remaining_request_ids": sorted(
                            p.request_id for p in remaining)})))
                return
            marker = system_injection(
                text=f"suspend_resolved:{record.record_id}",
                thread_id=self._thread_id, source="suspend_resolved")
            async with self._lock:
                self._history.append(marker)
            await self._store.append(marker)

        # 5. emit resolved
        await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id, "request_ids": sorted(record.request_ids())})))

        # 6. 续跑（abort 则不续；turn 已在挂起点终止，gap 已补齐即收尾）
        auto_retries = self._apply_plan_session_effects(plan, record)
        if plan.abort:
            return
        # Resume 续跑同样过 K2 闸门(resource-limit-retry-semantics):未经增额的
        # 续跑在会话已触顶时不得静默烧 token——按 policy 再裁决(挂起 / 终态)
        if await self._gate_session_tokens(sub.id):
            return
        turn_cancel = root_cancel.child(f"sub:{sub.id}")
        self._pending[sub.id] = _PendingTurn(submission_id=sub.id, cancel=turn_cancel)
        await self._build_and_run_runner(
            sub.id, turn_cancel, list(self._last_resolved or []),
            auto_retry_count=auto_retries)

    # -----------------------------------------------------------------
    # 子 thread resume：续跑链（leaf 子 thread → 逐层回填父 call_skill → 根）
    # -----------------------------------------------------------------

    async def _handle_child_resume(
        self, sub: Submission, op: Resume, root_cancel: CancellationToken
    ) -> None:
        """续跑一个【子 thread】的挂起，并把结果逐层回传父 call_skill 直到根完成。

        机制（对账 call_skill 正常非挂起回传路径 turn.py::_spawn_sub_runner）：
          1. 自根 self._thread_id 沿 CHILD_SKILL pending（detail.sub_thread_id）向下
             串出 [根, …, leaf] 链，每层记 (thread_id, entry_skill_id, 父 call_id)。
             —— 不依赖 store.get_metadata（MessageStore 协议无元数据查询）：根 thread /
             entry skill 由 engine 自持，子层谱系由父挂起 record 的 pending detail 携带。
          2. leaf 子 thread：用用户 resolutions 核销真实挂起（permission/form/data），
             补 gap → 重建 TurnRunner 续跑 → 拿 final_text（= 正常子 turn 完成）。
          3. 自 leaf 向上：把每个父 call_skill 的 function_call_output 回填为子结果
             （= 正常 run_sub_skill 的 ToolResult.ok），续跑父 turn；根用既有
             self._history / _build_and_run_runner 收尾。

        任一层续跑若又挂起（再触发挂起点），该层各自 emit turn_suspended，续跑链在该层
        中止（上层 call_skill 仍挂起，等下一次 Resume）—— 与单层 resume 语义一致。

        Args:
            sub: 本次 Resume submission（事件归因）。
            op: Resume(thread_id=<子 thread>, resolutions=...)。
            root_cancel: 根取消 token（派生各层 turn 的子 token）。
        """
        # 1. 自根向下串链至 leaf（op.thread_id）。链元素 = (thread_id, entry_skill_id, 父 call_id)
        chain = await self._build_resume_chain(op.thread_id)
        if chain is None:
            # 根/中途某层无活跃 CHILD_SKILL 挂起指向目标 leaf → 找不到挂起，拒绝
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "no_active_suspension", "record_id": None, "detail": {}})))
            return

        # 2. leaf：核销用户挂起 + 续跑，拿到回传给父的结果字符串
        leaf_tid, leaf_skill_id, _ = chain[-1]
        leaf_result = await self._resume_leaf_thread(
            sub, leaf_tid, leaf_skill_id, op.resolutions, root_cancel)
        if leaf_result is None:
            # leaf 核销失败（已 emit Rejected）或又挂起（已 emit turn_suspended）→ 不上溯
            return

        # 3. 自 leaf 向上逐层回填父 call_skill output + 续跑父 turn，直到根完成或中途再挂起
        child_result = leaf_result
        # 倒序遍历父链（去掉 leaf 自身）：[..., 祖父, 父]
        for level in range(len(chain) - 2, -1, -1):
            parent_tid, parent_skill_id, _ = chain[level]
            # 本层 call_skill 的 call_id = 子层元素携带的"父 call_id"
            call_id = chain[level + 1][2]
            cont = await self._resume_parent_level(
                sub, parent_tid, parent_skill_id, call_id, child_result, root_cancel)
            if cont is None:
                # 父是根（根分支已收尾）/ 父又挂起 → 链终止
                return
            child_result = cont

    async def _build_resume_chain(
        self, leaf_thread_id: str
    ) -> list[tuple[str, str, str | None]] | None:
        """自根 self._thread_id 沿 CHILD_SKILL pending 向下串出到 leaf 的续跑链。

        每层元素 = (thread_id, entry_skill_id, 该 thread 在【父】里对应的 call_skill call_id)；
        根层的"父 call_id"为 None。逐层用父挂起 record 的 CHILD_SKILL pending
        （detail.sub_thread_id / skill_id / related_call_id）确定下一层。

        Returns:
            [(根tid, 根skill, None), …, (leaftid, leafskill, 父callid)]；
            根/中途无指向 leaf 的活跃 CHILD_SKILL 挂起 → None（找不到挂起，调用方拒绝）。

        多 pending record(parallel 批多子同挂,multi-pending-partial-resume):
        按 DFS 遍历**全部** CHILD_SKILL 分支寻址 leaf——已核销分支的子 thread 无
        活跃挂起 → 自然死路回溯,不再"只取首个 pending 单路下探"。
        """
        from taifeng.suspend.reason import SuspendReason

        async def descend(
            tid: str, items: list[ResponseItem], depth: int,
        ) -> list[tuple[str, str, str | None]] | None:
            """返回自 tid 之下到 leaf 的链段(不含 tid 本层);找不到 → None。"""
            if tid == leaf_thread_id:
                return []
            if depth <= 0:
                return None  # 超出深度守卫(异常链)
            record = self._find_active_suspension_in(items)
            if record is None:
                return None  # 本层无活跃挂起 → 链断
            for pend in record.pending:
                if pend.reason is not SuspendReason.CHILD_SKILL:
                    continue
                child_tid = pend.detail.get("sub_thread_id")
                child_skill = pend.detail.get("skill_id")
                if not (isinstance(child_tid, str) and isinstance(child_skill, str)):
                    continue
                child_items = await self._load_thread_items(child_tid)
                rest = await descend(child_tid, child_items, depth - 1)
                if rest is not None:
                    return [(child_tid, child_skill, pend.related_call_id), *rest]
            return None  # 全部分支均不含 leaf

        rest = await descend(
            self._thread_id, list(self._history), self._max_total_spawns_guard())
        if rest is None:
            return None
        return [(self._thread_id, self._entry_skill.id, None), *rest]

    def _max_total_spawns_guard(self) -> int:
        """续跑链 DFS 下探的最大层数守卫(防坏数据成环)。

        必须低于 Python 默认递归限(1000):descend 系 async 递归逐帧压栈,守卫
        高于递归限时坏数据会先炸 RecursionError(create_task 中静默)而非由守卫
        终止。正常链深受 max_call_depth 约束(个位数),128 余量充足。
        """
        return 128

    @staticmethod
    def _next_child_link(
        record: SuspensionRecord,
    ) -> tuple[str, str, str | None] | None:
        """从一个挂起 record 里取首个 CHILD_SKILL pending → (子tid, 子skill_id, 父callid)。

        正常单链下探每层至多一条 CHILD_SKILL pending（并发多子各自挂起属另一形态，
        本续跑链按"用户指向的 leaf"单路下探）。无 CHILD_SKILL pending → None。
        """
        from taifeng.suspend.reason import SuspendReason
        for p in record.pending:
            if p.reason is SuspendReason.CHILD_SKILL:
                tid = p.detail.get("sub_thread_id")
                skill_id = p.detail.get("skill_id")
                if isinstance(tid, str) and isinstance(skill_id, str):
                    return tid, skill_id, p.related_call_id
        return None

    async def _resume_leaf_thread(
        self, sub: Submission, leaf_tid: str, leaf_skill_id: str,
        resolutions: dict[str, Any], root_cancel: CancellationToken,
        *, submission_id: str | None = None,
    ) -> str | None:
        """核销 leaf 子 thread 的用户挂起 + 续跑该子 turn，返回回传父的结果字符串。

        复用既有 resume 语义（SuspensionResolver + gap 补齐 + 续采样），但作用在
        【子 thread 的 load_thread 历史】而非 self._history。

        Returns:
            子 turn 续跑后的 final_text（成功）/ 错误串（失败）；核销被拒或子又挂起 → None。
        """
        items = await self._load_thread_items(leaf_tid)
        record = self._find_active_suspension_in(items)
        if record is None:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "no_active_suspension", "record_id": None, "detail": {}})))
            return None
        # 在飞守卫(与根路径同理):同 leaf record 并发 Resume 拒后到者
        if record.record_id in self._resolving_records:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "resolve_in_flight",
                      "record_id": record.record_id, "detail": {}})))
            return None
        self._resolving_records.add(record.record_id)
        try:
            return await self._resume_leaf_settled(
                sub, leaf_tid, leaf_skill_id, resolutions, record, root_cancel,
                submission_id=submission_id)
        finally:
            self._resolving_records.discard(record.record_id)

    async def _resume_leaf_settled(
        self, sub: Submission, leaf_tid: str, leaf_skill_id: str,
        resolutions: dict[str, Any], record: SuspensionRecord,
        root_cancel: CancellationToken, *, submission_id: str | None = None,
    ) -> str | None:
        """_resume_leaf_thread 的主体(在飞守卫占位后):配对 → 应用 → 结算 → 续跑。"""
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver

        # 到期哨兵与未核销 pending 求交;空 → 让位(已被并发人工核销)
        resolutions = self._effective_resolutions(
            record, await self._load_thread_items(leaf_tid), resolutions)
        if not resolutions:
            return None
        try:
            plan = SuspensionResolver().plan(record, resolutions)
        except ResolveError as e:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": str(e), "record_id": record.record_id, "detail": {}})))
            return None

        # 补 gap（在子 thread 上）：form/data 直填、permission deny 填 error、allow 执行 tool
        await self._apply_plan_on_thread(leaf_tid, leaf_skill_id, record, plan)
        # request 级核销:leaf record 仍有未核销 pending → 部分核销,不落 marker、
        # 不续跑 leaf(链中止,句柄/父层保持挂起等后续 Resume)
        items_after = await self._load_thread_items(leaf_tid)
        remaining = [p for p in self._unsettled_pendings(record, items_after)
                     if p.request_id not in resolutions]
        if remaining:
            await self._emit(EventMsg(
                submission_id=sub.id,
                msg=SuspensionPartiallyResolved(data={
                    "record_id": record.record_id, "thread_id": leaf_tid,
                    "resolved_request_ids": sorted(resolutions.keys()),
                    "remaining_request_ids": sorted(
                        p.request_id for p in remaining)})))
            return None
        await self._append_resolved_marker(leaf_tid, record.record_id)
        await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        auto_retries = self._apply_plan_session_effects(plan, record)
        if plan.abort:
            # system_retry abort：子 turn 在挂起点终止，不续跑 → 视为失败回传父
            return f"sub_skill_aborted: {record.record_id}"
        outcome = await self._run_thread_turn(
            sub, leaf_tid, leaf_skill_id, root_cancel, submission_id=submission_id,
            auto_retry_count=auto_retries)
        if outcome.end_reason == "suspended":
            # 子续跑又挂起：本层 emit 了 turn_suspended，续跑链中止（等下次 Resume）
            return None
        return outcome.final_text if outcome.success else (
            f"sub_skill_failed: {outcome.error or outcome.end_reason}")

    async def _resume_parent_level(
        self, sub: Submission, parent_tid: str, parent_skill_id: str,
        call_id: str | None, child_result: str, root_cancel: CancellationToken,
        *, submission_id: str | None = None,
    ) -> str | None:
        """回填父 thread 中 call_id 对应 call_skill 的 output，续跑父 turn。

        Returns:
            父 turn 续跑后的 final_text（需继续上溯时非 None）；父是根 / 父又挂起 → None。
        """
        is_root = parent_tid == self._thread_id
        items = (list(self._history) if is_root
                 else await self._load_thread_items(parent_tid))
        record = self._find_active_suspension_in(items)
        if record is None or call_id is None:
            logger.warning("child_resume: parent %s missing active CHILD_SKILL link",
                           parent_tid)
            return None
        # 回填父 call_skill 的 function_call_output（= 正常 run_sub_skill 的成功回传）
        is_error = (child_result.startswith("sub_skill_failed:")
                    or child_result.startswith("sub_skill_aborted:"))
        out = function_call_output(
            call_id=call_id, output=child_result,
            thread_id=parent_tid, is_error=is_error)
        # 1) 先回填本子的 fco(call_id 唯一,并发链各回各的,无冲突)
        if is_root:
            async with self._lock:
                self._history.append(out)
        await self._store.append(out)

        # 2) record 级结算判定:per-record 锁串行化并发续跑链(双子同时 Resume/
        #    到期),判定基于锁内 fresh 状态——否则可能双双判 partial(无人续跑父)
        #    或双双 settle(双重续跑)
        async with self._settle_lock(record.record_id):
            fresh = (list(self._history) if is_root
                     else await self._load_thread_items(parent_tid))
            active = self._find_active_suspension_in(fresh)
            if active is None or active.record_id != record.record_id:
                # 并发链已抢先全量结算并续跑 → 本链到此为止(补显式事件)
                await self._emit(EventMsg(
                    submission_id=sub.id, msg=SuspensionResolveRejected(data={
                        "reason": "superseded_by_concurrent_settlement",
                        "record_id": record.record_id, "detail": {}})))
                return None
            remaining = self._unsettled_pendings(record, fresh)
            if remaining:
                # request 级核销:仍有未核销 pending → 不落 marker、不续跑父 turn
                # (record 级 barrier,等错峰 Resume 结清)
                await self._emit(EventMsg(
                    submission_id=sub.id,
                    msg=SuspensionPartiallyResolved(data={
                        "record_id": record.record_id, "thread_id": parent_tid,
                        "resolved_request_ids": [
                            p.request_id for p in record.pending
                            if p.related_call_id == call_id],
                        "remaining_request_ids": sorted(
                            p.request_id for p in remaining)})))
                return None
            # 全量达成:锁内落 marker(并发链经 fresh 重读可见,不会二次结算)
            marker = system_injection(
                text=f"suspend_resolved:{record.record_id}",
                thread_id=parent_tid, source="suspend_resolved")
            if is_root:
                async with self._lock:
                    self._history.append(marker)
            await self._store.append(marker)
        await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        if is_root:
            # 根：续跑(重入重放已回填的全部子输出)
            turn_cancel = root_cancel.child(f"sub:{sub.id}")
            self._pending[sub.id] = _PendingTurn(
                submission_id=sub.id, cancel=turn_cancel)
            await self._build_and_run_runner(
                sub.id, turn_cancel, list(self._last_resolved or []))
            return None  # 根是终点，链结束
        # 非根祖先：续跑该祖先 turn
        outcome = await self._run_thread_turn(
            sub, parent_tid, parent_skill_id, root_cancel,
            submission_id=submission_id)
        if outcome.end_reason == "suspended":
            return None
        return outcome.final_text if outcome.success else (
            f"sub_skill_failed: {outcome.error or outcome.end_reason}")

    async def _build_spawn_resume_chain(
        self, root_tid: str, root_skill_id: str,
        resolutions: dict[str, Any] | None = None,
    ) -> list[tuple[str, str, str | None]] | None:
        """自 spawn 子 thread 沿 CHILD_SKILL pending 向下串到最深 leaf 的续跑链。

        与 ``_build_resume_chain``（自 engine 根下探到指定 leaf）的差异：detached spawn
        子 thread **不挂在 engine 根的 CHILD_SKILL 链上**（它是独立根），故续跑链须**以
        spawn 子 thread 为根**重建，并一路下探到「无 CHILD_SKILL pending」的那层（即真正
        持有用户 DATA/FORM 挂起的 leaf）——业务侧只知道 spawn 子 thread（SpawnSuspended.
        thread_id），不知道 leaf 具体是哪个，故按链下探自动定位。

        多 pending record(multi-pending-partial-resume):resolutions 非 None 时
        以「resolutions 的 request_id 子集落在哪层 record」为 leaf 判据 DFS 寻址
        (多个挂起分支错峰 Resume 时按提交内容精确定位);匹配不到则回退
        「首个可达的无 CHILD_SKILL 层」旧语义,核销不符由 ResolveError 显式拒绝。

        Returns:
            ``[(根tid, 根skill, None), …, (leaftid, leafskill, 父callid)]``；
            root_tid 自身无活跃挂起 → None（无可续）。
        """
        from taifeng.suspend.reason import SuspendReason

        async def descend(
            tid: str, depth: int, *, match: bool,
        ) -> list[tuple[str, str, str | None]] | None:
            """返回自 tid 之下到 leaf 的链段(不含 tid);本层即 leaf → []。"""
            items = await self._load_thread_items(tid)
            record = self._find_active_suspension_in(items)
            if record is None or depth <= 0:
                return None  # 无活跃挂起(死路/已核销分支)或超深度
            if (match and resolutions is not None
                    and set(resolutions) <= record.request_ids()):
                return []  # 本层 record 即裁决目标
            branches = [
                pend for pend in record.pending
                if pend.reason is SuspendReason.CHILD_SKILL
                and isinstance(pend.detail.get("sub_thread_id"), str)
                and isinstance(pend.detail.get("skill_id"), str)
            ]
            for pend in branches:
                child_tid = str(pend.detail["sub_thread_id"])
                rest = await descend(child_tid, depth - 1, match=match)
                if rest is not None:
                    return [(child_tid, str(pend.detail["skill_id"]),
                             pend.related_call_id), *rest]
            if not match and not branches:
                return []  # 旧语义:无 CHILD_SKILL pending 即 leaf
            return None

        guard = self._max_total_spawns_guard()
        rest = await descend(root_tid, guard, match=True)
        if rest is None:
            # 回退旧语义(resolutions 与任何层都不符 → 让 leaf 层 ResolveError 显式拒)
            rest = await descend(root_tid, guard, match=False)
        if rest is None:
            return None
        return [(root_tid, root_skill_id, None), *rest]

    async def _settle_call_skill_output(
        self, sub: Submission, thread_id: str, call_id: str, child_result: str
    ) -> str:
        """在 thread 上回填 call_id 对应 call_skill 的 function_call_output + 落 resolved-marker
        核销该层 CHILD_SKILL 挂起。

        供 spawn 嵌套续跑链在**重跑 spawn 子 thread 前**补齐其 call_skill gap（= 正常
        run_sub_skill 的成功回传）。与 ``_resume_parent_level`` 非根分支同语义，但**不**重跑
        该 thread（重跑交由 spawn 驱动经 ``_build_child_runner`` + ``_finalize_spawn`` 完成，
        以保持 detached 根语义 + 句柄终态回写 + join-barrier 触发）。

        Returns:
            "settled" 全量核销(可重跑)/ "partial" 部分核销(其余 pending 未结,
            句柄保持挂起)/ "missing" 该 thread 无活跃挂起(调用方拒绝,禁静默)。
        """
        items = await self._load_thread_items(thread_id)
        record = self._find_active_suspension_in(items)
        if record is None:
            return "missing"
        is_error = child_result.startswith(
            ("sub_skill_failed:", "sub_skill_aborted:"))
        out = function_call_output(
            call_id=call_id, output=child_result,
            thread_id=thread_id, is_error=is_error)
        await self._store.append(out)
        # record 级结算判定(per-record 锁 + fresh 重读,与 _resume_parent_level 同理)
        async with self._settle_lock(record.record_id):
            fresh = await self._load_thread_items(thread_id)
            active = self._find_active_suspension_in(fresh)
            if active is None or active.record_id != record.record_id:
                return "partial"  # 并发链已抢先结算 → 本链不再重跑
            remaining = self._unsettled_pendings(record, fresh)
            if remaining:
                await self._emit(EventMsg(
                    submission_id=sub.id,
                    msg=SuspensionPartiallyResolved(data={
                        "record_id": record.record_id, "thread_id": thread_id,
                        "resolved_request_ids": [
                            p.request_id for p in record.pending
                            if p.related_call_id == call_id],
                        "remaining_request_ids": sorted(
                            p.request_id for p in remaining)})))
                return "partial"
            await self._append_resolved_marker(thread_id, record.record_id)
        await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        return "settled"

    async def _load_thread_items(self, thread_id: str) -> list[ResponseItem]:
        """load_thread → list（子 thread resume 需要按当前持久态重建历史）。"""
        return [it async for it in await self._store.load_thread(thread_id)]

    async def _apply_plan_on_thread(
        self, thread_id: str, entry_skill_id: str,
        record: SuspensionRecord, plan: Any
    ) -> None:
        """在指定 thread 上应用 ResolvePlan 的 gap 补齐（form/data/deny/allow-execute）。

        与根路径 _handle_resume 第 3 步同语义，但作用在子 thread（落 store；子 turn
        续跑时由 load_thread 读回）。permission allow 走 _execute_resumed_tool_on_thread。
        entry_skill_id 用于该 thread 内执行被批准 tool 时构造 ToolContext 的 skill 上下文。
        """
        import json
        for call_id, payload in plan.direct_outputs.items():
            out = function_call_output(
                call_id=call_id, output=json.dumps(payload, ensure_ascii=False),
                thread_id=thread_id, is_error=False)
            await self._store.append(out)
        for call_id, reason in plan.deny_outputs.items():
            out = function_call_output(
                call_id=call_id,
                output=self._deny_output_text(record, call_id, reason),
                thread_id=thread_id, is_error=True)
            await self._store.append(out)
        for call_id in plan.execute_tool_call_ids:
            await self._execute_resumed_tool_on_thread(
                thread_id, entry_skill_id, call_id)
        # resolved-marker 不在此签发:request 级核销下由调用方在
        # 全部 pending 核销后经 _append_resolved_marker 落定(单一签发点)。

    async def _append_resolved_marker(self, thread_id: str, record_id: str) -> None:
        """落 record 级 resolved-marker(request 级核销全量达成时的唯一非根签发点)。"""
        marker = system_injection(
            text=f"suspend_resolved:{record_id}",
            thread_id=thread_id, source="suspend_resolved")
        await self._store.append(marker)

    async def _run_thread_turn(
        self, sub: Submission, thread_id: str, entry_skill_id: str,
        root_cancel: CancellationToken, *, submission_id: str | None = None,
        auto_retry_count: int = 0,
    ) -> Any:
        """为指定（非根）thread 构造 TurnRunner 并续跑一轮，返回 TurnOutcome。

        history_buffer 从 load_thread 重建（含本次 resume 补的 gap）；entry_skill 由
        续跑链携带的 entry_skill_id 经 snapshot 解析（不依赖 store.get_metadata）。
        turn 内若再挂起会 emit turn_suspended 并落新 SuspensionRecord
        （续跑链据 end_reason=='suspended' 中止）。

        ``submission_id``：续跑事件的归因 submission。默认 None → 用本次 Resume 的
        ``sub.id``（根 thread 续跑链语义）。**spawn 嵌套续跑链**显式传 spawn 子 thread id：
        使被 spawn 的子任务 subtree 续跑事件归因到与首发一致的 child_thread（业务侧按
        submission_id 分轨，否则 leaf 子步文本会错挂到 Resume submission，丢轨）。
        """
        from taifeng.loop.turn import TurnRunner
        from taifeng.skill.dispatch import CallStack

        entry = self._snapshot.get(entry_skill_id)
        if entry is None:
            raise RuntimeError(f"child_resume_entry_skill_missing: {entry_skill_id}")
        items = await self._load_thread_items(thread_id)
        turn_cancel = root_cancel.child(f"sub:{sub.id}:thr:{thread_id}")
        # 子 thread 续跑必须标记为非根 turn（is_root=False，由 call_stack 非空判定）：
        # 否则其 turn_completed 会误带 is_root=True，业务桥接层会把子完成当成 submission
        # 终结。push 子 skill 自身一帧即可（栈非空 → run() 不再补 entry 帧）。
        sub_stack = CallStack().push(
            skill_id=entry.id, call_id=f"resume_{thread_id}")
        runner = TurnRunner(
            entry_skill=entry,
            snapshot=self._snapshot,
            model_client=self._model_client,
            tool_runtime=self._tool_runtime,
            store=self._store,
            compressors=self._compressors,
            dispatch_policy=self._dispatch_policy,
            outcome_judge=self._outcome_judge,
            budget=self._budget,
            thread_id=thread_id,
            submission_id=submission_id or sub.id,
            emit=self._emit,
            cancel=turn_cancel,
            image_input_policy=self._image_input_policy,
            input_cost_estimator=self._input_cost_estimator,
            hooks=self._hooks,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            denial_breaker_config=self._denial_breaker_config,
            doom_loop_config=self._doom_loop_config,
            failure_policy=self._failure_policy,
            failure_suspend_ttl_seconds=self._failure_suspend_ttl_seconds,
            failure_suspend_on_expire=self._failure_suspend_on_expire,
            auto_retry_count=auto_retry_count,
            # K2 执法(suspend-review-fixes):leaf/父层续跑注入会话预算——
            # 增额后有执法;续跑用量不回写 engine 计量为既有缺口(文档声明)
            session_tokens_used=self._session_tokens,
            max_session_tokens=self._max_session_tokens,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            reasoning_passback=self._reasoning_passback,
            enable_request_capture=self._enable_request_capture,
            history_buffer=list(items),
            permission_policy=self._permission_policy,
            request_metadata=self._request_metadata,
            turn_index=self._turn_index,
            capabilities=self._capabilities,
            # T6: deferred 暴露阈值（驱动 child 列表 inline/deferred + 工具裁剪）
            recall_threshold=self._recall_threshold,
            # 召回后端存在性：无后端恒 inline（与阈值同口径透传）
            has_recall_backend=self._has_recall_backend,
            spawn_registry=self._spawn_registry,
            memory_store=self._memory_store,
            memory_query_builder=self._memory_query_builder,
            pinned_states=self._pinned_states,
            call_stack=sub_stack,
        )
        return await runner.run()

    async def _execute_resumed_tool_on_thread(
        self, thread_id: str, entry_skill_id: str, call_id: str
    ) -> None:
        """在指定 thread 上执行一个被批准的挂起 tool call，回填 function_call_output。

        与 _execute_resumed_tool 同语义（预批准 + dispatch + 回填），但作用在子 thread：
        从 load_thread 找原 function_call，落 output 到 store（子 turn 续跑时读回）。
        entry_skill_id 由续跑链携带（不依赖 store.get_metadata）。
        """
        from taifeng.tool.spec import ToolContext

        items = await self._load_thread_items(thread_id)
        fc: ResponseItem | None = None
        for item in items:
            if item.kind == "function_call" and item.payload.get("call_id") == call_id:
                fc = item
        if fc is None:
            raise RuntimeError(f"resumed_tool_call_not_found: {call_id}@{thread_id}")
        import json
        name = fc.payload["name"]
        try:
            args = json.loads(fc.payload.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        entry = self._snapshot.get(entry_skill_id) or self._entry_skill
        cancel = CancellationToken().child(f"resume_tool:{call_id}")
        ctx = ToolContext(
            call_id=call_id, cancel=cancel, thread_id=thread_id,
            extras={
                "skill_snapshot": self._snapshot,
                "visible_skills": self._snapshot.reachable_from(entry.id),
                "dispatch_policy": self._dispatch_policy,
                "outcome_judge": self._outcome_judge,
                "current_skill": entry,
                "entry_skill_id": entry.id,
                "permission_policy": self._permission_policy,
                "hook_runner": self._hooks,
                "request_metadata": self._request_metadata,
                "turn_index": self._turn_index,
                "script_executors": self._script_executors,
            },
        )
        if self._permission_policy is not None:
            self._permission_policy.preapprove(call_id)
        result = await self._tool_runtime.dispatch(name=name, arguments=args, ctx=ctx)
        out = function_call_output(
            call_id=call_id, output=result.output,
            thread_id=thread_id, is_error=result.is_error)
        await self._store.append(out)

    async def _cancel_active_suspension(
        self, cancel_sub_id: str, target_sub_id: str
    ) -> None:
        """R4：若存在 submission_id 匹配的活跃挂起，追加 resolved-marker 丢弃之。

        与 _handle_resume 第 4 步同机制（同 marker text 格式 'suspend_resolved:<id>'），
        保证两条丢弃路径被 _find_active_suspension 一致识别。丢弃后该 record 不再被
        _find_active_suspension 返回，后续 Resume 命中 no_active_suspension 被拒。
        无匹配挂起则 no-op（保持 CancelTurn 既有宽容语义：找不到目标不报错）。

        参数：
            cancel_sub_id: 本次 CancelTurn submission 的 id（EngineLog 归属）。
            target_sub_id: CancelTurn 要取消的目标 submission id（挂起 turn 的 sub）。
        副作用：向 history + store 追加一条 resolved-marker；emit 一条 EngineLog。
        """
        record = self._find_active_suspension()
        # 仅当存在活跃挂起且其 submission_id 与取消目标一致时才丢弃
        if record is None or record.submission_id != target_sub_id:
            return
        marker = system_injection(
            text=f"suspend_resolved:{record.record_id}",
            thread_id=self._thread_id,
            source="suspend_resolved",
        )
        async with self._lock:
            self._history.append(marker)
        await self._store.append(marker)
        await self._emit(
            EventMsg(
                submission_id=cancel_sub_id,
                msg=EngineLog(
                    data={
                        "level": "info",
                        "message": (
                            f"cancelled suspended turn {target_sub_id} "
                            f"(record {record.record_id})"
                        ),
                        "extra": {},
                    }
                ),
            )
        )

    def _find_active_suspension(self) -> SuspensionRecord | None:
        """扫 self._history，返回最后一条尚未被 resolved-marker 消费的 suspension record。

        resolved-marker = source=='suspend_resolved' 的 system_injection，
        其 text 形如 'suspend_resolved:<record_id>'。
        """
        return self._find_active_suspension_in(self._history)

    @staticmethod
    def _find_active_suspension_in(
        items: list[ResponseItem],
    ) -> SuspensionRecord | None:
        """在任意 items 序列中找最后一条未被 resolved-marker 消费的 suspension record。

        从 _find_active_suspension 泛化而来 —— 子 thread resume 时对【子 thread 的
        load_thread 结果】复用同一识别逻辑（resolved-marker 同 text 格式）。

        Args:
            items: 一个 thread 的 ResponseItem 序列（根 = self._history；子 = load_thread）。
        Returns:
            活跃挂起的 SuspensionRecord；无挂起或已被核销 → None。
        """
        resolved_ids: set[str] = set()
        last_suspension: ResponseItem | None = None
        for item in items:
            if item.kind == "system_injection" and item.payload.get("source") == "suspend_resolved":
                rid = (item.payload.get("text") or "").removeprefix("suspend_resolved:")
                resolved_ids.add(rid)
            elif item.kind == "suspension":
                last_suspension = item
        if last_suspension is None:
            return None
        record = SuspensionRecord.from_item(last_suspension)
        if record.record_id in resolved_ids:
            return None
        return record

    @staticmethod
    def _deny_output_text(
        record: SuspensionRecord, call_id: str, reason_text: str,
    ) -> str:
        """按 pending reason 渲染 deny 回填文案(suspension-ttl-hardening)。

        PERMISSION → ``permission_denied: ...``(用户拒绝语义不变);
        其余(DATA/FORM/CHILD_SKILL 的到期 abort)→ ``suspension_expired: ...``——
        数据问询超时不再被模型/业务误读为权限拒绝。
        """
        from taifeng.suspend.reason import SuspendReason

        pend = next(
            (p for p in record.pending if p.related_call_id == call_id), None)
        if pend is not None and pend.reason is not SuspendReason.PERMISSION:
            return f"suspension_expired: {reason_text}"
        return f"permission_denied: {reason_text}"

    def _apply_plan_session_effects(self, plan: Any, record: SuspensionRecord) -> int:
        """应用 ResolvePlan 的会话级副作用,返回续跑 runner 的 auto_retry_count。

        - K2 retry 增额:extend_session_tokens > 0 → 抬升 `_max_session_tokens`
          (显式抬顶;触顶条件随之清除,retry 真实有效)。
        - 谱系计数:plan 来自 TTL 到期自动 retry(expired_retry)→ 续跑计数 =
          record 内既有计数 + 1(人工 Resume 恒 0,不计数)。
        """
        if (getattr(plan, "extend_session_tokens", 0)
                and self._max_session_tokens is not None):
            self._max_session_tokens += plan.extend_session_tokens
        if not getattr(plan, "expired_retry", False):
            return 0
        prior = max(
            (int(p.detail.get("auto_retry_count", 0) or 0) for p in record.pending),
            default=0)
        return prior + 1

    def _effective_resolutions(
        self, record: SuspensionRecord, items: list[ResponseItem],
        resolutions: dict[str, Any],
    ) -> dict[str, Any]:
        """到期哨兵 resolutions 与未核销 pending 求交;人工 payload 原样返回。

        fire 快照与哨兵 Resume 实际处理之间存在陈旧窗口:期间被人工部分核销的
        pending 再收哨兵会对已配对 call_id 重复落 deny fco(suspend-review-fixes)。
        仅对**纯哨兵**提交过滤(内核签发形态);空交集 → 调用方 no-op 让位。
        """
        from taifeng.suspend.resolver import EXPIRE_SENTINEL

        if not resolutions or not all(
            isinstance(v, dict) and v.get(EXPIRE_SENTINEL) is True
            for v in resolutions.values()
        ):
            return dict(resolutions)
        unsettled = {p.request_id for p in self._unsettled_pendings(record, items)}
        return {rid: v for rid, v in resolutions.items() if rid in unsettled}

    def _settle_lock(self, record_id: str) -> asyncio.Lock:
        """取 record 级结算锁(惰性创建;record 终结后残留的空锁可忽略不计)。"""
        lock = self._settle_locks.get(record_id)
        if lock is None:
            lock = asyncio.Lock()
            self._settle_locks[record_id] = lock
        return lock

    @staticmethod
    def _unsettled_pendings(
        record: SuspensionRecord, items: list[ResponseItem],
    ) -> list[Any]:
        """返回 record 中尚未核销的 pending(request 级核销的推导真相,R5)。

        判据:pending 的 related_call_id 在该 record 的 suspension item **之后**
        已有配对 function_call_output 即视为已核销(gap 回填即核销凭据)——
        以 suspension item 为锚而非全量扫描,避免历史轮次同 call_id(编排合成
        call_id 跨 turn 相同)误判。related_call_id=None 的 pending(护栏挂起,
        设计上独占 record)无 fco 凭据,恒视为未核销(由整批裁决一次性结算)。
        """
        pos = -1
        for i, it in enumerate(items):
            if (it.kind == "suspension"
                    and it.payload.get("record_id") == record.record_id):
                pos = i
        settled_call_ids = {
            str(it.payload.get("call_id")) for it in items[pos + 1:]
            if it.kind == "function_call_output"
        }
        return [p for p in record.pending
                if p.related_call_id is None
                or p.related_call_id not in settled_call_ids]

    async def _execute_resumed_tool(self, call_id: str) -> None:
        """resume 时对一个被批准的挂起 tool call 真正执行，回填 function_call_output。

        从 history 找到该 call_id 的 function_call（取 name + arguments）→ 经
        tool_runtime.dispatch 执行 → 追加 function_call_output。

        Args:
            call_id: permission allow 后需真正执行的挂起 tool call id。

        Raises:
            RuntimeError: history 中找不到该 call_id 的 function_call（断点不一致）。
        """
        from taifeng.tool.spec import ToolContext

        # 找原 function_call（取最后一条匹配，与 turn.py 落盘序一致）
        fc: ResponseItem | None = None
        for item in self._history:
            if item.kind == "function_call" and item.payload.get("call_id") == call_id:
                fc = item
        if fc is None:
            raise RuntimeError(f"resumed_tool_call_not_found: {call_id}")
        import json
        name = fc.payload["name"]
        try:
            args = json.loads(fc.payload.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        # 构造 ToolContext：resume 续跑发生在 engine 层（无 TurnRunner），extras 提供
        # 工具运行所需的最小上下文（snapshot / 可见 skill / 权限策略 / 元数据）。
        # 关键：permission_policy 不再注入 ask prompter 的挂起语义——本次执行是"已批准"
        # 的二次放行，工具内若再次走 check 应按业务策略放行（业务侧据 resolutions 调整）。
        cancel = CancellationToken().child(f"resume_tool:{call_id}")
        ctx = ToolContext(
            call_id=call_id,
            cancel=cancel,
            thread_id=self._thread_id,
            extras={
                "skill_snapshot": self._snapshot,
                "visible_skills": self._snapshot.reachable_from(self._entry_skill.id),
                "dispatch_policy": self._dispatch_policy,
                "outcome_judge": self._outcome_judge,
                "current_skill": self._entry_skill,
                "entry_skill_id": self._entry_skill.id,
                "permission_policy": self._permission_policy,
                "hook_runner": self._hooks,
                "request_metadata": self._request_metadata,
                "turn_index": self._turn_index,
                "script_executors": self._script_executors,
            },
        )
        # resume：人类已批准该挂起 call → 预批准，避免重跑时再次触发 prompter（防无限挂起）
        if self._permission_policy is not None:
            self._permission_policy.preapprove(call_id)
        result = await self._tool_runtime.dispatch(name=name, arguments=args, ctx=ctx)
        out = function_call_output(
            call_id=call_id, output=result.output,
            thread_id=self._thread_id, is_error=result.is_error)
        async with self._lock:
            self._history.append(out)
        await self._store.append(out)

    async def _run_compact_now(
        self,
        submission_id: str,
        op: CompactNow,
        root_cancel: CancellationToken,
    ) -> None:
        if self._compressors is None:
            await self._emit(
                EventMsg(
                    submission_id=submission_id,
                    msg=EngineLog(
                        data={
                            "level": "warn",
                            "message": "compactor not configured",
                            "extra": {},
                        }
                    ),
                )
            )
            return
        # 若 op 提供了临时 budget 覆盖，用临时 budget；否则用 engine budget
        budget = self._budget
        if op.target_tokens is not None or op.preserve_tail is not None:
            budget = ContextBudget(
                context_window=self._budget.context_window,
                soft_limit_ratio=(
                    op.target_tokens / max(self._budget.context_window, 1)
                    if op.target_tokens is not None
                    else self._budget.soft_limit_ratio
                ),
                hard_limit_ratio=self._budget.hard_limit_ratio,
                preserve_tail_messages=(
                    op.preserve_tail
                    if op.preserve_tail is not None
                    else self._budget.preserve_tail_messages
                ),
            )

        cancel = root_cancel.child(f"sub:{submission_id}")
        runner = TurnRunner(
            entry_skill=self._entry_skill,
            snapshot=self._snapshot,
            model_client=self._model_client,
            tool_runtime=self._tool_runtime,
            store=self._store,
            compressors=self._compressors,
            dispatch_policy=self._dispatch_policy,
            outcome_judge=self._outcome_judge,
            budget=budget,
            thread_id=self._thread_id,
            submission_id=submission_id,
            emit=self._emit,
            cancel=cancel,
            image_input_policy=self._image_input_policy,
            input_cost_estimator=self._input_cost_estimator,
            hooks=self._hooks,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            denial_breaker_config=self._denial_breaker_config,
            doom_loop_config=self._doom_loop_config,
            failure_policy=self._failure_policy,
            failure_suspend_ttl_seconds=self._failure_suspend_ttl_seconds,
            failure_suspend_on_expire=self._failure_suspend_on_expire,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            reasoning_passback=self._reasoning_passback,
            enable_request_capture=self._enable_request_capture,
            history_buffer=list(self._history),
            cache_anchor_index=self._cache_anchor_index,
            pinned_states=self._pinned_states,
            # T6: 一致性透传（CompactNow runner 不采样，阈值无实效但保字段齐整）
            recall_threshold=self._recall_threshold,
            has_recall_backend=self._has_recall_backend,
        )
        await runner._maybe_compress(phase="manual", force=op.force)  # noqa: SLF001
        async with self._lock:
            self._history = list(runner.history_buffer)
            self._cache_anchor_index = runner.cache_anchor_index
            # turn-rewind：对当前全量逻辑 history 重算节点表(derive 为唯一产出方)。
            # CompactNow 路径：在压缩后 history 上重算，折叠语义与冷加载推导一致。
            self._rewind_checkpoints = derive_rewind_log(self._history)

    # -----------------------------------------------------------------
    # Op handlers (rewind / rollback / update_budget / refresh_snapshot)
    # -----------------------------------------------------------------

    async def _emit_rewind_rejected(
        self, submission_id: str, node_id: str, reason: str
    ) -> None:
        """rewind 校验失败统一出口(禁 silent fallback,显式发事件)。"""
        await self._emit(EventMsg(
            submission_id=submission_id,
            msg=RewindRejected(data={"node_id": node_id, "reason": reason}),
        ))

    async def _emit_rewind_table_rebuilt(self) -> None:
        """冷恢复后补发 rewind_table_rebuilt（R3 可观测）。

        pool resume 路径在 _rebuild_spawn_state_from_history 之后调用本方法，
        告知订阅者冷重建已完成、节点表已就绪。
        turn_count 取 history 内 user_message 数（与 count_turns 一致）。
        submission_id 用 '*' 标记系统级事件（不属于某次具体 submission）。
        """
        await self._emit(EventMsg(
            submission_id="*",
            msg=RewindTableRebuilt(data={
                "thread_id": self._thread_id,
                "turn_count": count_turns(self._history),
                "node_count": len(self._rewind_checkpoints),
            }),
        ))

    def _rewrite_seed_args(self, call_id: str, new_args: dict[str, Any]) -> None:
        """retry_tool new_args：把内存 history 中该 call_id 的 function_call 换成新 args。

        只改内存(自洽 + 供重跑读新参);store 保持 append-only,arg 覆盖经 rewind
        marker 留痕。调用方已持锁。
        """
        import json
        for i, item in enumerate(self._history):
            if item.kind == "function_call" and item.payload.get("call_id") == call_id:
                self._history[i] = function_call(
                    call_id=call_id, name=item.payload["name"],
                    arguments=json.dumps(new_args, ensure_ascii=False),
                    thread_id=self._thread_id,
                )

    async def _handle_rewind(
        self, sub: Submission, root_cancel: CancellationToken
    ) -> None:
        """回退到 turn 内某回访节点并主动重推(turn-rewind 能力)。

        - re_reason：截到节点采样前 → 重采样(LLM 重新决定下游)。
        - retry_tool：截到 retry_tool 切点(保留 assistant 的 function_call)→ 补跑
          该工具(可换 new_args)→ 续推。仅 dispatch 节点。

        actor 模型下提交 Rewind 时上一 turn 已结束(engine 空闲),故"重推" = 截断
        engine history → 建新 root TurnRunner 重跑。详见设计 spec
        2026-06-05-addressable-dispatch-rewind。
        """
        op = sub.op
        assert isinstance(op, Rewind)

        # thread-addressable rewind:thread_id 指向非根 thread → 路由到 spawn
        # 子 thread rewind 链(守卫/截断/重推在 spawn_rewind.py;与 Resume 的
        # thread 寻址分流同形)。缺省 None 或显式指根 → 既有根路径零变更。
        if op.thread_id is not None and op.thread_id != self._thread_id:
            await self._spawn.rewind_spawn(sub)
            return

        # 1. 查 checkpoint(最近一次 root turn 回写的节点表)
        cp = next(
            (c for c in self._rewind_checkpoints if c.node_id == op.node_id), None
        )
        if cp is None:
            await self._emit_rewind_rejected(sub.id, op.node_id, "unknown_node")
            return
        # 2. mode/kind 相容:retry_tool 仅 dispatch 节点(且有 inner 切点)
        if op.mode == "retry_tool" and (
            cp.kind != "dispatch" or cp.inner_history_len is None
        ):
            await self._emit_rewind_rejected(sub.id, op.node_id, "mode_kind_mismatch")
            return
        # 3. 挂起态守卫:活跃挂起的 turn v1 不支持 rewind(挂起态 rewind 留待后续)
        if self._find_active_suspension() is not None:
            await self._emit_rewind_rejected(sub.id, op.node_id, "turn_suspended")
            return

        # 4. 选截点:retry_tool 用 inner(保 fc);其余用 history_len(re_reason)
        cut = (
            cp.inner_history_len
            if op.mode == "retry_tool" and cp.inner_history_len is not None
            else cp.history_len
        )

        # 5. 截断 history + 回退 cache_anchor(锁内;append-only:store 不删,仅内存截)
        async with self._lock:
            self._history = self._history[:cut]
            if self._cache_anchor_index >= cut:
                self._cache_anchor_index = cut - 1
            # 5b. retry_tool + new_args:改写悬空 fc 的 arguments(自洽 + 重跑用新参)
            if op.mode == "retry_tool" and op.new_args is not None and cp.call_id:
                self._rewrite_seed_args(cp.call_id, op.new_args)

        # 6. marker(审计;同 rollback 范式,落 store、不进 history)
        # cut_index 持久化：供 reconstruct_logical_history 冷恢复时按截断点重建逻辑 history
        marker = system_injection(
            f"[rewind] node={op.node_id} kind={cp.kind} mode={op.mode}",
            thread_id=self._thread_id, source="rewind",
            extra={"cut_index": cut},
        )
        await self._store.append(marker)

        # 7. emit turn_rewound(R3)
        await self._emit(EventMsg(submission_id=sub.id, msg=TurnRewound(data={
            "node_id": op.node_id, "node_kind": cp.kind, "mode": op.mode,
            "cut_index": cut, "cache_anchor": self._cache_anchor_index,
        })))

        # 8a. 冷 engine 惰性 resolve 指令层（spec §7 lazy-on-rewind）：
        #   正常 turn 结束后 _last_resolved 已由 _handle_user_message 填充；
        #   冷 engine（initial_history 注入、未跑任何 turn）_last_resolved 为空。
        #   此处检测：resolver 存在 + _last_resolved 空 + history 非空 → 补一次
        #   resolve，以构造期 entry skill 为锚点（已知限制：不还原历史 turn 里曾
        #   使用的不同 entry skill 的指令层，v1 范围外）。
        if (
            self._instruction_resolver is not None
            and not self._last_resolved
            and self._history
        ):
            # cancel=None:rewind 时无活跃 turn-level token,刻意不传(同 warmup_engine_scope)
            ctx = InstructionContext(
                session_id=self._session_id,
                thread_id=self._thread_id,
                entry_skill_id=self._entry_skill.id,
                turn_index=self._turn_index,
                metadata=self._request_metadata,
                cancel=None,
            )
            # best-effort:turn_rewound 已发出,resolve 失败不硬 abort(会留下不一致),
            # 但不静默——按仓库惯例(turn.py on_pre_evict)落 warning 日志,保留可观测(R3)
            try:
                self._last_resolved = await self._instruction_resolver.resolve(
                    ("engine", "session", "turn"), ctx,
                )
            except InstructionFetchError:
                logger.warning(
                    "冷 rewind 指令 resolve 失败,以空指令层续推(thread=%s)",
                    self._thread_id,
                )

        # 8b. 主动重推:截断后建新 root TurnRunner;retry_tool 先补跑悬空 call
        turn_cancel = root_cancel.child(f"sub:{sub.id}")
        self._pending[sub.id] = _PendingTurn(
            submission_id=sub.id, cancel=turn_cancel
        )
        seed = cp.call_id if op.mode == "retry_tool" else None
        await self._build_and_run_runner(
            sub.id, turn_cancel, list(self._last_resolved or []),
            seed_pending_call_id=seed,
            cache_break_expected_reason="rewind",
        )

    async def _handle_rollback(self, submission_id: str, num_turns: int) -> None:
        """回滚最近 N 轮对话。

        一"轮" = 以 user_message 为锚点。从 history 末尾向前数 N 个 user_message，
        删掉它及之后的所有 items。
        """
        if num_turns < 1:
            return
        async with self._lock:
            new_history = list(self._history)
            removed = 0
            user_count = 0
            cut_idx = len(new_history)
            for i in range(len(new_history) - 1, -1, -1):
                if new_history[i].kind == "user_message":
                    user_count += 1
                    if user_count == num_turns:
                        cut_idx = i
                        break
            if user_count < num_turns:
                # 没有足够的 user_message，全部清空
                cut_idx = 0
            removed = len(new_history) - cut_idx
            self._history = new_history[:cut_idx]
            # cache anchor 也要回退
            if self._cache_anchor_index >= cut_idx:
                self._cache_anchor_index = cut_idx - 1

        # 写一条 system_injection 标记
        # cut_index 持久化：供 reconstruct_logical_history 冷恢复时按截断点重建逻辑 history
        marker = system_injection(
            f"[rollback] dropped {removed} item(s), {num_turns} turn(s)",
            thread_id=self._thread_id,
            source="rollback",
            extra={"cut_index": cut_idx},
        )
        await self._store.append(marker)
        await self._emit(
            EventMsg(
                submission_id=submission_id,
                msg=EngineLog(
                    data={
                        "level": "info",
                        "message": f"rolled back {num_turns} turn(s)",
                        "extra": {"removed_items": removed},
                    }
                ),
            )
        )

    def _handle_update_budget(self, submission_id: str, op: UpdateBudget) -> None:
        """运行时调整 ContextBudget（部分字段）。"""
        cur = self._budget
        self._budget = ContextBudget(
            context_window=(
                op.context_window if op.context_window is not None else cur.context_window
            ),
            soft_limit_ratio=(
                op.soft_limit_ratio if op.soft_limit_ratio is not None else cur.soft_limit_ratio
            ),
            hard_limit_ratio=(
                op.hard_limit_ratio if op.hard_limit_ratio is not None else cur.hard_limit_ratio
            ),
            preserve_tail_messages=(
                op.preserve_tail_messages
                if op.preserve_tail_messages is not None
                else cur.preserve_tail_messages
            ),
        )
        logger.info(
            "budget updated: window=%d soft=%.2f hard=%.2f tail=%d",
            self._budget.context_window,
            self._budget.soft_limit_ratio,
            self._budget.hard_limit_ratio,
            self._budget.preserve_tail_messages,
        )

    async def _handle_update_instructions(
        self, submission_id: str, op: UpdateInstructions,
    ) -> None:
        """T4: 替换 layer 的 source + 失效缓存 + 发事件。

        spec Requirement (热更):
            - 成功 → instruction_updated（含 layer_name / new_source_kind）
            - 未知 name → instruction_update_rejected（reason='unknown_layer'）
            - 缓存立即失效（resolver.replace_layer 内部已清理）
        """
        if self._instruction_resolver is None:
            # 没配 resolver → 视为未知 name
            await self._emit(EventMsg(
                submission_id=submission_id,
                msg=InstructionUpdateRejected(data={
                    "layer_name": op.layer_name,
                    "reason": "no_instruction_resolver",
                }),
            ))
            return
        self._current_emit_submission_id = submission_id
        try:
            ok = self._instruction_resolver.replace_layer(
                op.layer_name, op.new_source,
            )
        finally:
            self._current_emit_submission_id = "*"
        if not ok:
            await self._emit(EventMsg(
                submission_id=submission_id,
                msg=InstructionUpdateRejected(data={
                    "layer_name": op.layer_name,
                    "reason": "unknown_layer",
                }),
            ))
            return
        new_kind = "static" if isinstance(op.new_source, str) else "dynamic"
        await self._emit(EventMsg(
            submission_id=submission_id,
            msg=InstructionUpdated(data={
                "layer_name": op.layer_name,
                "new_source_kind": new_kind,
            }),
        ))

    def _handle_refresh_snapshot(self, submission_id: str) -> None:
        """从 registry 拉最新 snapshot（业务侧热更 SKILL.md 后调）。

        注意：当前 entry_skill 引用保持不变（lock-in 语义）。
        """
        # 找父级 registry —— 由业务侧通过 _registry_ref 注入；否则 noop
        registry = getattr(self, "_registry_ref", None)
        if registry is None:
            logger.warning("refresh_snapshot: no registry ref")
            return
        self._snapshot = registry.snapshot()
        logger.info("snapshot refreshed → version=%d", self._snapshot.version)
