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
    function_call_output,
    system_injection,
    user_message,
)
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
    SuspensionResolved,
    SuspensionResolveRejected,
    TurnFailed,
)
from taifeng.loop.event import Shutdown as ShutdownMsg
from taifeng.loop.submission import (
    CancelTurn,
    CompactNow,
    InjectSystemMessage,
    Op,
    RefreshSnapshot,
    Resume,
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
        # engine-resume-by-thread-id: 接收 initial_history 拷贝（resume 场景由
        # pool 层注入），未提供 → 空列表（既有行为）
        self._history: list[ResponseItem] = (
            list(initial_history) if initial_history else []
        )
        # cache anchor 保持 -1：resume 场景下 provider prompt cache 跨进程
        # 不可信任，下一次 turn 的 pre_turn 压缩会重新决定 anchor 位置
        self._cache_anchor_index: int = -1
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
                if ev.msg.kind in ("turn_completed", "turn_failed", "shutdown"):
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
                if isinstance(sub.op, Resume):
                    await self._handle_resume(sub, cancel)
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
        )
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
            # G-CACHE：读回本轮末的 prompt 指纹，作为下一轮的对比基线
            self._last_prompt_fingerprint = runner.last_prompt_fingerprint
            # G1c：读回累计压缩次数，跨 turn 持久
            self._compaction_count = runner.compaction_count
            # K2：累加本 turn 消耗，跨 turn 维护会话累计 token
            self._session_tokens += runner.total_usage.total_tokens

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
        resolved_ids: set[str] = set()
        last_suspension: ResponseItem | None = None
        for item in self._history:
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

    # -----------------------------------------------------------------
    # Op handlers (rollback / update_budget / refresh_snapshot)
    # -----------------------------------------------------------------

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
        marker = system_injection(
            f"[rollback] dropped {removed} item(s), {num_turns} turn(s)",
            thread_id=self._thread_id,
            source="rollback",
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
