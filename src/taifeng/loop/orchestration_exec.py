"""编排执行器 —— 把 OrchestrationSpec 翻译成一串合成 call_skill 请求，复用 A 的 dispatch_batch。

「纯编排器」：orchestration 声明存在时本模块驱动子 skill 调用，**不采样 LLM**；每个子 skill
内部仍各自走 LLM（经 call_skill → 子 TurnRunner）。

R1：仅 skill id + 结构，无业务概念。R2：并行组复用 A 的「按发起序配对回填」→ 子 turn 历史
隔离，父无 cache anchor 改动。R3：emit orchestration_plan_resolved/condition_missing + 复用
A 的 tool_batch_dispatched。R4：dispatch_batch 内 cancel.child 级联 + 段间 raise_if_cancelled。
R5：历史按发起序配对回填 JSONL。

参照 design.md §3「OrchestrationPlanner」。差异：执行驱动落 loop 层
（需 dispatch_batch/store/emit），纯解析+校验在 skill/orchestration.py；
turn.py 已超 800 行红线，故执行体独立成模块。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import function_call, function_call_output
from taifeng.loop.event import (
    OrchestrationConditionMissing,
    OrchestrationPlanResolved,
    ToolBatchDispatched,
)
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch
from taifeng.skill.orchestration import (
    OrchestrationConditionError,
    ParallelStep,
    SerialStep,
    WhenStep,
    extract_condition_flag,
    plan_to_groups,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.loop.turn import TurnRunner
    from taifeng.skill.orchestration import OrchestrationSpec
    from taifeng.tool.spec import ToolContext


def _seed_input(runner: TurnRunner) -> str:
    """取 entry turn 的种子用户输入文本（history_buffer 中最近一条 user_message）。"""
    for item in reversed(runner.history_buffer):
        if item.kind == "user_message":
            return str(item.payload.get("text", ""))
    return ""


async def run_orchestrated_turn(runner: TurnRunner) -> str:
    """按 ``entry_skill.orchestration`` 确定性驱动子 skill 调用，返回最后一步输出汇总文本。

    调用方（TurnRunner.run）已保证 ``runner.entry_skill.orchestration`` 非 None。
    抛出的 ``OrchestrationConditionError`` 会被 run() 的通用 except 捕获 → turn 硬失败。
    """
    spec: OrchestrationSpec | None = runner.entry_skill.orchestration
    assert spec is not None  # 调用方契约

    # 复用 runner 的私有事件/上下文方法（执行体独立成模块，故此处显式取用）
    emit = runner._emit  # noqa: SLF001
    build_ctx = runner._build_tool_context  # noqa: SLF001

    await emit(
        OrchestrationPlanResolved(
            data={"skill_id": runner.entry_skill.id, "groups": plan_to_groups(spec)}
        )
    )

    seed = _seed_input(runner)
    prev_outputs: tuple[str, ...] = ()
    last_outputs: tuple[str, ...] = ()

    for step_idx, step in enumerate(spec.steps):
        runner.cancel.raise_if_cancelled()  # R4：段间取消边界

        # when → 选具体叶子（flag 缺失/非布尔 → emit 事件后抛错，硬失败）
        if isinstance(step, WhenStep):
            leaf = await _resolve_when(runner, step, prev_outputs, emit=emit)
            if leaf is None:
                # condition=False 且无 else → 跳过本段（无 child 执行），不更新 last_outputs
                prev_outputs = ()
                continue
        else:
            leaf = step

        # 同种子 + 前序输出注入（按「顶层 step 类型」判定，非叶子类型）：
        # 顶层 parallel step 只给 seed；serial / when 段额外注入上一步输出。
        # 即便 when 段的叶子是 parallel，作为「条件后续段」它仍应看到触发步的输出
        # （锁定决策原文：「serial/when 段额外注入」，段=顶层 step）。
        inject_upstream = not isinstance(step, ParallelStep)
        outputs = await _execute_leaf(
            runner, leaf,
            step_idx=step_idx,
            seed=seed,
            upstream=prev_outputs if inject_upstream else (),
            emit=emit, build_ctx=build_ctx,
        )
        prev_outputs = outputs
        last_outputs = outputs

    return "\n\n".join(last_outputs)


async def _resolve_when(
    runner: TurnRunner,
    step: WhenStep,
    prev_outputs: tuple[str, ...],
    *,
    emit: Callable[[Any], Awaitable[None]],
) -> ParallelStep | SerialStep | None:
    """读上一步 flag 选 then/else 叶子；flag 缺失/非布尔 → emit condition_missing + 抛错。

    返回 None 表示 condition=False 且无 else 分支（跳过本段）。
    """
    try:
        flag = extract_condition_flag(step.condition, prev_outputs)
    except OrchestrationConditionError:
        await emit(
            OrchestrationConditionMissing(
                data={"skill_id": runner.entry_skill.id, "condition": step.condition}
            )
        )
        raise  # 硬失败：交给 run() 通用 except → TurnFailed
    return step.then if flag else step.otherwise


def _replay_paired_output(runner: TurnRunner, call_id: str) -> str | None:
    """重放查询:**本 turn 区间内**该 call_id 已有 function_call_output → 返回其 output。

    resume 重入幂等的坐标即确定性 call_id(orch_{entry}_{step}_{sid}_{idx});
    gap 回填(挂起子被 Resume 补的 output)同样命中。无配对 → None(需派发)。

    扫描区间限定最后一条 user_message 之后:call_id 不含 turn 维度,同一 entry
    的每一轮 call_id 完全相同——若全量扫描,同 thread 第二条 UserMessage 会命中
    第一轮的 fco,整轮零派发复读旧答案。本 turn 的全部 fc/fco(含挂起后 gap 回填)
    都追加在该 turn 的 user_message 之后,区间即精确的重放作用域;无 user_message
    锚点(理论不可达,编排 turn 必有种子输入)→ 不重放,宁可重派发不可错命中。
    """
    items = runner.history_buffer
    start: int | None = None
    # 反向定位本 turn 起点(最后一条 user_message)
    for i in range(len(items) - 1, -1, -1):
        if items[i].kind == "user_message":
            start = i + 1
            break
    if start is None:
        return None
    for item in items[start:]:
        if (item.kind == "function_call_output"
                and item.payload.get("call_id") == call_id):
            return str(item.payload.get("output", ""))
    return None


async def _execute_leaf(
    runner: TurnRunner,
    leaf: ParallelStep | SerialStep,
    *,
    step_idx: int,
    seed: str,
    upstream: tuple[str, ...],
    emit: Callable[[Any], Awaitable[None]],
    build_ctx: Callable[[str, int], ToolContext],
) -> tuple[str, ...]:
    """把一个叶子翻译成一批合成 call_skill 请求，经 A 的 dispatch_batch 执行。

    - parallel 段：Semaphore(max_parallel_tool_calls)（call_skill 在 runtime 跳锁 → 真并发）
    - serial 段：强制 Semaphore(1)（即便全局 cap 高也串行）
    - ``step_idx``：顶层 step 序号，拼进 call_id 保证 thread 内全局唯一（同一 skill 跨段
      多次出现时，避免 call_id 碰撞 → 否则压缩边界检测会按 call_id 误配 function_call/output）。
    - **重放**（orchestration-suspension-propagation）：派发前按确定性 call_id 查
      history，已配对的子直接复用 output 不重派发——resume 重入时已完成段零派发跳过。
    - **挂起分流**：``DispatchOutcome.suspend`` 非 None 的子只追加悬空 fc（不回填占位
      fco），批内任一挂起 → 抛 ``_BatchSuspend`` 由 run() 既有路径落盘挂起（与 LLM
      路径 ``_dispatch_tools`` 混合批语义同形）。

    返回各 child 输出文本（按发起序），供 when 判定 / upstream 注入 / 末步汇总。

    Raises:
        _BatchSuspend: 批内存在挂起子（收集全部 pending,编排 turn 转 suspended）。
    """
    skill_ids = leaf.skill_ids
    cap = 1 if isinstance(leaf, SerialStep) else max(1, runner.max_parallel_tool_calls)

    # 输出槽按 idx 预置;重放命中的直接填值,其余派发后回填
    outputs: dict[int, str] = {}
    requests: list[ToolCallRequest] = []
    for idx, sid in enumerate(skill_ids):
        call_id = f"orch_{runner.entry_skill.id}_{step_idx}_{sid}_{idx}"
        # 重放:resume 重入时已完成(含 gap 回填)的子零派发复用
        cached = _replay_paired_output(runner, call_id)
        if cached is not None:
            outputs[idx] = cached
            continue
        child_args: dict[str, Any] = {"input": seed}
        if upstream:
            child_args["upstream"] = list(upstream)
        call_args = {
            "skill_id": sid,
            "args": child_args,
            "reason": f"orchestrated:{runner.entry_skill.id}",
        }
        requests.append(
            ToolCallRequest(
                index=idx,
                call_id=call_id,
                name="call_skill",
                arguments=call_args,
                arguments_raw=json.dumps(call_args, ensure_ascii=False),
                # call_skill 在 runtime 内显式跳锁；此处标记不影响真并发
                parallel_safe=False,
            )
        )

    # R3:count 只计实际派发数 —— 全命中重放时 count=0,重放命中率可观测
    await emit(ToolBatchDispatched(data={"count": len(requests), "max_parallel": cap}))
    semaphore = asyncio.Semaphore(cap)

    def _ctx_for(call_id: str) -> ToolContext:
        # 编排 turn 无 LLM 迭代，iteration 固定 1（call_skill 的 turn_index 兜底用）
        return build_ctx(call_id, 1)

    outcomes = await dispatch_batch(
        requests,
        runtime=runner.tool_runtime,
        ctx_for=_ctx_for,
        hooks=runner.hooks,
        emit=emit,
        semaphore=semaphore,
        thread_id=runner.thread_id,
        submission_id=runner.submission_id,
        entry_skill_id=runner.entry_skill.id,
    )

    # 历史按发起序回填（R5 resume；与 A 阶段 3 一致）。编排 turn 无 assistant_message
    # （不采样 LLM）。完成子 (fc, fco) 成对追加;挂起子只追加悬空 fc —— 占位文本
    # "<suspended>" 不得入史(原 silent fallback 根因),pending 收集后整批上抛。
    suspended_pending: list[Any] = []
    for req, outcome in zip(requests, outcomes, strict=True):
        fc_item = function_call(
            call_id=req.call_id, name=req.name,
            arguments=req.arguments_raw, thread_id=runner.thread_id,
        )
        runner.history_buffer.append(fc_item)
        await runner.store.append(fc_item)
        if outcome.suspend is not None:
            # 挂起子:留无 output 的 fc,resume 时由 SuspensionRecord 重导 gap 回填
            suspended_pending.append(outcome.suspend)
            continue
        fco_item = function_call_output(
            call_id=req.call_id, output=outcome.result.output,
            thread_id=runner.thread_id, is_error=outcome.result.is_error,
        )
        runner.history_buffer.append(fco_item)
        await runner.store.append(fco_item)
        outputs[req.index] = outcome.result.output

    if suspended_pending:
        # 整批挂起 pending 上抛 → run() 既有 except _BatchSuspend 落盘挂起,
        # 编排 turn 以 suspended 终结(延迟 import 避免与 turn.py 的环)
        from taifeng.loop.turn import _BatchSuspend

        raise _BatchSuspend(tuple(suspended_pending))

    return tuple(outputs[i] for i in range(len(skill_ids)))
