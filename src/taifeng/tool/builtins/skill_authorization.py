"""call_skill 派发处的白名单外授权（相位 4，skill-authorization，ADR 0089）。

``DispatchPolicy.check`` 因 ``not_in_whitelist`` 拒绝、且注入了 ``SkillAuthorizationPolicy`` 时
进入本模块：目标在可发现范围内则请求授权；放行后由调用方带着
``authorized_outside_whitelist=True`` 重新过结构性裁决，其余的门照常执行。

不在可发现范围内的目标得到与未启用相位 4 时逐字相同的 ``not_in_whitelist`` 拒绝。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from taifeng.skill.authorization import (
    SkillAuthorizationRequest,
    is_discoverable_outside,
)
from taifeng.tool.spec import ToolResult

if TYPE_CHECKING:
    from taifeng.skill.authorization import SkillAuthorizationPolicy
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.dispatch import CallStack
    from taifeng.skill.registry import SkillSnapshot
    from taifeng.tool.spec import ToolContext

logger = logging.getLogger(__name__)


async def _emit(ctx: ToolContext, granted: bool, data: dict[str, Any]) -> None:
    """经 dispatcher 打授权裁决事件；无 dispatcher（裸 handler 单测）时不打。"""
    emit = getattr(ctx.extras.get("dispatcher"), "_emit", None)
    if emit is None:
        return
    # 延迟 import 防止 tool → loop 的 import 期循环依赖
    from taifeng.loop.event import SkillAuthorizationDenied, SkillAuthorizationGranted

    message = SkillAuthorizationGranted if granted else SkillAuthorizationDenied
    try:
        await emit(message(data=data))
    except Exception:
        # 可观测降级不得改变授权裁决
        logger.exception("emit skill authorization event failed")


def _not_in_whitelist(caller: SkillDefinition, target: SkillDefinition) -> ToolResult:
    """与 ``call_skill`` 对结构性拒绝的表述一致。"""
    path = [caller.id, target.id]
    return ToolResult.error(
        f"dispatch_rejected: not_in_whitelist (path: {' → '.join(path)})",
        reason="not_in_whitelist",
        path=path,
    )


async def authorize_outside_whitelist(
    authorization: SkillAuthorizationPolicy,
    *,
    caller: SkillDefinition,
    target: SkillDefinition,
    snapshot: SkillSnapshot,
    stack: CallStack,
    dispatch_reason: str,
    ctx: ToolContext,
) -> ToolResult | None:
    """为一次白名单外派发请求授权；放行返回 None，否则返回给模型的拒绝结果。

    Raises:
        SuspendSignal: 授权走人工审批且以挂起方式进行时原样上抛。
    """
    if not is_discoverable_outside(
        caller, target.id, snapshot, authorization, ctx.extras.get("capabilities"),
        on_stack=stack.path(),
    ):
        return _not_in_whitelist(caller, target)
    call_chain = tuple(stack.path())
    request = SkillAuthorizationRequest(
        caller_skill_id=caller.id,
        target_skill_id=target.id,
        target_description=target.description,
        target_source=str(target.source),
        origin="call_skill",
        reason=dispatch_reason,
        call_chain=call_chain,
        call_id=ctx.call_id,
        thread_id=ctx.thread_id,
        submission_id=str(ctx.extras.get("submission_id") or ""),
        entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
        turn_index=int(ctx.extras.get("turn_index") or 0),
        metadata=dict(ctx.extras.get("request_metadata") or {}),
        permission_policy=ctx.extras.get("permission_policy"),
    )
    decision = await authorization.authorize(request, cancel=ctx.cancel)
    await _emit(ctx, decision.granted, {
        "caller_skill_id": caller.id,
        "target_skill_id": target.id,
        "call_id": ctx.call_id,
        "origin": "call_skill",
        "reason": decision.reason,
        "request_reason": dispatch_reason,
        "call_chain": list(call_chain),
    })
    if decision.granted:
        return None
    return ToolResult.error(
        f"skill_authorization_denied: {decision.reason}",
        reason="authorization_denied",
        target_skill_id=target.id,
    )


__all__ = ["authorize_outside_whitelist"]
