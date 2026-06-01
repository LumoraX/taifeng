"""call_skill 工具 —— 派发子 skill 执行。

ToolContext.extras 必须含（由 TurnRunner 注入）：
    - ``skill_snapshot``: SkillSnapshot
    - ``dispatch_policy``: DispatchPolicy
    - ``call_stack``: CallStack（当前栈，本工具会 push 后传给子 dispatcher）
    - ``dispatcher``: SkillDispatcher（执行子 skill 的回调）
    - ``current_skill``: SkillDefinition（caller）

ToolContext.extras 可选（permission-gate-completeness 引入）：
    - ``permission_policy``: PermissionPolicy | None —— None 时跳过 step 3（向后兼容）
    - ``hook_runner``: HookRunner | None —— None 时跳过 pre/post_skill_dispatch hook
    - ``request_metadata``: dict —— 业务侧不透明上下文，合并进 PermissionRequest.metadata
    - ``submission_id`` / ``entry_skill_id`` / ``turn_index``: 上下文透传到 PermissionRequest

5 阶段流程（spec ``skill-dispatch``）：
    1. DispatchPolicy.check（白名单 / 深度 / 环 / unknown_skill）—— 失败立即返回
    2. pre_skill_dispatch hook —— 任一 deny 返回 error + emit ``skill_dispatch_hook_denied``
    3. PermissionPolicy.check —— deny 返回 error + emit ``skill_dispatch_permission_denied``
    4. push call_stack + 构造子 ToolContext + dispatcher.run_sub_skill
    5. post_skill_dispatch hook —— 仅审计（不影响 ToolResult；hook 异常也吞掉）
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.hooks.types import (
    SKILL_OUTPUT_PREVIEW_LIMIT,
    HookContext,
    PostSkillDispatchHook,
    PreSkillDispatchHook,
)
from taifeng.permission.types import PermissionRequest
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.dispatch import CallStack, DispatchPolicy
    from taifeng.skill.registry import SkillSnapshot


class SkillDispatcher(Protocol):
    """实际执行子 skill 的回调 —— 由 Engine 注入。"""

    async def run_sub_skill(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
    ) -> ToolResult:
        ...


async def _emit_event(ctx: ToolContext, kind: str, data: dict[str, Any]) -> None:
    """通过 dispatcher (TurnRunner) emit 一条 EventMsg；缺失时 noop。

    避免 call_skill 直接依赖 loop/event —— 通过 dispatcher 的 _emit 路径转发。
    注意：``TurnRunner._emit`` 自身会用 ``EventMsg(submission_id, msg=msg)``
    包装，这里只构造 msg 实例传入；**禁止**预先包 EventMsg 否则双重 wrap
    会触发 pydantic discriminator 错（"Unable to extract tag using 'kind'"）。
    """
    dispatcher = ctx.extras.get("dispatcher")
    if dispatcher is None:
        return
    # 延迟 import 防止循环依赖
    from taifeng.loop.event import (
        SkillDispatchHookDenied,
        SkillDispatchPermissionDenied,
    )
    msg_cls_map: dict[str, type] = {
        "skill_dispatch_hook_denied": SkillDispatchHookDenied,
        "skill_dispatch_permission_denied": SkillDispatchPermissionDenied,
    }
    cls = msg_cls_map.get(kind)
    if cls is None:
        return
    msg = cls(data=data)
    emit = getattr(dispatcher, "_emit", None)
    if emit is None:
        return
    try:
        # dispatcher._emit(msg) 而非 _emit(EventMsg(...)) ——
        # TurnRunner._emit 自身负责包 EventMsg
        await emit(msg)
    except Exception:
        # emit 失败不影响主流程
        import logging
        logging.getLogger(__name__).exception("emit %s failed", kind)


async def _call_skill_handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """call_skill handler —— 5 阶段派发。"""
    # ---- 参数校验 ----
    skill_id = args.get("skill_id")
    if not skill_id or not isinstance(skill_id, str):
        return ToolResult.error(
            "missing_or_invalid_argument: skill_id", reason="bad_args"
        )
    sub_args = args.get("args") or {}
    if not isinstance(sub_args, dict):
        return ToolResult.error(
            "invalid_argument: args must be object", reason="bad_args"
        )
    # call-skill-reason-field: LLM 可选附带 reason；类型必须 str
    # 透传到 PermissionRequest.reason + skill_dispatched.data["reason"]
    raw_reason = args.get("reason", "")
    if not isinstance(raw_reason, str):
        return ToolResult.error(
            "invalid_argument: reason must be string", reason="bad_args"
        )
    dispatch_reason: str = raw_reason

    # ---- 从 ctx.extras 取必需依赖 ----
    snapshot: SkillSnapshot | None = ctx.extras.get("skill_snapshot")
    policy: DispatchPolicy | None = ctx.extras.get("dispatch_policy")
    stack: CallStack | None = ctx.extras.get("call_stack")
    dispatcher: SkillDispatcher | None = ctx.extras.get("dispatcher")
    caller: SkillDefinition | None = ctx.extras.get("current_skill")

    if not all([snapshot, policy, stack is not None, dispatcher, caller]):
        return ToolResult.error(
            "call_skill misconfigured: missing "
            "snapshot/policy/stack/dispatcher/current_skill",
            reason="config_error",
        )
    # 静态类型断言
    assert snapshot is not None and policy is not None and stack is not None
    assert dispatcher is not None and caller is not None

    # 可选依赖（向后兼容：未注入则跳过对应阶段）
    permission_policy = ctx.extras.get("permission_policy")
    hook_runner = ctx.extras.get("hook_runner")
    request_metadata = ctx.extras.get("request_metadata") or {}
    submission_id = ctx.extras.get("submission_id") or ""
    entry_skill_id = ctx.extras.get("entry_skill_id") or ""
    turn_index = int(ctx.extras.get("turn_index") or 0)

    # 构造上下文常量（多个阶段复用）
    call_chain = tuple(stack.path())
    caller_skill_id = stack.current.skill_id if stack.current else caller.id
    hook_ctx = HookContext(
        thread_id=ctx.thread_id,
        submission_id=submission_id,
        entry_skill_id=entry_skill_id,
        extras={
            "request_metadata": request_metadata,
            "call_chain": list(call_chain),
            "turn_index": turn_index,
        },
    )

    # ============================================================
    # 阶段 1：DispatchPolicy.check —— 结构性保证（白名单/深度/环）
    # ============================================================
    target = snapshot.get(skill_id)
    verdict = policy.check(stack=stack, caller=caller, target=target)
    if not verdict.allowed:
        return ToolResult.error(
            f"dispatch_rejected: {verdict.reason} "
            f"(path: {' → '.join(verdict.path)})",
            reason=verdict.reason or "rejected",
            path=list(verdict.path),
        )
    assert target is not None

    # ============================================================
    # 阶段 2：pre_skill_dispatch hook —— 业务侧动态拦截
    # ============================================================
    if hook_runner is not None:
        pre_hook = PreSkillDispatchHook(
            target_skill_id=target.id,
            args=sub_args,
            caller_skill_id=caller_skill_id,
            call_chain=call_chain,
            depth=stack.depth + 1,
        )
        pre_decision = await hook_runner.run(
            "pre_skill_dispatch", pre_hook, hook_ctx,
        )
        if not pre_decision.allow:
            await _emit_event(
                ctx,
                "skill_dispatch_hook_denied",
                {
                    "target_skill_id": target.id,
                    "caller_skill_id": caller_skill_id,
                    "hook_reason": pre_decision.reason or "",
                    "call_chain": list(call_chain),
                },
            )
            return ToolResult.error(
                f"skill_dispatch_hook_denied: {pre_decision.reason}",
                reason="hook_denied",
                target_skill_id=target.id,
                hook_metadata=pre_decision.metadata,
            )

    # ============================================================
    # 阶段 3：PermissionPolicy.check —— 业务策略动态拦截
    # ============================================================
    if permission_policy is not None:
        perm_request = PermissionRequest.for_skill_dispatch(
            target.id,
            caller_skill_id=caller_skill_id,
            call_chain=call_chain,
            thread_id=ctx.thread_id,
            submission_id=submission_id,
            entry_skill_id=entry_skill_id,
            turn_index=turn_index,
            # 透传当前 tool call 的 call_id → SuspendingPrompter 据此填 related_call_id,
            # 使挂起的 PendingRequest 能与发起的 function_call 配对(history-gap 续跑依据)
            extra_metadata={"call_id": ctx.call_id, **request_metadata},
            reason=dispatch_reason,
        )
        perm_decision = await permission_policy.check(perm_request)
        if not perm_decision.granted:
            await _emit_event(
                ctx,
                "skill_dispatch_permission_denied",
                {
                    "target_skill_id": target.id,
                    "caller_skill_id": caller_skill_id,
                    "reason": perm_decision.reason,
                    # call-skill-reason-field: LLM 自陈与 policy decision 分开
                    # data["reason"] = policy decision.reason（既有语义）
                    # data["request_reason"] = LLM 提供的 PermissionRequest.reason
                    "request_reason": dispatch_reason,
                    "call_chain": list(call_chain),
                },
            )
            return ToolResult.error(
                f"skill_dispatch_denied: {perm_decision.reason}",
                reason="permission_denied",
                target_skill_id=target.id,
            )

    # ============================================================
    # 阶段 4：派发到子 TurnRunner
    # ============================================================
    sub_call_id = f"sk_{secrets.token_hex(4)}"
    new_stack = stack.push(skill_id=target.id, call_id=sub_call_id)
    # 计算 visible_skills —— 子 skill 可达图
    if target.entry:
        visible = snapshot.reachable_from(target.id)
    elif stack.frames:
        visible = snapshot.reachable_from(stack.frames[0].skill_id)
    else:
        visible = snapshot.ids()

    sub_ctx = ToolContext(
        call_id=sub_call_id,
        cancel=ctx.cancel.child(f"call_skill:{target.id}"),
        thread_id=ctx.thread_id,
        extras={
            **ctx.extras,
            "call_stack": new_stack,
            "current_skill": target,
            "visible_skills": visible,
            # call-skill-reason-field: 透传给 TurnRunner.run_sub_skill，让
            # SkillDispatched.data["reason"] 也能携带 LLM 自陈意图
            "dispatch_reason": dispatch_reason,
        },
    )

    sub_start = time.monotonic()
    result = await dispatcher.run_sub_skill(
        target=target,
        arguments=sub_args,
        parent_stack=new_stack,
        ctx=sub_ctx,
    )
    duration_ms = int((time.monotonic() - sub_start) * 1000)

    # ============================================================
    # 阶段 5：post_skill_dispatch hook —— 仅审计
    # ============================================================
    if hook_runner is not None:
        # 截断 output preview 到 SKILL_OUTPUT_PREVIEW_LIMIT 字节
        preview = result.output or ""
        if len(preview.encode("utf-8")) > SKILL_OUTPUT_PREVIEW_LIMIT:
            # 简单按字节截断（中文不在精确边界也能保持有效字符串）
            encoded = preview.encode("utf-8")[:SKILL_OUTPUT_PREVIEW_LIMIT]
            preview = encoded.decode("utf-8", errors="ignore")
        post_hook = PostSkillDispatchHook(
            target_skill_id=target.id,
            caller_skill_id=caller_skill_id,
            success=not result.is_error,
            duration_ms=duration_ms,
            sub_thread_id=result.data.get("sub_thread_id"),
            output_preview=preview,
        )
        # 用 run_audit_only —— hook 返回 deny / 异常都不影响 ToolResult
        await hook_runner.run_audit_only(
            "post_skill_dispatch", post_hook, hook_ctx,
        )

    # 在 ToolResult.data 中补 duration_ms（供调用方观察）
    if not result.is_error and "duration_ms" not in result.data:
        # 重建 ToolResult 加 duration_ms
        return ToolResult.ok(
            result.output,
            **{**result.data, "duration_ms": duration_ms},
        )
    return result


CALL_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {
            "type": "string",
            "description": (
                "要派发的子 skill id；必须在当前 skill 的 child_skills 白名单内"
            ),
        },
        "args": {
            "type": "object",
            "description": "传给子 skill 的参数（任意结构，由子 skill 自行解释）",
            "additionalProperties": True,
        },
        # call-skill-reason-field (AMENDMENT 2026-05-27): reason 从 optional
        # 改为 required —— 实践表明 LLM 对可选字段的填充率远低于预期，导致
        # HITL 审批方拿不到决策依据。改 required 后 gpt-4o / gemini-2.x 会
        # 严格执行 schema 强制填入。handler 仍保留 args.get 防御性兜底。
        "reason": {
            "type": "string",
            "description": (
                "1-2 句话用第一人称说明*为什么*要派发到这个子 skill。"
                "例：'需要安全审查这段处理用户输入的代码'。"
                "该字段会出现在审批方的 HITL 弹窗里 —— 缺乏有意义的 reason"
                "可能直接导致拒绝。"
            ),
        },
    },
    "required": ["skill_id", "reason"],
    "additionalProperties": False,
}


def make_call_skill_tool() -> ToolSpec:
    return ToolSpec(
        name="call_skill",
        description=(
            "派发到子 skill 执行。子 skill 必须在当前 skill 的 child_skills 白名单内，"
            "且未达递归深度上限，且不在调用栈上（环检测）。\n\n"
            "**调用时务必附带 ``reason`` 字段**（1-2 句话用第一人称说明"
            "*为什么*要派发到这个特定子 skill，例如 '需要 security-scanner "
            "审查这段处理用户输入的 SQL 拼接代码' 而非 '执行 security-scanner'）。"
            "该字段会出现在审批方的 HITL 对话框里 —— 如果你不提供有意义的 reason，"
            "审批方将无法判断你的意图，很可能直接拒绝派发。"
        ),
        input_schema=CALL_SKILL_SCHEMA,
        handler=_call_skill_handler,
        parallel_safe=False,  # 子 LLM 调用，独占
        timeout_seconds=300.0,  # 子 skill 可能耗时
    )
