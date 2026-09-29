"""工作集变更 → 事件（skill-working-set，ADR 0090）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.loop.event import SkillEvicted, SkillPromoted, SkillQuarantined, SkillReleased

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from taifeng.skill.working_set_runtime import WorkingSetChange

_MESSAGES: dict[str, Any] = {
    "promoted": SkillPromoted,
    "evicted": SkillEvicted,
    "quarantined": SkillQuarantined,
    "released": SkillReleased,
}


async def emit_working_set_changes(
    emit: Callable[[Any], Awaitable[None]],
    changes: Sequence[WorkingSetChange],
) -> None:
    """把生效的变更逐条打成事件（R3）。

    Args:
        emit: TurnRunner 的事件出口（收 msg 实例）。
        changes: ``SkillWorkingSet.observe`` / ``restore`` 返回的变更。
    """
    for change in changes:
        await emit(_MESSAGES[change.kind](data=change.as_payload()))


__all__ = ["emit_working_set_changes"]
