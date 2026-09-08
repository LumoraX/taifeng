"""engine Op handler：rewind / rollback / update_budget / update_instructions / refresh_snapshot。

从 ``engine.py`` 的「Op handlers」分段原样下沉（Wave 4 模块切分，行为零变化）。
本模块**无自有状态**，故按 `engine-module-structure` 契约落为**模块级函数**并显式
接收 engine（同 ``audit_tool.audited_tool_batch`` / ``pool_lifecycle.release_pool_session``），
不硬包成类。engine 侧在 `run()` 的提交分派处直接调用本模块函数。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import function_call, system_injection
from taifeng.instructions.source import InstructionFetchError
from taifeng.instructions.types import InstructionContext
from taifeng.loop.event import (
    EngineLog,
    EventMsg,
    InstructionUpdateRejected,
    InstructionUpdated,
    RewindRejected,
    RewindTableRebuilt,
    TurnRewound,
)
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.rewind import count_turns
from taifeng.loop.submission import Rewind, Submission, UpdateBudget, UpdateInstructions

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine

logger = logging.getLogger(__name__)


async def emit_rewind_rejected(
    engine: AgentEngine, submission_id: str, node_id: str, reason: str
) -> None:
    """rewind 校验失败统一出口(禁 silent fallback,显式发事件)。"""
    await engine._emit(EventMsg(
        submission_id=submission_id,
        msg=RewindRejected(data={"node_id": node_id, "reason": reason}),
    ))

async def emit_rewind_table_rebuilt(engine: AgentEngine) -> None:
    """冷恢复后补发 rewind_table_rebuilt（R3 可观测）。

    pool resume 路径在 _rebuild_spawn_state_from_history 之后调用本方法，
    告知订阅者冷重建已完成、节点表已就绪。
    turn_count 取 history 内 user_message 数（与 count_turns 一致）。
    submission_id 用 '*' 标记系统级事件（不属于某次具体 submission）。
    """
    await engine._emit(EventMsg(
        submission_id="*",
        msg=RewindTableRebuilt(data={
            "thread_id": engine._thread_id,
            "turn_count": count_turns(engine._history),
            "node_count": len(engine._rewind_checkpoints),
        }),
    ))

def rewrite_seed_args(engine: AgentEngine, call_id: str, new_args: dict[str, Any]) -> None:
    """retry_tool new_args：把内存 history 中该 call_id 的 function_call 换成新 args。

    只改内存(自洽 + 供重跑读新参);store 保持 append-only,arg 覆盖经 rewind
    marker 留痕。调用方已持锁。
    """
    for i, item in enumerate(engine._history):
        if item.kind == "function_call" and item.payload.get("call_id") == call_id:
            engine._history[i] = function_call(
                call_id=call_id, name=item.payload["name"],
                arguments=json.dumps(new_args, ensure_ascii=False),
                thread_id=engine._thread_id,
            )

async def handle_rewind(
    engine: AgentEngine, sub: Submission, root_cancel: CancellationToken
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
    if op.thread_id is not None and op.thread_id != engine._thread_id:
        await engine._spawn.rewind_spawn(sub)
        return

    # 1. 查 checkpoint(最近一次 root turn 回写的节点表)
    cp = next(
        (c for c in engine._rewind_checkpoints if c.node_id == op.node_id), None
    )
    if cp is None:
        await emit_rewind_rejected(engine, sub.id, op.node_id, "unknown_node")
        return
    # 2. mode/kind 相容:retry_tool 仅 dispatch 节点(且有 inner 切点)
    if op.mode == "retry_tool" and (
        cp.kind != "dispatch" or cp.inner_history_len is None
    ):
        await emit_rewind_rejected(engine, sub.id, op.node_id, "mode_kind_mismatch")
        return
    # 3. 挂起态守卫:活跃挂起的 turn v1 不支持 rewind(挂起态 rewind 留待后续)
    if engine._find_active_suspension() is not None:
        await emit_rewind_rejected(engine, sub.id, op.node_id, "turn_suspended")
        return

    # 4. 选截点:retry_tool 用 inner(保 fc);其余用 history_len(re_reason)
    cut = (
        cp.inner_history_len
        if op.mode == "retry_tool" and cp.inner_history_len is not None
        else cp.history_len
    )

    # 5. 截断 history + 回退 cache_anchor(锁内;append-only:store 不删,仅内存截)
    async with engine._lock:
        engine._history = engine._history[:cut]
        if engine._cache_anchor_index >= cut:
            engine._cache_anchor_index = cut - 1
        # 5b. retry_tool + new_args:改写悬空 fc 的 arguments(自洽 + 重跑用新参)
        if op.mode == "retry_tool" and op.new_args is not None and cp.call_id:
            rewrite_seed_args(engine, cp.call_id, op.new_args)

    # 6. marker(审计;同 rollback 范式,落 store、不进 history)
    # cut_index 持久化：供 reconstruct_logical_history 冷恢复时按截断点重建逻辑 history
    marker = system_injection(
        f"[rewind] node={op.node_id} kind={cp.kind} mode={op.mode}",
        thread_id=engine._thread_id, source="rewind",
        extra={"cut_index": cut},
    )
    await engine._store.append(marker)

    # 7. emit turn_rewound(R3)
    await engine._emit(EventMsg(submission_id=sub.id, msg=TurnRewound(data={
        "node_id": op.node_id, "node_kind": cp.kind, "mode": op.mode,
        "cut_index": cut, "cache_anchor": engine._cache_anchor_index,
    })))

    # 8a. 冷 engine 惰性 resolve 指令层（spec §7 lazy-on-rewind）：
    #   正常 turn 结束后 _last_resolved 已由 _handle_user_message 填充；
    #   冷 engine（initial_history 注入、未跑任何 turn）_last_resolved 为空。
    #   此处检测：resolver 存在 + _last_resolved 空 + history 非空 → 补一次
    #   resolve，以构造期 entry skill 为锚点（已知限制：不还原历史 turn 里曾
    #   使用的不同 entry skill 的指令层，v1 范围外）。
    if (
        engine._instruction_resolver is not None
        and not engine._last_resolved
        and engine._history
    ):
        # cancel=None:rewind 时无活跃 turn-level token,刻意不传(同 warmup_engine_scope)
        ctx = InstructionContext(
            session_id=engine._session_id,
            thread_id=engine._thread_id,
            entry_skill_id=engine._entry_skill.id,
            turn_index=engine._turn_index,
            metadata=engine._request_metadata,
            cancel=None,
        )
        # best-effort:turn_rewound 已发出,resolve 失败不硬 abort(会留下不一致),
        # 但不静默——按仓库惯例(turn.py on_pre_evict)落 warning 日志,保留可观测(R3)
        try:
            engine._last_resolved = await engine._instruction_resolver.resolve(
                ("engine", "session", "turn"), ctx,
            )
        except InstructionFetchError:
            logger.warning(
                "冷 rewind 指令 resolve 失败,以空指令层续推(thread=%s)",
                engine._thread_id,
            )

    # 8b. 主动重推:截断后建新 root TurnRunner;retry_tool 先补跑悬空 call
    turn_cancel = root_cancel.child(f"sub:{sub.id}")
    engine._pending[sub.id] = _PendingTurn(
        submission_id=sub.id, cancel=turn_cancel
    )
    seed = cp.call_id if op.mode == "retry_tool" else None
    await engine._build_and_run_runner(
        sub.id, turn_cancel, list(engine._last_resolved or []),
        seed_pending_call_id=seed,
        cache_break_expected_reason="rewind",
    )

async def handle_rollback(engine: AgentEngine, submission_id: str, num_turns: int) -> None:
    """回滚最近 N 轮对话。

    一"轮" = 以 user_message 为锚点。从 history 末尾向前数 N 个 user_message，
    删掉它及之后的所有 items。
    """
    if num_turns < 1:
        return
    async with engine._lock:
        new_history = list(engine._history)
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
        engine._history = new_history[:cut_idx]
        # cache anchor 也要回退
        if engine._cache_anchor_index >= cut_idx:
            engine._cache_anchor_index = cut_idx - 1

    # 写一条 system_injection 标记
    # cut_index 持久化：供 reconstruct_logical_history 冷恢复时按截断点重建逻辑 history
    marker = system_injection(
        f"[rollback] dropped {removed} item(s), {num_turns} turn(s)",
        thread_id=engine._thread_id,
        source="rollback",
        extra={"cut_index": cut_idx},
    )
    await engine._store.append(marker)
    await engine._emit(
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

def handle_update_budget(engine: AgentEngine, submission_id: str, op: UpdateBudget) -> None:
    """运行时调整 ContextBudget（部分字段）。"""
    cur = engine._budget
    engine._budget = ContextBudget(
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
        engine._budget.context_window,
        engine._budget.soft_limit_ratio,
        engine._budget.hard_limit_ratio,
        engine._budget.preserve_tail_messages,
    )

async def handle_update_instructions(
    engine: AgentEngine, submission_id: str, op: UpdateInstructions,
) -> None:
    """T4: 替换 layer 的 source + 失效缓存 + 发事件。

    spec Requirement (热更):
        - 成功 → instruction_updated（含 layer_name / new_source_kind）
        - 未知 name → instruction_update_rejected（reason='unknown_layer'）
        - 缓存立即失效（resolver.replace_layer 内部已清理）
    """
    if engine._instruction_resolver is None:
        # 没配 resolver → 视为未知 name
        await engine._emit(EventMsg(
            submission_id=submission_id,
            msg=InstructionUpdateRejected(data={
                "layer_name": op.layer_name,
                "reason": "no_instruction_resolver",
            }),
        ))
        return
    engine._current_emit_submission_id = submission_id
    try:
        ok = engine._instruction_resolver.replace_layer(
            op.layer_name, op.new_source,
        )
    finally:
        engine._current_emit_submission_id = "*"
    if not ok:
        await engine._emit(EventMsg(
            submission_id=submission_id,
            msg=InstructionUpdateRejected(data={
                "layer_name": op.layer_name,
                "reason": "unknown_layer",
            }),
        ))
        return
    new_kind = "static" if isinstance(op.new_source, str) else "dynamic"
    await engine._emit(EventMsg(
        submission_id=submission_id,
        msg=InstructionUpdated(data={
            "layer_name": op.layer_name,
            "new_source_kind": new_kind,
        }),
    ))

def handle_refresh_snapshot(engine: AgentEngine, submission_id: str) -> None:
    """从 registry 拉最新 snapshot（业务侧热更 SKILL.md 后调）。

    注意：当前 entry_skill 引用保持不变（lock-in 语义）。
    """
    # 找父级 registry —— 由业务侧通过 _registry_ref 注入；否则 noop
    registry = getattr(engine, "_registry_ref", None)
    if registry is None:
        logger.warning("refresh_snapshot: no registry ref")
        return
    engine._snapshot = registry.snapshot()
    logger.info("snapshot refreshed → version=%d", engine._snapshot.version)
