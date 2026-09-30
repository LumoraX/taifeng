"""审批通过后重跑工具时的 ``ToolContext``（根 thread 与子 thread 共用）。

``Resume`` 批准的调用在 engine 层直接重跑，没有 ``TurnRunner``，上下文只能给出不依赖 runner 的那
部分：skill 快照、权限策略、脚本执行器，以及这次执行的归属——发起重跑的那一轮提交（``Resume``
本身的 submission id）与所属会话（ADR 0109）。依赖 runner 的键（``dispatcher``、``call_stack``、
``spawn_coordinator`` 等）不在其中；需要它们的派发类工具由续跑的 turn 自己重跑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine
    from taifeng.skill.definition import SkillDefinition


def resumed_tool_context(
    engine: AgentEngine,
    *,
    call_id: str,
    thread_id: str,
    entry: SkillDefinition,
    cancel: CancellationToken,
    submission_id: str | None,
) -> ToolContext:
    """为一次重跑构造工具上下文。

    Args:
        entry: 调用所在 thread 的 entry skill（子 thread 上是子 skill）。
        submission_id: 触发这次重跑的 ``Resume`` 的 submission id；内核自己发起的重跑
            （到期裁决等）没有对应提交时为 None。
    """
    extras: dict[str, Any] = {
        "skill_snapshot": engine._snapshot,  # noqa: SLF001
        "visible_skills": engine._snapshot.reachable_from(entry.id),  # noqa: SLF001
        "dispatch_policy": engine._dispatch_policy,  # noqa: SLF001
        "outcome_judge": engine._outcome_judge,  # noqa: SLF001
        "current_skill": entry,
        "entry_skill_id": entry.id,
        "permission_policy": engine._permission_policy,  # noqa: SLF001
        "hook_runner": engine._hooks,  # noqa: SLF001
        "request_metadata": engine._request_metadata,  # noqa: SLF001
        "turn_index": engine._turn_index,  # noqa: SLF001
        "script_executors": engine._script_executors,  # noqa: SLF001
        "session_id": engine._session_id,  # noqa: SLF001
    }
    if submission_id is not None:
        extras["submission_id"] = submission_id
    return ToolContext(call_id=call_id, cancel=cancel, thread_id=thread_id, extras=extras)


__all__ = ["resumed_tool_context"]
