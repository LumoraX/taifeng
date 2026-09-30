"""engine 预热：在用户输入到来之前做掉首轮采样的准备工作（prewarm，ADR 0092）。

```
Prewarm Op → 登记在飞预热 → 取 root gate
  ├─ instructions  解析指令层（首轮解析命中解析器缓存）
  ├─ working_set   由已有战绩重算工作集
  └─ model         组装下一次采样会发出的请求 → ModelPrewarmer.prewarm
→ 释放 root gate → prewarm_completed
```

三条约束：

1. **不留痕迹**：不改 history、不占用 turn 序号、不写 store、不产生 turn 事件。
2. **让路**：用户消息到达时取消未完成的预热（``yield_to_turn``），真实的 turn 不等它。
3. **失败不传染**：任何一步失败只记进 ``prewarm_completed``，之后的 turn 照常进行。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from taifeng.instructions.types import InstructionContext
from taifeng.loop.cancellation import CancelReason
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.event import EventMsg, PrewarmCompleted, PrewarmStarted
from taifeng.loop.working_set_events import emit_working_set_changes

if TYPE_CHECKING:
    from taifeng.instructions.types import ResolvedInstruction
    from taifeng.llm.types import TokenUsage
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.submission import Prewarm

logger = logging.getLogger(__name__)

_PREWARM_KIND = "prewarm"


class _Run:
    """一次预热的过程量。"""

    def __init__(self, steps: tuple[str, ...]) -> None:
        self.steps: dict[str, str] = dict.fromkeys(steps, "cancelled")
        self.errors: dict[str, str] = {}
        self.resolved: list[ResolvedInstruction] = []
        self.usage: TokenUsage | None = None


def yield_to_turn(engine: AgentEngine) -> None:
    """用户消息到达：取消未完成的预热，不让真实的 turn 等它。"""
    for pending in list(engine._pending.values()):  # noqa: SLF001
        if pending.kind == _PREWARM_KIND:
            pending.cancel.cancel(CancelReason.REQUESTED, "superseded_by_turn")


async def run_prewarm(
    engine: AgentEngine, submission_id: str, op: Prewarm, root_cancel: CancellationToken,
) -> None:
    """执行一次预热；永不上抛步骤里的失败。"""
    cancel = root_cancel.child(f"sub:{submission_id}:prewarm")
    pending = _PendingTurn(submission_id, cancel, is_root=False, kind=_PREWARM_KIND)
    engine._pending[submission_id] = pending  # noqa: SLF001
    run = _Run(op.steps)
    started = time.monotonic()
    await _emit(engine, submission_id, PrewarmStarted(data={"steps": list(op.steps)}))
    acquired = await engine._acquire_root_gate(submission_id, cancel)  # noqa: SLF001
    try:
        if acquired:
            await _run_steps(engine, submission_id, op, cancel, run)
    finally:
        engine._pending.pop(submission_id, None)  # noqa: SLF001
        if acquired:
            await _deliver_stray_input(engine, pending)
            engine._release_root_gate()  # noqa: SLF001
        await _emit(engine, submission_id, PrewarmCompleted(data={
            "steps": run.steps,
            "errors": run.errors,
            "cancelled": cancel.is_cancelled,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "usage": None if run.usage is None else run.usage.model_dump(),
        }))


async def _run_steps(
    engine: AgentEngine,
    submission_id: str,
    op: Prewarm,
    cancel: CancellationToken,
    run: _Run,
) -> None:
    """按给定顺序逐步执行；取消后其余步骤保持 ``cancelled``。"""
    handlers = {
        "instructions": _resolve_instructions,
        "working_set": _restore_working_set,
        "model": _prewarm_model,
    }
    for step in op.steps:
        if cancel.is_cancelled:
            return
        try:
            run.steps[step] = await handlers[step](engine, submission_id, cancel, run)
        except Exception as exc:
            if cancel.is_cancelled:
                # 取消引发的异常不算失败
                return
            run.steps[step] = "failed"
            run.errors[step] = f"{type(exc).__name__}: {exc}"
            logger.warning("prewarm step %s failed: %s", step, exc)


async def _resolve_instructions(
    engine: AgentEngine, submission_id: str, cancel: CancellationToken, run: _Run,
) -> str:
    """解析三档指令层；没有解析器时跳过。"""
    resolver = engine._instruction_resolver  # noqa: SLF001
    if resolver is None:
        return "skipped"
    engine._current_emit_submission_id = submission_id  # noqa: SLF001
    try:
        run.resolved = list(await resolver.resolve(
            ("engine", "session", "turn"),
            InstructionContext(
                session_id=engine._session_id,  # noqa: SLF001
                thread_id=engine._thread_id,  # noqa: SLF001
                entry_skill_id=engine._entry_skill.id,  # noqa: SLF001
                turn_index=engine._turn_index,  # noqa: SLF001
                metadata=engine._request_metadata,  # noqa: SLF001
                cancel=cancel,
            ),
        ))
    finally:
        engine._current_emit_submission_id = "*"  # noqa: SLF001
    return "resolved"


async def _restore_working_set(
    engine: AgentEngine, submission_id: str, cancel: CancellationToken, run: _Run,
) -> str:
    """由已有战绩重算工作集；未启用时跳过。"""
    policy = engine._dispatch_policy  # noqa: SLF001
    if policy.working_set is None:
        return "skipped"
    changes = await policy.working_set.restore(engine._snapshot, policy.trust)  # noqa: SLF001

    async def emit(msg: Any) -> None:
        await _emit(engine, submission_id, msg)

    await emit_working_set_changes(emit, changes)
    return "restored"


async def _prewarm_model(
    engine: AgentEngine, submission_id: str, cancel: CancellationToken, run: _Run,
) -> str:
    """把下一次采样会发出的请求交给模型侧预热；没有注入预热器时为 ``unsupported``。"""
    prewarmer = engine._model_prewarmer  # noqa: SLF001
    if prewarmer is None:
        return "unsupported"
    limit = engine._max_session_tokens  # noqa: SLF001
    if limit is not None and engine._session_tokens >= limit:  # noqa: SLF001
        # 会话 token 已触顶：预热不再消耗
        run.errors["model"] = "session_token_limit"
        return "skipped"
    runner = engine._new_turn_runner(submission_id, cancel, run.resolved)  # noqa: SLF001
    request = await runner._sample.preview_request()  # noqa: SLF001
    outcome = await prewarmer.prewarm(request, cancel=cancel)
    if outcome.usage is not None:
        run.usage = outcome.usage
        # 预热的消耗记进会话账，受会话上限约束
        engine._usage_meter.add(  # noqa: SLF001
            outcome.usage,
            thread_id=engine._thread_id,  # noqa: SLF001
            skill_id=engine._entry_skill.id,  # noqa: SLF001
        )
    return "primed" if outcome.primed else "unsupported"


async def _deliver_stray_input(engine: AgentEngine, pending: _PendingTurn) -> None:
    """预热期间误投到它名下的输入（peer 消息等）落回 root history，不丢（R5）。"""
    if not pending.pending_input:
        return
    items = list(pending.pending_input)
    pending.pending_input.clear()
    async with engine._lock:  # noqa: SLF001
        engine._history.extend(items)  # noqa: SLF001
    for item in items:
        await engine._store.append(item)  # noqa: SLF001


async def _emit(engine: AgentEngine, submission_id: str, msg: Any) -> None:
    """以预热的 submission 归因发事件。"""
    await engine._emit(EventMsg(submission_id=submission_id, msg=msg))  # noqa: SLF001


__all__ = ["run_prewarm", "yield_to_turn"]
