"""EnginePool —— 进程级单例 + 会话级 Engine 缓存。

参照：openclaw SessionStore + claw-code lane registry
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionOrchestrator, CompressionStrategy
from taifeng.context.strategies import HandoffCompactionStrategy, SlidingWindowStrategy
from taifeng.conversation.hook_runner import HookRunner
from taifeng.conversation.models import ResponseItem, ThreadInfo, ThreadMetadata
from taifeng.conversation.protocols import IndexHook, NoopIndexHook, ThreadDirectory
from taifeng.conversation.sqlite_directory import SqliteThreadDirectory
from taifeng.conversation.store import MessageStore
from taifeng.conversation.transcript import JsonlMessageStore, JsonlMessageWriter
from taifeng.llm.client import ModelClient
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.engine import AgentEngine
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry, SkillRegistry
from taifeng.tool.builtins import (
    make_call_skill_tool,
    make_read_skill_tool,
    make_run_script_tool,
)
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime
from taifeng.tool.spec import ToolSpec

if TYPE_CHECKING:
    from taifeng.telemetry.sink import TelemetrySink

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------
# _HookEmittingStore —— 包装 MessageStore，写完成后 spawn IndexHook 后台 task
# -----------------------------------------------------------------


class _HookEmittingStore(MessageStore):
    """MessageStore 代理 —— 所有写操作完成后 spawn IndexHook 后台 task（fire-and-forget）。

    EnginePool 在用户传入 ``index_hook`` 时用本代理包装真实 store，让 engine / turn 透明走 hook。
    业务对协议无感（仍是 MessageStore），但所有 ``create_thread`` / ``append`` 都会触发对应 hook。
    """

    def __init__(
        self,
        *,
        inner: MessageStore,
        runner: HookRunner,
        directory: ThreadDirectory,
    ) -> None:
        self._inner = inner
        self._runner = runner
        self._directory = directory

    async def create_thread(
        self,
        *,
        cwd: str | None = None,
        entry_skill_id: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        thread_id = await self._inner.create_thread(
            cwd=cwd, entry_skill_id=entry_skill_id, source=source, extra=extra
        )
        # 拉取刚写入的 metadata 作为 hook 入参（兼容封装内 JsonlMessageStore 已 upsert 到 directory）
        meta = await self._directory.get_metadata(thread_id)
        if meta is None:
            # 极少数情况（用户传入自定义 store 不 upsert 元数据）：合成最小 metadata
            import time as _t
            meta = ThreadMetadata(
                thread_id=thread_id,
                created_at=_t.time(),
                updated_at=_t.time(),
                entry_skill_id=entry_skill_id or "general",
                source=source or "user",
                tags=(),
                extra={"cwd": cwd} if cwd is not None else {},
            )
        self._runner.spawn_on_thread_created(meta)
        return thread_id

    async def append(self, item: ResponseItem) -> None:
        await self._inner.append(item)
        self._runner.spawn_on_message_appended(item.thread_id, [item])

    async def append_batch(self, items: list[ResponseItem]) -> None:
        if not items:
            return
        await self._inner.append_batch(items)
        # 按 thread 分组发 hook
        by_thread: dict[str, list[ResponseItem]] = {}
        for it in items:
            by_thread.setdefault(it.thread_id, []).append(it)
        for tid, group in by_thread.items():
            self._runner.spawn_on_message_appended(tid, group)

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        return await self._inner.load_thread(thread_id)

    async def list_threads(
        self, *, cwd: str | None = None, limit: int = 50
    ) -> list[ThreadInfo]:
        return await self._inner.list_threads(cwd=cwd, limit=limit)

    async def select_resume_path(self, cwd: str) -> str | None:
        return await self._inner.select_resume_path(cwd)

    async def close(self) -> None:
        # 不在此处 shutdown runner —— 由 pool.close 统一调度（先 await hook，后关 store）
        await self._inner.close()


class EnginePool:
    """进程级单例 —— 持有所有共享依赖 + 缓存活跃 Engine。"""

    def __init__(
        self,
        *,
        skill_registry: SkillRegistry,
        model_client: ModelClient,
        store: MessageStore,
        tool_registry: ToolRegistry,
        compressors: list[CompressionStrategy] | None = None,
        budget: ContextBudget | None = None,
        dispatch_policy: DispatchPolicy | None = None,
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
        instruction_layers: list[Any] | None = None,
        hook_runner: HookRunner | None = None,
        script_executors: dict[str, Any] | None = None,
        event_queue_size: int = 65536,
        event_high_water_ratio: float = 0.75,
        event_low_water_ratio: float = 0.5,
        event_warn_cooldown_sec: int = 5,
        submission_queue_size: int = 256,
        permission_policy: Any = None,
        request_metadata: dict[str, Any] | None = None,
        max_concurrent_spawns: int = 16,
        max_total_spawns: int = 1000,
        max_session_tokens: int | None = None,
        memory_store: Any = None,
        memory_query_builder: Any = None,
        pinned_state_sources: list[Any] | None = None,
        pinned_total_max_chars: int = 8000,
    ) -> None:
        self._registry = skill_registry
        self._model_client = model_client
        self._store = store
        self._tool_registry = tool_registry
        self._tool_runtime = ToolCallRuntime(tool_registry)
        self._compressors = (
            CompressionOrchestrator(compressors) if compressors else None
        )
        self._budget = budget or ContextBudget()
        self._dispatch_policy = dispatch_policy or DispatchPolicy()
        self._hooks = hooks
        self._max_iterations = max_iterations
        # turn-resource-guards：denial 断路器配置，透传到每个 AgentEngine。
        self._denial_breaker_config = denial_breaker_config
        self._doom_loop_config = doom_loop_config
        # failure-suspension-policy：失败处置裁决 policy,透传到每个 AgentEngine。
        self._failure_policy = failure_policy
        # 构造期校验(与 engine 同口径,报错点贴近配置点)
        if (failure_suspend_ttl_seconds is not None
                and failure_suspend_ttl_seconds <= 0):
            raise ValueError(
                f"failure_suspend_ttl_seconds must be positive or None, "
                f"got {failure_suspend_ttl_seconds}")
        self._failure_suspend_ttl_seconds = failure_suspend_ttl_seconds
        if (failure_suspend_max_auto_retries is not None
                and failure_suspend_max_auto_retries <= 0):
            raise ValueError(
                f"failure_suspend_max_auto_retries must be positive or None, "
                f"got {failure_suspend_max_auto_retries}")
        self._failure_suspend_max_auto_retries = failure_suspend_max_auto_retries
        if max_session_tokens is not None and max_session_tokens <= 0:
            raise ValueError(
                f"max_session_tokens must be positive or None, "
                f"got {max_session_tokens}")
        self._failure_suspend_on_expire = failure_suspend_on_expire
        # suspension-ttl：壁钟工厂(测试可注入固定时钟),透传到每个 AgentEngine。
        self._now_factory = now_factory
        # 单 turn 内一批 tool call 的最大并发；默认 1=串行。透传到 AgentEngine。
        self._max_parallel_tool_calls = max_parallel_tool_calls
        # reasoning-content-passback：thinking 模型 reasoning 回传开关，透传到 AgentEngine。
        self._reasoning_passback = reasoning_passback
        # 审计可观测 层1：LLM request 全文留痕开关，透传到 AgentEngine（默认关）
        self._enable_request_capture = enable_request_capture
        # Phase 0: instruction_layers 仅占位存储 + 透传到 AgentEngine
        self._instruction_layers: list[Any] = list(instruction_layers or [])
        # store-protocol-decoupling: hook_runner 由 EnginePool.create 注入；用户没传 index_hook 时为 None
        self._hook_runner = hook_runner
        # T5 scripts-runtime: ScriptLanguage → ScriptExecutor 映射
        self._script_executors: dict[str, Any] = dict(script_executors or {})
        # config-consistency-fixes C2: 透传到 AgentEngine 让 event_queue_size 真正生效
        # 审计可观测 层1：默认 65536（有界大容量，不 OOM）+ 高/低水位/限频可配。
        self._event_queue_size = event_queue_size
        self._event_high_water_ratio = event_high_water_ratio
        self._event_low_water_ratio = event_low_water_ratio
        self._event_warn_cooldown_sec = event_warn_cooldown_sec
        self._submission_queue_size = submission_queue_size
        # PermissionPolicy / request_metadata 注入路径：pool 透传到 AgentEngine
        # 再透传到 TurnRunner。request_metadata 是业务侧不透明上下文（无业务命名
        # 字段，R1），合并进 PermissionRequest.metadata；taifeng 不解析其 keys。
        self._permission_policy = permission_policy
        self._request_metadata: dict[str, Any] = dict(request_metadata or {})
        # K1/K2 内核资源控制：spawn 配额 + 会话 token 上限，透传到 AgentEngine
        self._max_concurrent_spawns = max_concurrent_spawns
        self._max_total_spawns = max_total_spawns
        self._max_session_tokens = max_session_tokens
        # K3：长期记忆 swap 接口（None=无内存层级）
        self._memory_store = memory_store
        # prefetch query 构造器，透传到 AgentEngine
        self._memory_query_builder = memory_query_builder
        # postcompact-state-reinjection：pinned 源列表 + 总预算，透传到 AgentEngine
        self._pinned_state_sources: list[Any] = list(pinned_state_sources or [])
        self._pinned_total_max_chars = pinned_total_max_chars

        self._engines: dict[str, AgentEngine] = {}
        self._engine_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._root_cancel = CancellationToken(name="pool")
        self._closed = False
        self._watcher_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        *,
        skills_dir: str | Path,
        threads_dir: str | Path | None = None,
        model_client: ModelClient,
        extra_tools: list[ToolSpec] | None = None,
        compressors: list[CompressionStrategy] | None = None,
        budget: ContextBudget | None = None,
        dispatch_policy: DispatchPolicy | None = None,
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
        auto_watch_skills: bool = False,
        watch_poll_interval_seconds: float = 1.0,
        instruction_layers: list[Any] | None = None,
        script_executors: dict[str, Any] | None = None,
        # store-protocol-decoupling T5 新增三参数：
        storage_dir: str | Path | None = None,
        thread_directory: ThreadDirectory | None = None,
        index_hook: IndexHook | None = None,
        sink: "TelemetrySink | None" = None,
        # config-consistency-fixes C2: 透传到 AgentEngine
        # 审计可观测 层1：默认 65536（有界大容量）+ 高/低水位比例 + 告警限频秒数
        event_queue_size: int = 65536,
        event_high_water_ratio: float = 0.75,
        event_low_water_ratio: float = 0.5,
        event_warn_cooldown_sec: int = 5,
        submission_queue_size: int = 256,
        # G1 mcp-server-hitl-elicitation T1: HITL 注入参数
        permission_policy: Any = None,
        request_metadata: dict[str, Any] | None = None,
        # K1/K2 内核资源控制（机制在内核，上限值业务在此注入）
        max_concurrent_spawns: int = 16,
        max_total_spawns: int = 1000,
        max_session_tokens: int | None = None,
        memory_store: Any = None,
        memory_query_builder: Any = None,
        pinned_state_sources: list[Any] | None = None,
        pinned_total_max_chars: int = 8000,
    ) -> EnginePool:
        """便捷构造。

        ``threads_dir`` (旧名) 与 ``storage_dir`` (新名) 等价，至少需提供一个：

        - 旧调用方式：``EnginePool.create(threads_dir=...)`` 走默认 JsonlMessageStore（含 SQLite 索引）
        - 新调用方式：``EnginePool.create(storage_dir=...)`` 同上，命名更准确
        - 显式注入：``thread_directory=`` 替换默认 SqliteThreadDirectory（Redis / PG / Null）
        - 业务事件：``index_hook=`` 订阅 thread 生命周期（fire-and-forget）
        - 事件总线：``sink=`` 接收 hook 失败 / 持久化层事件
        """
        # 统一为 storage_dir
        resolved_storage = Path(storage_dir or threads_dir or "").expanduser().resolve() if (storage_dir or threads_dir) else None
        if resolved_storage is None:
            raise ValueError("必须提供 storage_dir 或 threads_dir 之一")

        registry = await FilesystemSkillRegistry.load(skills_dir)

        # 默认 store：JsonlMessageStore（内部已是 writer + SqliteThreadDirectory + NoopIndexHook 兼容封装）
        store: MessageStore = JsonlMessageStore(resolved_storage)

        # 显式覆盖 thread_directory（业务侧 Redis / PG / Null）
        # 注：当前实现下，覆盖 directory 需要业务侧自己构造适配 JsonlMessageWriter 的组合
        # 这里 thread_directory 主要用于 hook proxy 的 metadata 查询（_HookEmittingStore.create_thread）
        directory: ThreadDirectory
        if thread_directory is not None:
            directory = thread_directory
        else:
            # 默认从 storage_dir 推 SqliteThreadDirectory（与 JsonlMessageStore 内部用同一个 db_path）
            directory = SqliteThreadDirectory(
                resolved_storage / "taifeng-index.db",
                threads_dir=resolved_storage,
                sink=sink,
            )

        # 构造 HookRunner（若用户传了 index_hook，则真正发挥作用；否则用 NoopIndexHook 等价空操作）
        actual_hook = index_hook if index_hook is not None else NoopIndexHook()
        hook_runner = HookRunner(hook=actual_hook, sink=sink)

        # 用 _HookEmittingStore 包装真实 store，所有写完成后自动 spawn hook 后台 task
        wrapped_store: MessageStore = _HookEmittingStore(
            inner=store, runner=hook_runner, directory=directory
        )

        tools = ToolRegistry()
        tools.register(make_read_skill_tool())
        tools.register(make_call_skill_tool())
        tools.register(make_run_script_tool())
        for t in extra_tools or []:
            tools.register(t)

        if compressors is None:
            compressors = [
                HandoffCompactionStrategy(model_client=model_client),
                SlidingWindowStrategy(),
            ]

        pool = cls(
            skill_registry=registry,
            model_client=model_client,
            store=wrapped_store,
            tool_registry=tools,
            compressors=compressors,
            budget=budget,
            dispatch_policy=dispatch_policy,
            hooks=hooks,
            max_iterations=max_iterations,
            denial_breaker_config=denial_breaker_config,
            doom_loop_config=doom_loop_config,
            failure_policy=failure_policy,
            failure_suspend_ttl_seconds=failure_suspend_ttl_seconds,
            failure_suspend_max_auto_retries=failure_suspend_max_auto_retries,
            failure_suspend_on_expire=failure_suspend_on_expire,
            now_factory=now_factory,
            max_parallel_tool_calls=max_parallel_tool_calls,
            reasoning_passback=reasoning_passback,
            enable_request_capture=enable_request_capture,
            instruction_layers=instruction_layers,
            hook_runner=hook_runner,
            script_executors=script_executors,
            event_queue_size=event_queue_size,
            event_high_water_ratio=event_high_water_ratio,
            event_low_water_ratio=event_low_water_ratio,
            event_warn_cooldown_sec=event_warn_cooldown_sec,
            submission_queue_size=submission_queue_size,
            permission_policy=permission_policy,
            request_metadata=request_metadata,
            max_concurrent_spawns=max_concurrent_spawns,
            max_total_spawns=max_total_spawns,
            max_session_tokens=max_session_tokens,
            memory_store=memory_store,
            memory_query_builder=memory_query_builder,
            pinned_state_sources=pinned_state_sources,
            pinned_total_max_chars=pinned_total_max_chars,
        )

        if auto_watch_skills:
            from taifeng.skill.watcher import SkillFileWatcher

            async def _on_change(snap) -> None:
                logger.info("skill snapshot refreshed via watcher → version=%d", snap.version)
                # 通知所有活跃 engine（lock-in 语义：当前 turn 不变；下个 turn 取新 snapshot）
                async with pool._lock:
                    for eng in pool._engines.values():
                        from taifeng.loop.submission import RefreshSnapshot
                        await eng.submit(RefreshSnapshot())

            watcher = SkillFileWatcher(
                registry,
                poll_interval_seconds=watch_poll_interval_seconds,
                on_change=_on_change,
            )
            pool._watcher_task = asyncio.create_task(watcher.run())
            pool._watcher = watcher

        return pool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def skill_registry(self) -> SkillRegistry:
        return self._registry

    @property
    def store(self) -> MessageStore:
        return self._store

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def introspect(self) -> dict[str, dict[str, Any]]:
        """K6：池级 /proc 视图 —— 各活跃 session_id → engine.introspect() 快照。

        运维一眼看清:哪些 session 活着、各自在飞 turn / spawn / token 用量。纯读。
        """
        return {sid: eng.introspect() for sid, eng in self._engines.items()}

    async def get_or_create(
        self,
        *,
        session_id: str,
        entry_skill_id: str,
        cwd: str | None = None,
        resume_thread_id: str | None = None,
    ) -> AgentEngine:
        """按 session_id 缓存 Engine 实例。

        如果不存在，构造新 Engine 并启动 actor。

        Args:
            resume_thread_id: 从已存在的 thread 续接。``None`` → 既有行为
                （新建 thread）；非 ``None`` → 调 ``store.load_thread``
                物化历史 + 用同一 thread_id 构造 engine，**不**新建 thread。
                未知 thread_id 抛 ``ValueError``，不静默回退。session_id
                已有 cached engine 时本参数被忽略（既有 cache 命中优先）。
                详见 spec ``jsonl-transcript`` / change ``engine-resume-by-thread-id``。
        """
        async with self._lock:
            if self._closed:
                raise RuntimeError("EnginePool closed")
            if session_id in self._engines:
                # 既有 cache 命中：忽略 resume_thread_id（与既有语义一致）
                return self._engines[session_id]

            snapshot = self._registry.snapshot()
            entry = snapshot.get(entry_skill_id)
            if entry is None:
                raise ValueError(f"unknown skill: {entry_skill_id}")
            if not entry.entry:
                raise ValueError(f"skill {entry_skill_id!r} is not entry-eligible")

            # === engine-resume-by-thread-id ===
            # 分支：resume 已有 thread vs. 新建 thread
            initial_history: list = []
            if resume_thread_id is not None:
                # 物化 store 中的历史
                gen = await self._store.load_thread(resume_thread_id)
                initial_history = [it async for it in gen]
                if not initial_history:
                    # 不存在 / 空 thread 都拒绝静默回退，避免业务以为 resume 成功
                    raise ValueError(
                        f"resume_thread_id {resume_thread_id!r} not found "
                        f"or empty thread"
                    )
                thread_id = resume_thread_id
            else:
                thread_id = await self._store.create_thread(
                    cwd=cwd,
                    entry_skill_id=entry_skill_id,
                    source=f"session:{session_id}",
                )

            engine = AgentEngine(
                entry_skill=entry,
                skill_snapshot=snapshot,
                tool_runtime=self._tool_runtime,
                model_client=self._model_client,
                store=self._store,
                thread_id=thread_id,
                session_id=session_id,
                compressors=self._compressors,
                dispatch_policy=self._dispatch_policy,
                budget=self._budget,
                hooks=self._hooks,
                max_iterations=self._max_iterations,
                denial_breaker_config=self._denial_breaker_config,
                doom_loop_config=self._doom_loop_config,
                failure_policy=self._failure_policy,
                failure_suspend_ttl_seconds=self._failure_suspend_ttl_seconds,
                failure_suspend_max_auto_retries=self._failure_suspend_max_auto_retries,
                failure_suspend_on_expire=self._failure_suspend_on_expire,
                now_factory=self._now_factory,
                max_parallel_tool_calls=self._max_parallel_tool_calls,
                reasoning_passback=self._reasoning_passback,
                enable_request_capture=self._enable_request_capture,
                instruction_layers=self._instruction_layers,
                script_executors=self._script_executors,
                event_queue_size=self._event_queue_size,
                event_high_water_ratio=self._event_high_water_ratio,
                event_low_water_ratio=self._event_low_water_ratio,
                event_warn_cooldown_sec=self._event_warn_cooldown_sec,
                submission_queue_size=self._submission_queue_size,
                initial_history=initial_history if resume_thread_id else None,
                permission_policy=self._permission_policy,
                request_metadata=self._request_metadata,
                max_concurrent_spawns=self._max_concurrent_spawns,
                max_total_spawns=self._max_total_spawns,
                max_session_tokens=self._max_session_tokens,
                memory_store=self._memory_store,
                memory_query_builder=self._memory_query_builder,
                pinned_state_sources=self._pinned_state_sources,
                pinned_total_max_chars=self._pinned_total_max_chars,
            )
            # 让 engine 能在收到 RefreshSnapshot 时拉最新快照
            engine._registry_ref = self._registry  # type: ignore[attr-defined]
            # T4: 启动期一次性 resolve engine scope（fail-fast；失败抛给业务侧）
            await engine.warmup_engine_scope()
            engine_cancel = self._root_cancel.child(f"session:{session_id}")
            task = asyncio.create_task(engine.run(engine_cancel))
            self._engines[session_id] = engine
            self._engine_tasks[session_id] = task

            # detached-spawn 冷恢复:resume 已有 thread 时,从 parent thread 持久项
            # 重建 spawn 句柄表 / barrier / fired 守卫集(R5 可 resume)。仅 resume 路径
            # 走此重建(新建 thread 无 prior spawn),恰好一次。重建内部会 _check_barriers
            # 补触发未 fired 的全终态 barrier;已 fired 的因守卫集存在被幂等跳过。
            # 放在 run() 任务起后:_root_cancel 已就绪,补触发的聚合 turn 可派生子 token。
            if resume_thread_id is not None:
                await engine._rebuild_spawn_state_from_history()  # noqa: SLF001
                # 冷重建完成后补发 rewind_table_rebuilt（R3 可观测）；
                # _emit 要求事件循环已就绪（run() task 已起）——此处满足。
                await engine._emit_rewind_table_rebuilt()  # noqa: SLF001

            # engine-resume-by-thread-id: resume 路径 emit ThreadResumed
            # 在 engine.run 启动 task 之后投递，让订阅者（业务可在 pool.get_or_create
            # 返回后立刻 subscribe_all）能消费。订阅者尚未 attach 的事件会丢
            # —— 既有 emit 语义，可接受。
            if resume_thread_id is not None:
                from taifeng.loop.event import EventMsg, ThreadResumed
                await engine._emit(EventMsg(  # noqa: SLF001
                    submission_id="*",
                    msg=ThreadResumed(data={
                        "thread_id": resume_thread_id,
                        "item_count": len(initial_history),
                        "entry_skill_id_at_resume": entry_skill_id,
                        # 暂不查 directory metadata（首版简化；后续可加 directory ref）
                        "entry_skill_id_recorded": None,
                    }),
                ))
            return engine

    async def release(self, session_id: str, *, force: bool = False) -> None:
        """释放并关停指定 session 的 engine。

        引用计数保活（detached-spawn）：engine 仍有未终结 detached spawn
        （running / suspended）时**不得**释放——否则后台 detached 子任务 / 待
        resume 的挂起 spawn 会随 engine 取消而丢失。此时静默跳过释放（engine
        仍在缓存里继续服务），等 spawn 全终态后调用方可再次 release。

        Args:
            session_id: 要释放的会话 id。
            force: True 时无视 has_live_spawns 强制释放（pool.close 全局拆除走此路）。
        """
        async with self._lock:
            engine = self._engines.get(session_id)
            # 保活闸：非 force 且仍有 live spawn → 不弹出、不关停，原样保留。
            if engine is not None and not force and engine.has_live_spawns():
                return
            engine = self._engines.pop(session_id, None)
            task = self._engine_tasks.pop(session_id, None)
        if engine is not None:
            await engine.shutdown()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            ids = list(self._engines.keys())
        for sid in ids:
            # 全局拆除：force 释放，无视保活闸（进程退出时 detached spawn 也应随之停）。
            await self.release(sid, force=True)
        # 停止 watcher
        if self._watcher_task is not None and not self._watcher_task.done():
            watcher = getattr(self, "_watcher", None)
            if watcher is not None:
                watcher.stop()
            try:
                await asyncio.wait_for(self._watcher_task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                self._watcher_task.cancel()
        self._root_cancel.cancel()
        # store-protocol-decoupling T5: 先 await hook runner（5s grace + cancel overrun），
        # 再关 store；保证 pending hook 在 store 关闭前完成或被显式 abandoned
        if self._hook_runner is not None:
            await self._hook_runner.shutdown(grace_seconds=5.0)
        await self._store.close()
