"""tool_batch —— 一批 tool call 的并发派发器。

从 ``TurnRunner._sample_once`` 抽出,职责单一:对一批【已解析】的
``ToolCallRequest`` 并发执行(``asyncio.gather`` + 调用方给的 ``Semaphore`` 限流),
RwLock 安全由 ``ToolCallRuntime.dispatch`` 内部兜底。结果按 ``request.index`` 升序
返回,供调用方按发起序配对回填历史。

参照:codex codex-rs/core/src/tools/parallel.rs(并发边界)。差异:taifeng 的
RwLock 在 runtime 层(读类重叠 / 写类独占),本模块只负责「同时发起 + 收敛排序」,
不重复加锁;``call_skill`` 在 runtime 内显式跳锁 → 真并行子 turn。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng.context.truncate import truncate_middle
from taifeng.loop.event import ToolCallCompleted
from taifeng.suspend.signal import SuspendSignal  # 运行时 except 捕获,不可放 TYPE_CHECKING
from taifeng.tool.spec import ToolContext, ToolResult

if TYPE_CHECKING:
    # 仅注解用 → 放 TYPE_CHECKING 块(ruff TC003;from __future__ annotations 下运行时不需要)
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True)
class ToolCallRequest:
    """一条已解析的 tool call 请求。

    字段:
        index: 发起序(用于结果回填排序;阶段 3 按它配对追加历史)
        call_id: provider 给的工具调用 id
        name: 工具名
        arguments: 已 json 解析的参数 dict
        arguments_raw: 原始参数字符串(写 function_call item 用,保持与 LLM 原样一致)
        parallel_safe: 该工具是否 parallel_safe(供 PreToolUseHook 透传)
        extra_content: provider 专属 tool_call 扩展字段，派发不解释，仅供落史回放
        arguments_error: ``parse_tool_arguments`` 报告的参数解析错误(非法 JSON / 非
            对象);非 None 时派发层以 ``invalid_arguments`` 拒绝执行,不调 handler。
            解析与裁决分离:``ToolCallStarted`` 仍带原始串,历史仍落原始 fc。
    """

    index: int
    call_id: str
    name: str
    arguments: dict[str, Any]
    arguments_raw: str
    parallel_safe: bool
    extra_content: dict[str, Any] | None = None
    arguments_error: str | None = None


def parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """把 LLM 产出的 tool call ``arguments`` 原始串解析为参数对象 —— 全仓唯一入口。

    规则(tool-whitelist 契约「参数非法 JSON 对象拒绝执行」):
    - 空串 / 全空白 → ``({}, None)``(无参工具常见,视为合法空对象);
    - 非法 JSON → ``({}, "invalid_json: <错误>")``;
    - 合法 JSON 但不是对象(list / str / number / null)→ ``({}, "not_an_object: <类型>")``。

    返回 ``(arguments, error)``;``error`` 非 None 时调用方 MUST NOT 执行 handler,
    而以 ``invalid_arguments`` error 结果核销该 call(不退化为 ``{}`` 静默执行)。
    turn 主派发、rewind retry_tool 补跑、resumed tool 两条路径共用本函数。
    """
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"invalid_json: {exc.msg} (pos {exc.pos})"
    if not isinstance(parsed, dict):
        return {}, f"not_an_object: got {type(parsed).__name__}"
    return parsed, None


@dataclass(frozen=True)
class ToolCallOutcome:
    """单条 tool call 的执行结果。

    suspend 非 None 时表示该 tool call 命中挂起点(此时 result 为占位,
    调用方据 suspend 改走挂起落盘路径,不回填 function_call_output)。
    """

    index: int
    call_id: str
    name: str
    result: ToolResult
    duration_ms: int
    suspend: Any = None  # PendingRequest | None;Any 避免顶层 import suspend 包到本模块注解层


async def dispatch_batch(
    requests: list[ToolCallRequest],
    *,
    runtime: Any,  # ToolCallRuntime;用 Any 避免顶层循环 import 负担
    ctx_for: Callable[[str], ToolContext],  # call_id → ToolContext(调用方建好,含 cancel.child)
    hooks: Any,  # HookRunner | None
    emit: Callable[[Any], Awaitable[None]],  # 已绑定 submission_id 的 emit 闭包(收 _Msg)
    semaphore: asyncio.Semaphore,
    thread_id: str,
    submission_id: str,
    entry_skill_id: str,
    visible_tools: frozenset[str],
) -> list[ToolCallOutcome]:
    """并发执行一批 tool call,返回按 ``index`` 升序的结果列表。

    - 限流:每条 ``_dispatch_one`` 进出 ``semaphore``;容量=1 时退化为串行。
    - 安全:RwLock 在 ``runtime.dispatch`` 内;本层不加锁。
    - 异常:``_dispatch_one`` 不抛(异常已被 runtime/hook 吞成 ``ToolResult.error``)。
    - ``thread_id`` / ``submission_id`` / ``entry_skill_id``:构造 HookContext 用。
    - ``visible_tools``:本轮**实际注入请求**的工具名集(与请求严格同源,tool-whitelist
      契约)——LLM 调用集合外的工具在 hook 之前被拒,以 is_error 输出核销。
    """

    async def _run(req: ToolCallRequest) -> ToolCallOutcome:
        # semaphore 限流:容量耗尽时本协程在此挂起排队
        async with semaphore:
            return await _dispatch_one(
                req, runtime=runtime, ctx_for=ctx_for, hooks=hooks, emit=emit,
                thread_id=thread_id, submission_id=submission_id,
                entry_skill_id=entry_skill_id, visible_tools=visible_tools,
            )

    outcomes = await asyncio.gather(*(_run(r) for r in requests))
    # gather 保留输入顺序;为稳妥再按 index 排一次(防御调用方乱序传入)
    return sorted(outcomes, key=lambda o: o.index)


async def _dispatch_one(
    req: ToolCallRequest,
    *,
    runtime: Any,
    ctx_for: Callable[[str], ToolContext],
    hooks: Any,
    emit: Callable[[Any], Awaitable[None]],
    thread_id: str,
    submission_id: str,
    entry_skill_id: str,
    visible_tools: frozenset[str],
) -> ToolCallOutcome:
    """执行单条:可执行校验 → PreToolUse hook → dispatch → PostToolUse hook → emit。

    若执行链抛 SuspendSignal(如 SuspendingPrompter / request_user_input 触发),
    捕获为带 suspend=pending 的 outcome,不让其冒泡打断整批(挂起不是错误)。
    """
    ctx = ctx_for(req.call_id)
    start = time.monotonic()

    # tool-whitelist 可执行校验(hook 之前):LLM 幻觉调用本轮未提供的工具 →
    # is_error 输出核销 call_id(LLM 可见错误自行恢复,turn 不中断),
    # 不消耗 hook / 权限 / 锁资源;registry 有但本轮没提供的同样拒(可见才可执行)
    if req.name not in visible_tools:
        return await _reject_before_dispatch(
            req, f"tool_not_offered: {req.name}", reason="not_offered",
            start=start, emit=emit,
        )
    # 参数校验(同样在 hook 之前,次于 not_offered——工具不存在是更根本的错):
    # 坏 JSON / 非对象参数不执行 handler,以 is_error 输出核销 call_id 让模型改参
    # 重试;不退化为 {} 静默执行(禁止 silent fallback)
    if req.arguments_error is not None:
        return await _reject_before_dispatch(
            req, f"invalid_arguments: {req.arguments_error}",
            reason="invalid_arguments", start=start, emit=emit,
        )

    try:
        return await _dispatch_one_inner(
            req, ctx=ctx, start=start, runtime=runtime, hooks=hooks, emit=emit,
            thread_id=thread_id, submission_id=submission_id,
            entry_skill_id=entry_skill_id,
        )
    except SuspendSignal as sig:
        # 挂起:不 emit ToolCallCompleted(turn 侧据 suspend 落 suspension);返回占位 result
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolCallOutcome(
            index=req.index, call_id=req.call_id, name=req.name,
            result=ToolResult(output="<suspended>", is_error=False),
            duration_ms=duration_ms, suspend=sig.pending,
        )


async def _reject_before_dispatch(
    req: ToolCallRequest,
    message: str,
    *,
    reason: str,
    start: float,
    emit: Callable[[Any], Awaitable[None]],
) -> ToolCallOutcome:
    """派发前拒绝的统一形态:is_error 结果核销 call_id + emit ToolCallCompleted。

    not_offered / invalid_arguments 同构——不进 hook、不问权限、不取锁,turn 不中断。
    """
    result = ToolResult.error(message, reason=reason)
    duration_ms = int((time.monotonic() - start) * 1000)
    await emit(ToolCallCompleted(data={
        "call_id": req.call_id, "name": req.name,
        "output": result.output, "is_error": True,
        "duration_ms": duration_ms,
    }))
    return ToolCallOutcome(
        index=req.index, call_id=req.call_id, name=req.name,
        result=result, duration_ms=duration_ms,
    )


async def _dispatch_one_inner(
    req: ToolCallRequest,
    *,
    ctx: ToolContext,
    start: float,
    runtime: Any,
    hooks: Any,
    emit: Callable[[Any], Awaitable[None]],
    thread_id: str,
    submission_id: str,
    entry_skill_id: str,
) -> ToolCallOutcome:
    """``_dispatch_one`` 的成功路径主体(从原函数体平移而来)。

    单列为内层函数,使 ``_dispatch_one`` 仅承担 SuspendSignal try/except 边界,
    保持各函数 ≤ 80 行且圈复杂度受控。
    """
    # G5a：PreToolUse hook 可改写 args（与 script hook 的 args_override 对齐）。
    # effective_args 默认 = LLM 原始 args；hook 放行且给出合法 dict override 时替换。
    # 注：直接遍历 registry handlers —— HookRunner.run 在全部 allow 时返回新的
    # HookDecision.ok() 会丢失 metadata['args_override']（与 run_script 同处理）。
    effective_args = req.arguments
    denied = None  # HookDecision | None

    if hooks is not None:
        from taifeng.hooks.types import HookContext, HookDecision, PreToolUseHook

        hook_ctx = HookContext(
            thread_id=thread_id, submission_id=submission_id,
            entry_skill_id=entry_skill_id,
        )
        for handler in hooks.registry.handlers("pre_tool_use"):
            try:
                decision = await handler(
                    PreToolUseHook(
                        tool_name=req.name, arguments=effective_args,
                        parallel_safe=req.parallel_safe, call_id=req.call_id,
                    ),
                    hook_ctx,
                )
            except SuspendSignal:
                # 挂起信号不是 hook 错误,放行给外层 _dispatch_one 捕获落挂起
                raise
            except Exception as e:  # noqa: BLE001 —— hook 异常按 deny 处理
                denied = HookDecision.deny(f"hook_error: {e}")
                break
            if not decision.allow:
                denied = decision
                break
            override = decision.metadata.get("args_override")
            if isinstance(override, dict):
                effective_args = override  # 链式：后续 handler 看到改写后的 args

    if denied is not None:
        result = ToolResult.error(
            f"hook_denied: {denied.reason}", reason="hook_denied",
            hook_metadata=denied.metadata,
        )
    else:
        result = await runtime.dispatch(
            name=req.name, arguments=effective_args, ctx=ctx
        )

    duration_ms = int((time.monotonic() - start) * 1000)

    # === PostToolUse hook ===
    if hooks is not None:
        from taifeng.hooks.types import HookContext, PostToolUseHook

        await hooks.run(
            "post_tool_use",
            PostToolUseHook(
                tool_name=req.name, arguments=effective_args,
                output=result.output, is_error=result.is_error,
                duration_ms=duration_ms, call_id=req.call_id,
            ),
            HookContext(
                thread_id=thread_id, submission_id=submission_id,
                entry_skill_id=entry_skill_id,
            ),
        )

    # 完成即 emit(并发下按真实完成序交错,反映真实并行;输出截断给事件流)
    await emit(
        ToolCallCompleted(
            data={
                "call_id": req.call_id, "name": req.name,
                # G6b：中段截断，错误信息常在尾部，朴素 [:500] 会丢失
                "output": truncate_middle(result.output, 500),
                "is_error": result.is_error,
                "duration_ms": duration_ms,
            }
        )
    )
    return ToolCallOutcome(
        index=req.index, call_id=req.call_id, name=req.name,
        result=result, duration_ms=duration_ms,
    )
