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
from typing import Any

from taifeng.context.budget import (
    ContextBudget,
    estimate_history_bytes,
    estimate_history_tokens,
)
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
)
from taifeng.conversation.store import MessageStore
from taifeng.llm.client import ModelClient
from taifeng.llm.errors import (
    LLMError,
    RequestTooLargeError,
    classify_failure,
)
from taifeng.llm.recovery import recommend_recovery
from taifeng.llm.types import TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.denial_breaker import DenialBreaker, DenialBreakerConfig
from taifeng.loop.iteration_budget import IterationBudget
from taifeng.loop.event import (
    AssistantReasoning,
    AssistantText,
    CacheBreakDetected,
    CompactionCompleted,
    CompactionDegradationWarning,
    CompactionIntegrityRolledBack,
    CompactionStarted,
    ContextBudgetExceeded,
    EventMsg,
    PreCompactHookSkipped,
    ProviderRetry,
    ResourceLimitExceeded,
    DenialCircuitOpen,
    SkillDispatched,
    SkillReturned,
    RewindCheckpointRecorded,
    SkillSpawnRejected,
    SubagentPolicyOverridden,
    ToolBatchDispatched,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    TurnSuspended,
    UserInputInjected,
)
from taifeng.loop.prompt import build_api_request
from taifeng.loop.rewind import RewindLog, count_turns
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.dispatch import CallStack, DispatchPolicy
from taifeng.skill.eligibility import RuntimeCapabilities
from taifeng.skill.registry import SkillSnapshot
from taifeng.suspend.signal import SuspendSignal  # 运行时 except 捕获，不可放 TYPE_CHECKING
from taifeng.tool.runtime import ToolCallRuntime
from taifeng.tool.spec import ToolContext

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


def _should_suspend_on_error(err: Exception) -> bool:
    """LLMError 是否转 SYSTEM_RETRY 挂起(等外部条件清除后重跑同次 sample 可过)。

    判据(见 spec §5.3):retryable 为真,或 failure_class 属"等外部介入"类
    (鉴权 / 配额 / 余额)。ContentFilter / ContextOverflow / InvalidRequest
    这类确定性失败不挂起,照旧硬失败(resume 也救不回,只会无限挂)。

    Args:
        err: 待判定的异常;非 LLMError 一律返回 False(不挂起)。

    Returns:
        True 表示应转 SYSTEM_RETRY 挂起;False 表示硬失败照旧上抛。
    """
    from taifeng.llm.errors import LLMError

    if not isinstance(err, LLMError):
        return False
    # retryable 为真(限流 / 瞬时网络 / 5xx)→ 外部条件清除后重跑可过
    if getattr(err, "retryable", False):
        return True
    # 等外部介入类:鉴权(本仓库 provider_auth);配额 / 余额命名保留以兼容其他 provider
    return getattr(err, "failure_class", None) in (
        "provider_auth",
        "provider_quota",
        "provider_balance",
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
    # 单 turn 内一批 tool call 的最大并发数；默认 1 = 严格串行（等同历史行为，零回归）
    max_parallel_tool_calls: int = 1
    # turn-级累积 usage
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    history_buffer: list[ResponseItem] = field(default_factory=list)
    """In-memory 视图，与 store 同步追加。"""

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
        self._current_iteration = 0
        # DenialBreaker 实例在 run() 起点按 config 新建（turn 级生命周期）
        self._denial_breaker: DenialBreaker | None = None

    async def _emit(self, msg) -> None:
        try:
            await self.emit(EventMsg(submission_id=self.submission_id, msg=msg))
        except Exception:
            logger.exception("emit failed")

    async def _prefetch_memory(self) -> None:
        """K3 page-in：按最近用户消息 prefetch 长期记忆 → ``_prefetched_memory``。

        best-effort：``memory_store`` 抛异常被吞掉（内存层不得打断 turn）。
        """
        if self.memory_store is None:
            return
        query = ""
        for it in reversed(self.history_buffer):
            if it.kind == "user_message":
                query = str(it.payload.get("text", ""))
                break
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
                        await self._emit(ResourceLimitExceeded(data={
                            "limit_kind": "session_tokens",
                            "used": used,
                            "limit": self.max_session_tokens or 0,
                            "scope": "turn_aborted",
                        }))
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
                        end_reason = "denial_circuit_open"
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

        # 取可用 tool 集合（白名单）
        tools = []
        for name in sorted(self.entry_skill.tool_names | {"read_skill", "call_skill"}):
            spec = self.tool_runtime._registry.get(name)  # noqa: SLF001
            if spec is not None:
                tools.append(spec.to_ref())

        # G-CACHE：算本轮 prompt 结构指纹 + 归因结构性 cache 失效原因，再更新指纹
        prompt_fingerprint = self._compute_prompt_fingerprint(tools)
        structural_break_reason = self._detect_structural_break_reason(
            prompt_fingerprint
        )
        self.last_prompt_fingerprint = prompt_fingerprint

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
        )

        # G2b：发送前预检 —— 即便经过压缩，估算 token 仍超 hard limit 时 emit
        # 非阻塞告警（估算偏粗，不据此拒发；供业务侧主动限流 / 排查 provider 400）
        preflight_tokens = estimate_history_tokens(self.history_buffer)
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
        max_bytes = self.budget.max_request_bytes
        if max_bytes is not None:
            request_bytes = estimate_history_bytes(self.history_buffer)
            if request_bytes > max_bytes:
                raise RequestTooLargeError(
                    f"request body ~{request_bytes}B 超出上限 {max_bytes}B",
                    estimated_bytes=request_bytes,
                    max_bytes=max_bytes,
                )

        sess = self.model_client.session(cancel=self.cancel)
        assistant_text = ""
        # 累积 tool calls
        tool_calls: list[dict[str, Any]] = []
        # retry 已由 provider 内 retry_async 兜底(≤3 次);走到这里的 LLMError 即重试耗尽。
        # 可恢复 / 等外部介入类 → 转 SYSTEM_RETRY 挂起(等业务侧 resume 重跑同次 sample);
        # 确定性失败照旧上抛硬失败。CancelledError 走 asyncio 路径,不在此 except 内。
        try:
            async with sess as s:
                async for ev in s.stream(request):
                    self.cancel.raise_if_cancelled()
                    if ev.kind == "text_delta":
                        delta = ev.data.get("text", "")
                        assistant_text += delta
                        await self._emit(AssistantText(data={"delta": delta}))
                    elif ev.kind == "reasoning_delta":
                        await self._emit(
                            AssistantReasoning(data={"delta": ev.data.get("delta", "")})
                        )
                    elif ev.kind == "tool_call_done":
                        tool_calls.append(
                            {
                                "call_id": ev.data["call_id"],
                                "name": ev.data["name"],
                                "arguments": ev.data.get("arguments", "{}"),
                            }
                        )
                    elif ev.kind == "completed":
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
                        recorder = getattr(self.model_client, "record_cache_read", None)
                        if callable(recorder):
                            try:
                                recorder(cache_read)
                            except Exception:
                                logger.debug("record_cache_read on client failed", exc_info=True)
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
            if _should_suspend_on_error(e):
                # 可恢复错误且重试已耗尽 → 转 SYSTEM_RETRY 挂起,等业务侧 resume 重跑同次 sample。
                # 该 SuspendSignal 穿透回 run_turn 的 except SuspendSignal(Task 7 已加),落盘挂起。
                from taifeng.suspend.reason import PendingRequest, SuspendReason
                from taifeng.suspend.signal import SuspendSignal as _SuspendSignal

                raise _SuspendSignal(
                    PendingRequest(
                        request_id=self._suspend_id_factory(),
                        reason=SuspendReason.SYSTEM_RETRY,
                        payload_schema={
                            "type": "object",
                            "properties": {"action": {"enum": ["retry", "abort"]}},
                        },
                        related_call_id=None,
                        detail={
                            "failure_class": getattr(e, "failure_class", None),
                            "retry_after_seconds": getattr(e, "retry_after_seconds", None),
                            "kind": type(e).__name__,
                        },
                    )
                ) from e
            raise  # 确定性失败:照旧上抛硬失败(走 run_turn 宽 except → TurnFailed)

        # 落 assistant message（即使为空也记下，因为 tool calls 也在这条消息上）
        if assistant_text or tool_calls:
            msg = assistant_message(
                assistant_text,
                thread_id=self.thread_id,
                model=self.entry_skill.model or "auto",
            )
            self.history_buffer.append(msg)
            await self.store.append(msg)

        if not tool_calls:
            return assistant_text, False

        # === 阶段 1：按发起序 emit Started + 解析参数 + 建请求（暂不写历史）===
        requests: list[ToolCallRequest] = []
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

        outcomes = await dispatch_batch(
            requests,
            runtime=self.tool_runtime,
            ctx_for=_ctx_for,
            hooks=self.hooks,
            emit=self._emit,
            semaphore=semaphore,
            thread_id=self.thread_id,
            submission_id=self.submission_id,
            entry_skill_id=self.entry_skill.id,
        )

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
            )
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
            fco_item = function_call_output(
                call_id=req.call_id,
                output=outcome.result.output,
                thread_id=self.thread_id,
                is_error=outcome.result.is_error,
            )
            self.history_buffer.append(fco_item)
            await self.store.append(fco_item)
            # turn-resource-guards：单点观察 deny/success 记账 + refunds_iteration 退还
            await self._note_tool_outcome(req.name, outcome.result)

        if suspended_pending:
            # 整批挂起 pending 上抛给 run()/run_turn 聚合落一条 SuspensionRecord。
            raise _BatchSuspend(tuple(suspended_pending))
        return assistant_text, True

    async def _note_tool_outcome(self, name: str, result: Any) -> None:
        """配对回填后的单点记账（turn-resource-guards）。

        - DenialBreaker：``ToolResult.data["reason"] ∈ {hook_denied,
          permission_denied}`` 计 deny（含 HITL ask 超时产生的 deny —— 它就是
          deny 结果）；成功结果重置 consecutive。其他 error 中性（不计任何一边）。
          恰好越阈值那次 emit ``denial_circuit_open``（单次闩锁）。
        - refund：spec 静态声明 ``refunds_iteration`` 且本次**成功** → 外层迭代
          预算退还一步（失败轮照常计费；不暴露为 LLM 可触发语义）。
        """
        deny_reason = result.data.get("reason") if result.is_error else None
        if self._denial_breaker is not None:
            if deny_reason in ("hook_denied", "permission_denied"):
                if self._denial_breaker.record_denial(name):
                    await self._emit(
                        DenialCircuitOpen(data=self._denial_breaker.snapshot())
                    )
            elif not result.is_error:
                self._denial_breaker.record_success()
        if not result.is_error and self.iteration_budget is not None:
            spec = self.tool_runtime.spec_for(name)
            if spec is not None and spec.refunds_iteration:
                self.iteration_budget.refund(1)

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
        )
        outcome = outcomes[0]
        if outcome.suspend is not None:
            raise _BatchSuspend((outcome.suspend,))
        fco = function_call_output(
            call_id=call_id, output=outcome.result.output,
            thread_id=self.thread_id, is_error=outcome.result.is_error,
        )
        self.history_buffer.append(fco)
        await self.store.append(fco)

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
            total_tokens=self.total_usage.total_tokens + (u.total_tokens or u.input_tokens + u.output_tokens),
            cache_creation_input_tokens=self.total_usage.cache_creation_input_tokens
            + u.cache_creation_input_tokens,
            cache_read_input_tokens=self.total_usage.cache_read_input_tokens
            + u.cache_read_input_tokens,
            reasoning_tokens=self.total_usage.reasoning_tokens + u.reasoning_tokens,
            raw=u.raw,
        )

    async def _drain_pending_input(self) -> None:
        """B1：把 pending_input 队列并入 history（迭代边界调用）。

        取出全部 pending（保留提交顺序），逐条追加 history_buffer + store.append +
        emit ``UserInputInjected{delivered:true}``，并清空队列。turn 已取消则不并入
        （留给 engine 收尾落历史，不丢用户输入，R5）。

        副作用：history_buffer / store 追加；pending_input 清空；emit 事件。
        """
        if not self.pending_input or self.cancel.is_cancelled:
            return
        # 同 event loop 协作式调度：取出 + 清空在无 await 的同步段完成，避免与
        # engine 主循环 append 竞态（无需锁）。
        drained = list(self.pending_input)
        self.pending_input.clear()
        for item in drained:
            self.history_buffer.append(item)
            await self.store.append(item)
            preview = str(item.payload.get("text", ""))[:80]
            await self._emit(
                UserInputInjected(
                    data={
                        "submission_id": self.submission_id,
                        "delivered": True,
                        "text_preview": preview,
                    }
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
        tokens = estimate_history_tokens(self.history_buffer)
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
    ):
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

        # K1 广度准入：spawn 配额（engine 注入 registry 时生效）。超限→拒绝派发，
        # 不创建 thread / 不跑子 turn，返回 error 让 LLM 自行调整（fork-bomb 防护）。
        if self.spawn_registry is not None:
            from taifeng.loop.spawn import SpawnLimitError
            from taifeng.tool.spec import ToolResult
            try:
                async with self.spawn_registry.reserve():
                    return await self._spawn_sub_runner(
                        target=target, arguments=arguments,
                        parent_stack=parent_stack, ctx=ctx,
                    )
            except SpawnLimitError as e:
                await self._emit(SkillSpawnRejected(data={
                    "skill_id": target.id,
                    "call_id": ctx.call_id,
                    "limit_kind": e.kind,
                    "limit": e.limit,
                }))
                return ToolResult.error(
                    f"spawn_limit_exceeded: {e.kind} >= {e.limit}",
                    reason="spawn_limit",
                )
        return await self._spawn_sub_runner(
            target=target, arguments=arguments,
            parent_stack=parent_stack, ctx=ctx,
        )

    async def _spawn_sub_runner(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
    ):
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
        # K7：把谱系（父 thread / spawn 深度 / 调用栈）持久化进 ThreadMetadata.extra，
        # 使 resume 可从持久谱系重导子 agent 的深度/特权（不改"独立 seed"隔离模型）。
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
            max_parallel_tool_calls=self.max_parallel_tool_calls,
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
            call_stack=parent_stack,
            history_buffer=[seed],
        )
        outcome = await sub_runner.run()
        from taifeng.tool.spec import ToolResult

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
        if outcome.success:
            return ToolResult.ok(outcome.final_text, sub_thread_id=sub_thread_id)
        return ToolResult.error(
            f"sub_skill_failed: {outcome.error or outcome.end_reason}",
            sub_thread_id=sub_thread_id,
        )
