"""审计 Session 此刻在等人作答的挂起（ADR 0097）。

释放一个 Session 时要分清两件事：它是结束了，还是只是停在那里等人。后者不能写
``session_ended``——写了就再也接管不了。这里按 Session 记下「已经进了 Journal、
还没有结清」的挂起，供释放时判断。

登记的时机只有三处：挂起落账之后、结清落账之后、接管时从 Journal 重建。没有进
Journal 的挂起不登记：Journal 里没有它，接管之后也就无从继续。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from taifeng.loop.audit_bootstrap import AuditedSessionState

# coordinator → 未结清的挂起 id（按落账顺序）；随 Session 一起回收
_AWAITING: WeakKeyDictionary[object, list[str]] = WeakKeyDictionary()


def mark_awaiting(state: AuditedSessionState, suspension_id: str) -> None:
    """登记一次已落账的挂起。"""
    waiting = _AWAITING.setdefault(state.coordinator, [])
    if suspension_id not in waiting:
        waiting.append(suspension_id)


def clear_awaiting(state: AuditedSessionState, suspension_id: str) -> None:
    """一次挂起已结清。"""
    waiting = _AWAITING.get(state.coordinator)
    if waiting is not None and suspension_id in waiting:
        waiting.remove(suspension_id)


def awaiting_suspensions(state: AuditedSessionState) -> tuple[str, ...]:
    """此刻仍在等人作答的挂起 id，按落账顺序。"""
    return tuple(_AWAITING.get(state.coordinator, ()))
