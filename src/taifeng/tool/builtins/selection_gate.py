"""call_skill / spawn_skill 派发处的选择置信度分流门（相位 3，skill-selection-gate，ADR 0088）。

只约束**经发现选中**的 skill：模型在本轮里经 ``search_skills`` 看到它、且那份结果给了分流
结论。作者白名单里、模型直接选中的子 skill 不经本门。

| 分流 | 放行条件 | 拦下时的结果 |
| --- | --- | --- |
| ``proceed`` | 直接放行 | — |
| ``trial`` | 那次召回之后模型成功 ``read_skill`` 过它；或配置了试用门且放行 | ``selection_needs_trial`` / ``selection_trial_rejected`` |
| ``escalate`` | 不放行 | ``selection_low_confidence`` |

判定依据全部来自 history（``skill/selection.py``），本模块不持有状态。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from taifeng.skill.selection import latest_selection, was_read_after
from taifeng.tool.spec import ToolResult

if TYPE_CHECKING:
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.selection import RecalledSelection, SkillSelectionGate
    from taifeng.tool.spec import ToolContext

logger = logging.getLogger(__name__)

_NEEDS_TRIAL = (
    "selection_needs_trial: skill {skill_id!r} was found with confidence {confidence}; "
    "read its instructions with read_skill first, then call it if it fits"
)
_TRIAL_REJECTED = (
    "selection_trial_rejected: skill {skill_id!r} does not fit this task ({reason}); "
    "search again with different keywords or pick another candidate"
)
_LOW_CONFIDENCE = (
    "selection_low_confidence: skill {skill_id!r} was found with confidence {confidence}, "
    "too low to dispatch. Search again with different keywords; if nothing fits, say so "
    "or ask the user"
)


async def _emit_gated(ctx: ToolContext, data: dict[str, Any]) -> None:
    """经 dispatcher 打分流门裁决事件；无 dispatcher（裸 handler 单测）时不打。"""
    emit = getattr(ctx.extras.get("dispatcher"), "_emit", None)
    if emit is None:
        return
    # 延迟 import 防止 tool → loop 的 import 期循环依赖
    from taifeng.loop.event import SkillSelectionGated

    try:
        await emit(SkillSelectionGated(data=data))
    except Exception:
        # 可观测降级不得改变派发裁决
        logger.exception("emit skill_selection_gated failed")


def _confidence_text(selection: RecalledSelection) -> str:
    """错误文本里的置信度；召回结果没给出时写 unknown。"""
    return "unknown" if selection.confidence is None else f"{selection.confidence:.2f}"


async def check_selection_gate(
    gate: SkillSelectionGate | None,
    target: SkillDefinition,
    ctx: ToolContext,
) -> ToolResult | None:
    """派发前过分流门；放行返回 None，拦下返回给模型的错误结果。

    Args:
        gate: 注入的分流门；None = 未启用相位 3，恒放行。
        target: 已通过 ``DispatchPolicy`` 的派发目标。
        ctx: 工具上下文（取 dispatcher 的 history 与当前任务）。
    """
    dispatcher = ctx.extras.get("dispatcher")
    history = getattr(dispatcher, "history_buffer", None)
    if gate is None or history is None:
        return None
    selection = latest_selection(history, target.id)
    if selection is None:
        return None
    admitted, basis, reason = await _decide(gate, target, selection, history, ctx)
    await _emit_gated(ctx, {
        "skill_id": target.id,
        "call_id": ctx.call_id,
        "route": selection.route,
        "confidence": selection.confidence,
        "admitted": admitted,
        "basis": basis,
    })
    if admitted:
        return None
    templates = {
        "needs_trial": _NEEDS_TRIAL,
        "trial_rejected": _TRIAL_REJECTED,
        "low_confidence": _LOW_CONFIDENCE,
    }
    return ToolResult.error(
        templates[basis].format(
            skill_id=target.id, confidence=_confidence_text(selection), reason=reason
        ),
        reason=f"selection_{basis}",
        skill_id=target.id,
        route=selection.route,
        confidence=selection.confidence,
    )


async def check_selection_gate_by_id(
    gate: SkillSelectionGate | None,
    skill_id: str,
    ctx: ToolContext,
) -> ToolResult | None:
    """按 skill id 过分流门（detached 派发用）。

    snapshot 里没有该 skill 时返回 None：交给后续的准入检查按「未知 skill」拒绝。
    """
    snapshot = ctx.extras.get("skill_snapshot")
    if gate is None or snapshot is None:
        return None
    target = snapshot.get(skill_id)
    if target is None:
        return None
    return await check_selection_gate(gate, target, ctx)


async def _decide(
    gate: SkillSelectionGate,
    target: SkillDefinition,
    selection: RecalledSelection,
    history: list[Any],
    ctx: ToolContext,
) -> tuple[bool, str, str]:
    """返回 (是否放行, 依据, 说明)。"""
    if selection.route == "proceed":
        return True, "route_proceed", ""
    if selection.route == "escalate":
        return False, "low_confidence", ""
    if was_read_after(history, target.id, selection.search_index):
        return True, "read_skill", ""
    if gate.trial_judge is None:
        return False, "needs_trial", ""
    verdict = await gate.trial_judge.judge(
        task=str(ctx.extras.get("current_task") or ""),
        skill_id=target.id,
        description=target.description,
        body=target.body,
        cancel=ctx.cancel,
    )
    if verdict.approved:
        return True, "trial_judge", verdict.reason
    return False, "trial_rejected", verdict.reason


__all__ = ["check_selection_gate", "check_selection_gate_by_id"]
