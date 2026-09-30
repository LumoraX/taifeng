"""AgentEngine —— 主 actor + Submission / EventMsg 双向消息总线。

参照：codex codex-rs/core/src/session/mod.rs::Codex
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Literal

from taifeng.context.budget import ContextBudget, TokenCalibration
from taifeng.context.cache_stats import PromptCacheStats
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.instructions.resolver import InstructionResolver
from taifeng.llm.retrying import with_default_retry
from taifeng.loop import (
    engine_facade,
    engine_loop,
    engine_public,
    engine_submit,
)
from taifeng.loop.audit_mailbox import (
    AuditedSubmissionMailbox,
)
from taifeng.loop.audit_peer import root_inbox
from taifeng.loop.child_resume_chain import ChildResumeChain
from taifeng.loop.engine_events import EngineEvents
from taifeng.loop.engine_gate import EngineGate
from taifeng.loop.engine_lifecycle import EngineLifecycle
from taifeng.loop.engine_operations import EngineOperations
from taifeng.loop.engine_resume import EngineResume
from taifeng.loop.engine_runner import EngineRunner
from taifeng.loop.event import (
    EventMsg,
    InstructionCacheHit,
    InstructionFetched,
    InstructionFetchFailed,
    InstructionUpdated,
    InstructionUpdateRejected,
)
from taifeng.loop.rewind import RewindCheckpoint, derive_rewind_log
from taifeng.loop.spawn_driver import SpawnDriver
from taifeng.loop.suspension_access import SuspensionAccess
from taifeng.loop.suspension_ttl import SuspensionTtlScheduler
from taifeng.loop.turn import TurnRunner
from taifeng.loop.usage_meter import SessionUsageMeter
from taifeng.skill.dispatch import DispatchPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.context.compressor import CompressionOrchestrator
    from taifeng.conversation.models import (
        ResponseItem,
    )
    from taifeng.conversation.store import MessageStore
    from taifeng.instructions.types import (
        InstructionLayer,
        ResolvedInstruction,
    )
    from taifeng.llm.client import ModelClient
    from taifeng.llm.prewarm import ModelPrewarmer
    from taifeng.llm.retry import RetryConfig
    from taifeng.loop.audit_admission import (
        AcceptedUserMessage,
    )
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.submission import (
        Submission,
    )
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.registry import SkillSnapshot
    from taifeng.tool.runtime import ToolCallRuntime

logger = logging.getLogger(__name__)

# 进程内类型已下沉 engine_types.py（Wave 4 模块切分）。DeliveredEvent 是公共 API，
# 此处原样再导出，`from taifeng.loop.engine import DeliveredEvent` 的既有写法不变。
from taifeng.loop.engine_types import (  # noqa: E402
    DeliveredEvent as DeliveredEvent,  # `as` 同名 = 显式再导出,满足 no_implicit_reexport
)

# 白盒测试按 ``from taifeng.loop.engine import _PendingTurn / _Subscriber`` 寻址，原样再导出
from taifeng.loop.engine_types import _PendingTurn as _PendingTurn  # noqa: E402, TC001
from taifeng.loop.engine_types import _Subscriber as _Subscriber  # noqa: E402, TC001


class AgentEngine:
    """主 actor。

    生命周期：
        1. 业务构造 `engine = AgentEngine(...)`
        2. `task = asyncio.create_task(engine.run(root_cancel))`
        3. `sub_id = await engine.submit(UserMessage(...))`
        4. `async for ev in engine.subscribe(sub_id): ...`
        5. `await engine.submit(Shutdown())` + `await task`
    """

    # 模型侧预热器（ADR 0092）；pool 构造 engine 后注入，None = Prewarm 的 model 步骤不可用
    _model_prewarmer: ModelPrewarmer | None = None

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
        auto_retry: bool = True,
        retry_config: RetryConfig | None = None,
        now_factory: Any = None,
        max_parallel_tool_calls: int = 1,
        reasoning_passback: bool = True,
        enable_request_capture: bool = False,
        event_queue_size: int = 65536,
        event_high_water_ratio: float = 0.75,
        event_low_water_ratio: float = 0.5,
        event_warn_cooldown_sec: int = 5,
        submission_queue_size: int = 256,
        terminal_replay_size: int = 256,
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
        file_input_policy: Any = None,
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
        # ADR 0041：默认套有界重试（幂等；strict audit 适配器 / 已套过 / auto_retry=False 原样）
        self._model_client = with_default_retry(
            model_client, config=retry_config, enabled=auto_retry,
        )
        from taifeng.llm.file_input import DISABLED_FILE_POLICY
        from taifeng.llm.image_input import DISABLED_IMAGE_POLICY

        self._image_input_policy = image_input_policy or DISABLED_IMAGE_POLICY
        self._file_input_policy = file_input_policy or DISABLED_FILE_POLICY
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
        # TTL 裁决协作器：无自有状态，定时器表仍由本 engine 持有
        self._ttl = SuspensionTtlScheduler(self)
        # call_skill 子链续跑协作器：无自有状态，运行态仍由本 engine 持有
        self._child_chain = ChildResumeChain(self)
        self._suspend_access = SuspensionAccess(self)
        self._events = EngineEvents(self)
        self._resume = EngineResume(self)
        self._gate = EngineGate(self)
        self._runner = EngineRunner(self)
        self._lifecycle = EngineLifecycle(self)
        self._ops = EngineOperations(self)
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
        # usage-tree-accounting：整棵 turn 树共享的会话计量器（root / call_skill /
        # spawn / resume 续跑的每次采样实时入账）；_session_tokens 为其总量视图。
        self._usage_meter = SessionUsageMeter()
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
        # 晚到订阅者终态补投（ADR 0031）：记住每个 submission 最后一条终结事件，
        # 使「submission 已终态之后才 subscribe」立刻拿到终结而不是永久挂死。
        # 有界 FIFO：超出 terminal_replay_size 淘汰最老的（退化回等待，不吃内存）。
        # <=0 = 关闭补投（历史行为逃生口）。
        self._terminal_replay_size = terminal_replay_size
        self._terminal_replay: OrderedDict[str, EventMsg] = OrderedDict()
        self._pending: dict[str, _PendingTurn] = {}
        # run() 收敛完毕后置 True：此后过滤订阅立即得到合成终结（ADR 0029）
        self._closed = False
        # ADR 0029 root gate：同一 engine 同时只跑一个根 turn；gated Op 按提交序排队
        self._root_gate = asyncio.Lock()
        self._root_gate_owner: str | None = None
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
        # token-accounting-calibration：上下文 token 实测校准锚点（跨 turn 携带；
        # 冷重载 / 进程重启后为 None，首次采样后重建）
        self._token_calibration: TokenCalibration | None = None
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

    # 注：本方法**刻意留在 engine.py**——TurnRunner 的构造点是 engine 模块的白盒
    # 注入面，test_audit_history_merge 按 monkeypatch.setattr(engine_module,
    # "TurnRunner", ...) 替换该模块级符号；搬进 engine_runner.py 会让注入失效。
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
        if self._audit_state is not None:
            pending_input = root_inbox(self._audit_state)  # 跟着 Session 走（ADR 0100）
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
            session_id=self._session_id,
            emit=self._emit,
            cancel=turn_cancel,
            image_input_policy=self._image_input_policy,
            input_cost_estimator=self._input_cost_estimator,
            file_input_policy=self._file_input_policy,
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
            token_calibration=self._token_calibration,
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
            usage_meter=self._usage_meter,
            memory_store=self._memory_store,
            memory_query_builder=self._memory_query_builder,
            pinned_states=self._pinned_states,
            spawn_coordinator=self,
        )

    # -----------------------------------------------------------------
    # 方法体按区段放在兄弟模块（W7.1 拆文件，零行为变更）；此处按原名赋值，
    # engine 仍是唯一白盒寻址面，签名逐字保留。
    # -----------------------------------------------------------------
    register_pinned_state = engine_public.register_pinned_state
    unregister_pinned_state = engine_public.unregister_pinned_state
    instructions_snapshot = engine_public.instructions_snapshot
    history_snapshot = engine_public.history_snapshot
    rewind_nodes = engine_public.rewind_nodes
    rewind_nodes_for = engine_public.rewind_nodes_for
    estimate_tokens = engine_public.estimate_tokens
    usage_ratio = engine_public.usage_ratio
    introspect = engine_public.introspect
    thread_id = property(engine_public.thread_id)
    session_id = property(engine_public.session_id)
    entry_skill = property(engine_public.entry_skill)
    budget = property(engine_public.budget)
    snapshot = property(engine_public.snapshot)
    max_iterations = property(engine_public.max_iterations)
    max_parallel_tool_calls = property(engine_public.max_parallel_tool_calls)
    cache_stats = property(engine_public.cache_stats)
    _session_tokens = property(engine_public._session_tokens, engine_public._set_session_tokens)
    submit = engine_submit.submit
    _submit_audited_cancel_turn = engine_submit._submit_audited_cancel_turn
    _emit_cancel_turn_log = engine_submit._emit_cancel_turn_log
    _submit_audited_user_message = engine_submit._submit_audited_user_message
    _submit_audited_user_message_locked = engine_submit._submit_audited_user_message_locked
    _new_subscriber = engine_submit._new_subscriber
    subscribe_all_envelopes = engine_submit.subscribe_all_envelopes
    subscribe_all = engine_submit.subscribe_all
    subscribe_envelopes = engine_submit.subscribe_envelopes
    subscribe = engine_submit.subscribe
    shutdown = engine_submit.shutdown
    _instruction_emit_bridge = engine_submit._instruction_emit_bridge
    warmup_engine_scope = engine_submit.warmup_engine_scope
    run = engine_loop.run
    _is_queued_user_message = staticmethod(engine_loop._is_queued_user_message)
    _start_queued_user_message = engine_loop._start_queued_user_message
    _run_claimed_audited_turn = engine_loop._run_claimed_audited_turn
    _run_audited_turn_for = engine_loop._run_audited_turn_for
    _apply_accepted_item_owned = engine_loop._apply_accepted_item_owned
    _apply_accepted_item = engine_loop._apply_accepted_item
    _run_audited_target = engine_loop._run_audited_target
    _emit = engine_facade._emit
    _record_terminal = engine_facade._record_terminal
    _deliver = engine_facade._deliver
    _maybe_warn_water = engine_facade._maybe_warn_water
    _start_operation = engine_facade._start_operation
    _guarded_operation = engine_facade._guarded_operation
    _emit_operation_terminal = engine_facade._emit_operation_terminal
    _forget_operation = engine_facade._forget_operation
    _converge_operations = engine_facade._converge_operations
    _arm_ttl_timer = engine_facade._arm_ttl_timer
    _ttl_expire_after = engine_facade._ttl_expire_after
    _rearm_ttl_timers_cold = engine_facade._rearm_ttl_timers_cold
    _rearm_spawn_ttl_timers_cold = engine_facade._rearm_spawn_ttl_timers_cold
    _ttl_record_active = engine_facade._ttl_record_active
    _resolve_expiry_route = engine_facade._resolve_expiry_route
    _chain_contains_thread = engine_facade._chain_contains_thread
    _cancel_ttl_timers = engine_facade._cancel_ttl_timers
    _memory_session_end = engine_facade._memory_session_end
    _finalize_run_lifecycle = engine_facade._finalize_run_lifecycle
    _terminate_orphan_submissions = engine_facade._terminate_orphan_submissions
    _acquire_root_gate = engine_facade._acquire_root_gate
    _abandon_acquire = engine_facade._abandon_acquire
    _resume_tool_cancel = engine_facade._resume_tool_cancel
    _release_root_gate = engine_facade._release_root_gate
    _run_gated_op = engine_facade._run_gated_op
    _run_turn_for = engine_facade._run_turn_for
    _run_turn_for_gated = engine_facade._run_turn_for_gated
    _gate_session_tokens = engine_facade._gate_session_tokens
    _suspend_engine_gate = engine_facade._suspend_engine_gate
    _active_root_pending = engine_facade._active_root_pending
    _drain_residual_injections = engine_facade._drain_residual_injections
    _writeback_turn_runner = engine_facade._writeback_turn_runner
    _build_and_run_runner = engine_facade._build_and_run_runner
    _fire_post_turn_hook = engine_facade._fire_post_turn_hook
    spawn_skill = engine_facade.spawn_skill
    _build_child_runner = engine_facade._build_child_runner
    _resume_spawn = engine_facade._resume_spawn
    _match_suspended_spawn = engine_facade._match_suspended_spawn
    spawn_status = engine_facade.spawn_status
    is_spawn_thread = engine_facade.is_spawn_thread
    deliver_peer_message = engine_facade.deliver_peer_message
    wait_spawn_terminal = engine_facade.wait_spawn_terminal
    wait_spawn_any = engine_facade.wait_spawn_any
    kill_spawn = engine_facade.kill_spawn
    has_live_spawns = engine_facade.has_live_spawns
    set_join_barrier = engine_facade.set_join_barrier
    _rebuild_spawn_state_from_history = engine_facade._rebuild_spawn_state_from_history
    _handle_resume = engine_facade._handle_resume
    _handle_resume_resolved = engine_facade._handle_resume_resolved
    _handle_child_resume = engine_facade._handle_child_resume
    _build_resume_chain = engine_facade._build_resume_chain
    _max_total_spawns_guard = engine_facade._max_total_spawns_guard
    _next_child_link = staticmethod(engine_facade._next_child_link)
    _resume_leaf_thread = engine_facade._resume_leaf_thread
    _resume_leaf_settled = engine_facade._resume_leaf_settled
    _resume_parent_level = engine_facade._resume_parent_level
    _build_spawn_resume_chain = engine_facade._build_spawn_resume_chain
    _settle_call_skill_output = engine_facade._settle_call_skill_output
    _load_thread_items = engine_facade._load_thread_items
    _apply_plan_on_thread = engine_facade._apply_plan_on_thread
    _append_resolved_marker = engine_facade._append_resolved_marker
    _run_thread_turn = engine_facade._run_thread_turn
    _execute_resumed_tool_on_thread = engine_facade._execute_resumed_tool_on_thread
    _cancel_active_suspension = engine_facade._cancel_active_suspension
    _find_active_suspension = engine_facade._find_active_suspension
    _find_active_suspension_in = staticmethod(engine_facade._find_active_suspension_in)
    _deny_output_text = staticmethod(engine_facade._deny_output_text)
    _apply_plan_session_effects = engine_facade._apply_plan_session_effects
    _effective_resolutions = engine_facade._effective_resolutions
    _settle_lock = engine_facade._settle_lock
    _unsettled_pendings = staticmethod(engine_facade._unsettled_pendings)
    _execute_resumed_tool = engine_facade._execute_resumed_tool
    _run_compact_now = engine_facade._run_compact_now
    _emit_rewind_table_rebuilt = engine_facade._emit_rewind_table_rebuilt
    events_dropped = property(engine_facade.events_dropped)
    _spawn_handles = property(engine_facade._spawn_handles)
    _fired_barriers = property(engine_facade._fired_barriers)
