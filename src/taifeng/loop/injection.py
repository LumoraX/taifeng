"""在飞 turn 的注入（InjectUserInput / InjectSystemMessage）事件构造。

ADR 0029：根 turn 在飞期间 root history 只有 runner 一个写者，两种注入都走
``_PendingTurn.pending_input`` 队列，由 runner 在迭代边界并入；turn 退出后残留由
engine 收尾落史。两处都要发同形事件，构造逻辑集中在这里。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.loop.event import SystemMessageInjected, UserInputInjected

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.event import _Msg

# 注入事件里文本预览的截断长度
_PREVIEW_LEN = 80


def injection_event(
    item: ResponseItem,
    submission_id: str,
    *,
    delivered: bool,
    reason: str | None = None,
) -> _Msg:
    """按注入项的 kind 构造对应事件。

    Args:
        item: ``user_message``（InjectUserInput）或 ``system_injection``（InjectSystemMessage）
        submission_id: 目标 turn 的 submission id
        delivered: True = 已并入 turn；False = turn 结束后由 engine 收尾落史
        reason: delivered=False 时的原因（``"turn_ended"``），True 时为 None

    Raises:
        ValueError: item.kind 不是可注入的两种之一（接线错误，不静默）
    """
    data = {
        "submission_id": submission_id,
        "delivered": delivered,
        "text_preview": str(item.payload.get("text", ""))[:_PREVIEW_LEN],
        "reason": reason,
    }
    if item.kind == "user_message":
        return UserInputInjected(data=data)
    if item.kind == "system_injection":
        return SystemMessageInjected(data=data)
    raise ValueError(f"non-injectable item kind: {item.kind!r}")
