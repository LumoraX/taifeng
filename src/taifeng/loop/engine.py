"""AgentEngine —— 主 actor + Submission / EventMsg 双向消息总线。

参照：codex codex-rs/core/src/session/mod.rs::Codex
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from taifeng.context.budget import ContextBudget
from taifeng.context.cache_stats import PromptCacheStats
from taifeng.context.compressor import CompressionOrchestrator
from taifeng.conversation.models import (
    ResponseItem,
    function_call,
    function_call_output,
    system_injection,
    user_message,
)
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.conversation.store import MessageStore
from taifeng.instructions.resolver import InstructionResolver
from taifeng.instructions.source import InstructionFetchError
from taifeng.instructions.types import (
    InstructionContext,
    InstructionLayer,
    ResolvedInstruction,
)
from taifeng.llm.client import ModelClient
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import (
    EngineLog,
    EventMsg,
    InstructionCacheHit,
    InstructionFetched,
    InstructionFetchFailed,
    InstructionUpdated,
    InstructionUpdateRejected,
    PreTurnHookDenied,
    ResourceLimitExceeded,
    RewindRejected,
    RewindTableRebuilt,
    SuspensionResolved,
    SuspensionResolveRejected,
    TurnFailed,
    TurnRewound,
)
from taifeng.loop.event import Shutdown as ShutdownMsg
from taifeng.loop.rewind import RewindCheckpoint, count_turns, derive_rewind_log
from taifeng.loop.spawn_driver import SpawnDriver
from taifeng.loop.spawn_handle import SpawnHandle, SpawnHandleRegistry
from taifeng.loop.submission import (
    CancelTurn,
    CompactNow,
    InjectSystemMessage,
    Op,
    RefreshSnapshot,
    Resume,
    Rewind,
    Shutdown,
    Submission,
    ThreadRollback,
    UpdateBudget,
    UpdateInstructions,
    UserMessage,
)
from taifeng.loop.turn import TurnRunner
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import SkillSnapshot
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.runtime import ToolCallRuntime

logger = logging.getLogger(__name__)


@dataclass
class _PendingTurn:
    submission_id: str
    cancel: CancellationToken


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
        budget: ContextBudget | None = None,
        hooks: Any = None,
        max_iterations: int | None = None,
        max_parallel_tool_calls: int = 1,
        event_queue_size: int = 1024,
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
        self._store = store
        self._thread_id = thread_id
        # session_id 主要用于 InstructionContext.session_id 缓存键；若未传，
        # 退回到 thread_id（保持单 engine 单 session 的一对一）
        self._session_id = session_id or thread_id
        self._compressors = compressors
        self._dispatch_policy = dispatch_policy or DispatchPolicy()
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
        # config-consistency-fixes C2: 把 event_queue_size kwarg 真正生效
        # 之前此 kwarg 收下后未存到 self，subscribe / subscribe_all 内仍硬编码 1024
        self._event_queue_size = event_queue_size
        # permission_policy / request_metadata 透传链：
        # EnginePool → AgentEngine → TurnRunner。request_metadata 是业务侧不透明
        # 上下文（无业务命名字段，R1），合并进 PermissionRequest.metadata /
        # InstructionContext.metadata；taifeng 不解析其 keys。
        self._permission_policy = permission_policy
        self._request_metadata: dict[str, Any] = dict(request_metadata or {})
        # G4a: 业务注入的运行时能力快照（用于 skill 资格过滤）；None=不过滤
        self._capabilities = capabilities
        # K1：spawn 配额 registry —— engine 持有一份，贯穿整棵 turn 树（含跨 turn）。
        from taifeng.loop.spawn import SpawnSlotRegistry

        self._spawn_registry = SpawnSlotRegistry(
            max_concurrent=max_concurrent_spawns,
            max_total=max_total_spawns,
        )
        # K2：会话级累计 token 上限（OOM-killer）。_session_tokens 跨 turn 累计；
        # None → 不强制（默认，行为不变）。
        self._max_session_tokens = max_session_tokens
        # K3：长期记忆 swap 接口（None=无内存层级，默认行为不变）
        self._memory_store = memory_store
        self._session_tokens: int = 0
        # detached-spawn：协调器（句柄表 / 分离驱动 / 错峰 resume / join-barrier / 冷恢复）
        # 抽到 SpawnDriver（见 spawn_driver.py），engine 仅留薄转发器（公共 API + 调用点）。
        # SpawnDriver 复用 engine 的 _spawn_registry(K1) / _root_cancel / _build_child_runner /
        # store / emit / snapshot / lock / history 等共享内部，不复制这些状态。
        self._spawn = SpawnDriver(self)
        # 根取消 token —— 由 run() 入口捕获；spawn 的分离 task 据此派生子 token（R4）。
        # run() 启动前为 None（spawn_skill 在 engine.run 已起的前提下被调用）。
        self._root_cancel: CancellationToken | None = None

        # K4 入站背压：bounded submission 队列；submit() await put，满则业务侧阻塞
        # （flow control）。<=0 视为不限（保留逃生口）。
        self._submissions: asyncio.Queue[Submission] = asyncio.Queue(
            maxsize=submission_queue_size if submission_queue_size > 0 else 0
        )
        # K4 出站丢弃计数：事件队列满时不再静默丢——累计 + 暴露（可观测）。
        self._events_dropped: int = 0
        self._event_subs: dict[str, asyncio.Queue[EventMsg]] = {}
        """submission_id 'all' 表示订阅全部事件。"""
        self._all_subs: list[asyncio.Queue[EventMsg]] = []
        self._pending: dict[str, _PendingTurn] = {}
        self._running = False
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
        await self._submissions.put(sub)
        return sub.id

    async def subscribe_all(self) -> AsyncIterator[EventMsg]:
        """订阅本 engine 的全部事件。"""
        q: asyncio.Queue[EventMsg] = asyncio.Queue(maxsize=self._event_queue_size)
        self._all_subs.append(q)
        try:
            while True:
                ev = await q.get()
                yield ev
                if ev.msg.kind == "shutdown":
                    return
        finally:
            try:
                self._all_subs.remove(q)
            except ValueError:
                pass

    async def subscribe(self, submission_id: str) -> AsyncIterator[EventMsg]:
        """订阅指定 submission 的事件。完成后自动结束。"""
        q: asyncio.Queue[EventMsg] = asyncio.Queue(maxsize=self._event_queue_size)
        self._event_subs[submission_id] = q
        try:
            while True:
                ev = await q.get()
                if ev.submission_id != submission_id:
                    continue
                yield ev
                # turn_suspended 是独立终结态(turn 已结束，等待 Resume)——必须纳入自动
                # 终止集合，否则 turn 挂起时消费者的 async for 永远拿不到终结信号、卡死，
                # 业务也无法释放实例并提交 Resume(Task 16 回归根因)。
                if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended", "shutdown"):
                    return
        finally:
            self._event_subs.pop(submission_id, None)

    async def shutdown(self) -> None:
        await self.submit(Shutdown())

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
        # 广播给 all subs
        for q in list(self._all_subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # K4：不再静默丢——累计计数 + WARNING（consumer 可读 events_dropped
                # 自检漏事件）。不阻塞 emit（慢/缺席 consumer 不得拖死主 actor，R4）。
                self._events_dropped += 1
                logger.warning("event queue full, drop event %s", ev.msg.kind)
        # 投递给 per-submission sub
        per = self._event_subs.get(ev.submission_id)
        if per is not None:
            try:
                per.put_nowait(ev)
            except asyncio.QueueFull:
                self._events_dropped += 1
                logger.warning("per-sub queue full, drop event")

    @property
    def events_dropped(self) -> int:
        """K4：累计因订阅队列满而丢弃的事件数（0 = 无丢弃）。

        业务侧观测：>0 说明某订阅消费过慢、漏了事件——应加大 ``event_queue_size``
        或更快 drain。lossy-but-accounted：内核绝不为慢 consumer 阻塞主 actor。
        """
        return self._events_dropped

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

    # -----------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------

    async def run(self, cancel: CancellationToken) -> None:
        self._running = True
        # detached-spawn：记下根取消 token，供 spawn 的分离 task 派生子 token（R4 可取消）。
        self._root_cancel = cancel
        try:
            while self._running:
                if cancel.is_cancelled:
                    break
                try:
                    sub = await asyncio.wait_for(self._submissions.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(sub.op, Shutdown):
                    self._running = False
                    # K3 on_session_end（teardown）：会话结束最终 flush。best-effort。
                    await self._memory_session_end()
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
                    asyncio.create_task(self._handle_rewind(sub, cancel))
                    continue
                if isinstance(sub.op, Resume):
                    # detached spawn 续跑优先判定：Resume.thread_id 命中某个【挂起】的
                    # spawn 句柄 child_thread_id → 走 _resume_spawn（在该 child thread
                    # 自己的线上独立续跑，与父 turn 完全解耦；父 turn 早已结束）。
                    # 不命中（根 thread / call_skill 子链）→ 维持既有 _handle_resume。
                    spawn_handle = self._match_suspended_spawn(sub.op.thread_id)
                    if spawn_handle is not None:
                        asyncio.create_task(self._resume_spawn(sub, spawn_handle))
                        continue
                    # 与 UserMessage 一致用 create_task 异步派发：让续跑链（可能跨子/根
                    # 多个 turn）不阻塞主 run 循环，且给 subscribe(submission_id) 留出在
                    # 事件流出前注册队列的窗口（子 thread resume 续跑链 emit 多个事件，
                    # 内联执行会与"submit 后再 subscribe"的消费者抢跑导致丢首批事件→挂死）。
                    asyncio.create_task(self._handle_resume(sub, cancel))
                    continue
                if isinstance(sub.op, UserMessage):
                    asyncio.create_task(self._run_turn_for(sub, cancel))
                    continue
        finally:
            self._running = False
            # 通知所有 subscriber 退出
            for q in list(self._all_subs):
                try:
                    q.put_nowait(EventMsg(submission_id="*", msg=ShutdownMsg()))
                except asyncio.QueueFull:
                    pass

    async def _run_turn_for(self, sub: Submission, root_cancel: CancellationToken) -> None:
        assert isinstance(sub.op, UserMessage)
        turn_cancel = root_cancel.child(f"sub:{sub.id}")
        self._pending[sub.id] = _PendingTurn(submission_id=sub.id, cancel=turn_cancel)

        # 把 user 消息落 buffer + 持久化
        item = user_message(
            sub.op.text, thread_id=self._thread_id, attachments=sub.op.attachments
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
                    user_text=sub.op.text,
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
                preview = (sub.op.text or "")[:200]
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

        # K2 跨 turn 守卫：会话累计 token 已触顶 → 拒绝开新 turn（不再消耗）。
        if (
            self._max_session_tokens is not None
            and self._session_tokens >= self._max_session_tokens
        ):
            await self._emit(EventMsg(submission_id=sub.id, msg=ResourceLimitExceeded(
                data={
                    "limit_kind": "session_tokens",
                    "used": self._session_tokens,
                    "limit": self._max_session_tokens,
                    "scope": "turn_refused",
                },
            )))
            await self._emit(EventMsg(submission_id=sub.id, msg=TurnFailed(data={
                "error": "session_token_limit_exceeded",
                "kind": "resource_limit_exceeded",
                "iterations": 0,
                "is_root": True,
            })))
            self._pending.pop(sub.id, None)
            self._turn_index += 1
            return

        await self._build_and_run_runner(sub.id, turn_cancel, resolved_for_turn)

    async def _build_and_run_runner(
        self,
        submission_id: str,
        turn_cancel: CancellationToken,
        resolved_for_turn: list[ResolvedInstruction],
        *,
        seed_pending_call_id: str | None = None,
        cache_break_expected_reason: str | None = None,
    ) -> None:
        """构造 TurnRunner(基于当前 self._history)→ run → 回写 engine 状态。

        从 _run_turn_for 抽出，供 UserMessage turn 与 resume 续跑复用。
        调用方负责在调用前完成 gating(user_message 落盘 / instruction resolve /
        pre_turn hook / token 守卫)并把 pending 注册好；本方法只负责"跑一轮 + 回写"。

        参数:
            submission_id: 当前 turn 的 submission id（用于 pending 注销 / emit 归因）
            turn_cancel: 当前 turn 的取消子 token（由调用方从 root_cancel 派生）
            resolved_for_turn: 当前 turn 已 resolve 的 instruction 列表
        副作用:
            注销 self._pending[submission_id]、自增 self._turn_index、
            回写 self._history / cache_anchor / prompt 指纹 / 压缩计数 / 会话 token。
        """
        runner = TurnRunner(
            entry_skill=self._entry_skill,
            snapshot=self._snapshot,
            model_client=self._model_client,
            tool_runtime=self._tool_runtime,
            store=self._store,
            compressors=self._compressors,
            dispatch_policy=self._dispatch_policy,
            budget=self._budget,
            thread_id=self._thread_id,
            submission_id=submission_id,
            emit=self._emit,
            cancel=turn_cancel,
            hooks=self._hooks,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            history_buffer=list(self._history),
            cache_anchor_index=self._cache_anchor_index,
            instructions=list(resolved_for_turn),
            permission_policy=self._permission_policy,
            request_metadata=self._request_metadata,
            turn_index=self._turn_index,
            capabilities=self._capabilities,
            spawn_registry=self._spawn_registry,
            # G-CACHE：注入持久 cache 统计 + 上一轮 prompt 指纹（跨 turn 归因）
            cache_stats=self._cache_stats,
            last_prompt_fingerprint=self._last_prompt_fingerprint,
            # G1c：注入跨 turn 累计压缩次数 + 阈值
            compaction_count=self._compaction_count,
            compaction_degradation_threshold=self._compaction_degradation_threshold,
            # K2：注入会话累计 token 基线 + 上限（OOM-killer）
            session_tokens_used=self._session_tokens,
            max_session_tokens=self._max_session_tokens,
            memory_store=self._memory_store,
            # detached-spawn：注入自身作 spawn 协调器 → spawn_skill/await_skills/
            # join_skill/kill_skill 四工具经 ctx.extras['spawn_coordinator'] 转发
            spawn_coordinator=self,
        )
        # turn-rewind retry_tool：让 runner 采样前先补跑被保留的悬空 call
        runner._seed_pending_call_id = seed_pending_call_id  # noqa: SLF001
        # turn-rewind R2：rewind 蓄意回退 anchor → 首采样的 cache 失效记为 expected
        if cache_break_expected_reason is not None:
            runner._next_cache_break_expected = True  # noqa: SLF001
            runner._next_cache_break_reason = cache_break_expected_reason  # noqa: SLF001
        try:
            await runner.run()
        finally:
            self._pending.pop(submission_id, None)
            self._turn_index += 1

        # 同步 turn 内的 history 变更回 engine
        async with self._lock:
            # runner 直接改 history_buffer 引用 → 把 runner.history_buffer 倒回 engine
            self._history = list(runner.history_buffer)
            self._cache_anchor_index = runner.cache_anchor_index
            # turn-rewind：回写本 root turn 的回访节点表(供 rewind_nodes / _handle_rewind)
            self._rewind_checkpoints = list(runner.rewind_log.checkpoints)
            # G-CACHE：读回本轮末的 prompt 指纹，作为下一轮的对比基线
            self._last_prompt_fingerprint = runner.last_prompt_fingerprint
            # G1c：读回累计压缩次数，跨 turn 持久
            self._compaction_count = runner.compaction_count
            # K2：累加本 turn 消耗，跨 turn 维护会话累计 token
            self._session_tokens += runner.total_usage.total_tokens

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
    ) -> TurnRunner:
        """构造 detached spawn 的子 TurnRunner（镜像 turn.py::_spawn_sub_runner 的 kwargs）。

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
            budget=self._budget,
            thread_id=child_thread_id,
            submission_id=child_thread_id,
            emit=self._emit,
            cancel=cancel,
            hooks=self._hooks,
            permission_policy=self._permission_policy,
            request_metadata=self._request_metadata,
            turn_index=self._turn_index,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            capabilities=self._capabilities,
            spawn_registry=self._spawn_registry,
            session_tokens_used=self._session_tokens,
            max_session_tokens=self._max_session_tokens,
            memory_store=self._memory_store,
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

        # 2. 配对 + 计划（不允许部分 resume；ResolveError 显式拒绝，不静默兜底）
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver
        try:
            plan = SuspensionResolver().plan(record, op.resolutions)
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
            # 3b. permission deny → error output（让模型知道被拒并据此改写后续）
            for call_id, reason in plan.deny_outputs.items():
                out = function_call_output(
                    call_id=call_id, output=f"permission_denied: {reason}",
                    thread_id=self._thread_id, is_error=True)
                self._history.append(out)
                await self._store.append(out)
        # 3c. permission allow → 真正执行 tool（复用 runtime，不绕 RwLock）
        for call_id in plan.execute_tool_call_ids:
            await self._execute_resumed_tool(call_id)

        # 4. 落 resolved-marker（幂等：下次 _find_active_suspension 会跳过本 record）
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
        if plan.abort:
            return
        turn_cancel = root_cancel.child(f"sub:{sub.id}")
        self._pending[sub.id] = _PendingTurn(submission_id=sub.id, cancel=turn_cancel)
        await self._build_and_run_runner(sub.id, turn_cancel, list(self._last_resolved or []))

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
        """
        chain: list[tuple[str, str, str | None]] = [
            (self._thread_id, self._entry_skill.id, None)
        ]
        cur_tid, cur_items = self._thread_id, list(self._history)
        # 至多沿链下探 spawn 深度上限的层数（防御性，正常链很短）
        for _ in range(self._max_total_spawns_guard()):
            if cur_tid == leaf_thread_id:
                return chain
            record = self._find_active_suspension_in(cur_items)
            if record is None:
                return None  # 本层无活跃挂起 → 链断（找不到 leaf）
            nxt = self._next_child_link(record)
            if nxt is None:
                return None  # 本层挂起无 CHILD_SKILL pending → 到底但非目标 leaf
            child_tid, child_skill_id, call_id = nxt
            chain.append((child_tid, child_skill_id, call_id))
            cur_tid = child_tid
            cur_items = await self._load_thread_items(child_tid)
        return None  # 超出深度守卫（异常链）

    def _max_total_spawns_guard(self) -> int:
        """续跑链下探的最大层数守卫（取 spawn 总配额上界 + 余量，避免坏数据死循环）。"""
        return 1024

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
    ) -> str | None:
        """核销 leaf 子 thread 的用户挂起 + 续跑该子 turn，返回回传父的结果字符串。

        复用既有 resume 语义（SuspensionResolver + gap 补齐 + 续采样），但作用在
        【子 thread 的 load_thread 历史】而非 self._history。

        Returns:
            子 turn 续跑后的 final_text（成功）/ 错误串（失败）；核销被拒或子又挂起 → None。
        """
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver

        items = await self._load_thread_items(leaf_tid)
        record = self._find_active_suspension_in(items)
        if record is None:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "no_active_suspension", "record_id": None, "detail": {}})))
            return None
        try:
            plan = SuspensionResolver().plan(record, resolutions)
        except ResolveError as e:
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": str(e), "record_id": record.record_id, "detail": {}})))
            return None

        # 补 gap（在子 thread 上）：form/data 直填、permission deny 填 error、allow 执行 tool
        await self._apply_plan_on_thread(leaf_tid, leaf_skill_id, record, plan)
        await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        if plan.abort:
            # system_retry abort：子 turn 在挂起点终止，不续跑 → 视为失败回传父
            return f"sub_skill_aborted: {record.record_id}"
        outcome = await self._run_thread_turn(sub, leaf_tid, leaf_skill_id, root_cancel)
        if outcome.end_reason == "suspended":
            # 子续跑又挂起：本层 emit 了 turn_suspended，续跑链中止（等下次 Resume）
            return None
        return outcome.final_text if outcome.success else (
            f"sub_skill_failed: {outcome.error or outcome.end_reason}")

    async def _resume_parent_level(
        self, sub: Submission, parent_tid: str, parent_skill_id: str,
        call_id: str | None, child_result: str, root_cancel: CancellationToken,
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
        marker = system_injection(
            text=f"suspend_resolved:{record.record_id}",
            thread_id=parent_tid, source="suspend_resolved")
        if is_root:
            # 根：回填进 self._history（续跑机制依赖）+ store，再走既有根续跑
            async with self._lock:
                self._history.append(out)
                self._history.append(marker)
            await self._store.append(out)
            await self._store.append(marker)
            await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
                data={"record_id": record.record_id,
                      "request_ids": sorted(record.request_ids())})))
            turn_cancel = root_cancel.child(f"sub:{sub.id}")
            self._pending[sub.id] = _PendingTurn(
                submission_id=sub.id, cancel=turn_cancel)
            await self._build_and_run_runner(
                sub.id, turn_cancel, list(self._last_resolved or []))
            return None  # 根是终点，链结束
        # 非根祖先：落 output + marker 到其 thread，续跑该祖先 turn
        await self._store.append(out)
        await self._store.append(marker)
        await self._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        outcome = await self._run_thread_turn(
            sub, parent_tid, parent_skill_id, root_cancel)
        if outcome.end_reason == "suspended":
            return None
        return outcome.final_text if outcome.success else (
            f"sub_skill_failed: {outcome.error or outcome.end_reason}")

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
                call_id=call_id, output=f"permission_denied: {reason}",
                thread_id=thread_id, is_error=True)
            await self._store.append(out)
        for call_id in plan.execute_tool_call_ids:
            await self._execute_resumed_tool_on_thread(
                thread_id, entry_skill_id, call_id)
        # 落 resolved-marker 核销 leaf 记录
        marker = system_injection(
            text=f"suspend_resolved:{record.record_id}",
            thread_id=thread_id, source="suspend_resolved")
        await self._store.append(marker)

    async def _run_thread_turn(
        self, sub: Submission, thread_id: str, entry_skill_id: str,
        root_cancel: CancellationToken,
    ) -> Any:
        """为指定（非根）thread 构造 TurnRunner 并续跑一轮，返回 TurnOutcome。

        history_buffer 从 load_thread 重建（含本次 resume 补的 gap）；entry_skill 由
        续跑链携带的 entry_skill_id 经 snapshot 解析（不依赖 store.get_metadata）。
        turn 内若再挂起会 emit turn_suspended 并落新 SuspensionRecord
        （续跑链据 end_reason=='suspended' 中止）。
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
            budget=self._budget,
            thread_id=thread_id,
            submission_id=sub.id,
            emit=self._emit,
            cancel=turn_cancel,
            hooks=self._hooks,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            history_buffer=list(items),
            permission_policy=self._permission_policy,
            request_metadata=self._request_metadata,
            turn_index=self._turn_index,
            capabilities=self._capabilities,
            spawn_registry=self._spawn_registry,
            memory_store=self._memory_store,
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
            budget=budget,
            thread_id=self._thread_id,
            submission_id=submission_id,
            emit=self._emit,
            cancel=cancel,
            hooks=self._hooks,
            script_executors=self._script_executors,
            max_iterations=self._max_iterations,
            max_parallel_tool_calls=self._max_parallel_tool_calls,
            history_buffer=list(self._history),
            cache_anchor_index=self._cache_anchor_index,
        )
        await runner._maybe_compress(phase="manual", force=op.force)  # noqa: SLF001
        async with self._lock:
            self._history = list(runner.history_buffer)
            self._cache_anchor_index = runner.cache_anchor_index
            # turn-rewind：回写本 root turn 的回访节点表(供 rewind_nodes / _handle_rewind)
            self._rewind_checkpoints = list(runner.rewind_log.checkpoints)

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

        # 8. 主动重推:截断后建新 root TurnRunner;retry_tool 先补跑悬空 call
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
