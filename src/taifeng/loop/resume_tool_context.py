"""审批通过后重跑工具时的 ``ToolContext``（根 thread 与子 thread 共用）。

``Resume`` 批准的调用在 engine 层直接重跑，没有 ``TurnRunner``，上下文只能给出不依赖 runner 的那
部分：skill 快照、权限策略、脚本执行器，以及这次执行的归属——发起重跑的那一轮提交（``Resume``
本身的 submission id）与所属会话（ADR 0109）。依赖 runner 的键（``dispatcher``、``call_stack``、
``spawn_coordinator`` 等）不在其中；需要它们的派发类工具由续跑的 turn 自己重跑。
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from taifeng.conversation.attachment_store import thread_attachment_refs
from taifeng.conversation.models import function_call_output
from taifeng.conversation.origin import INPUT_TAINT_EXTRAS_KEY, tag_origin, tool_output_origin
from taifeng.llm.errors import LLMError
from taifeng.loop.attachment_resolve import externalize_tool_attachments
from taifeng.tool.builtins.file_io import ATTACHMENT_REFS_EXTRAS_KEY
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine
    from taifeng.skill.definition import SkillDefinition
    from taifeng.tool.spec import ToolResult


def resumed_tool_context(
    engine: AgentEngine,
    *,
    call_id: str,
    thread_id: str,
    entry: SkillDefinition,
    cancel: CancellationToken,
    submission_id: str | None,
    items: list[ResponseItem] | None = None,
) -> ToolContext:
    """为一次重跑构造工具上下文。

    Args:
        entry: 调用所在 thread 的 entry skill（子 thread 上是子 skill）。
        submission_id: 触发这次重跑的 ``Resume`` 的 submission id；内核自己发起的重跑
            （到期裁决等）没有对应提交时为 None。
        items: 调用所在 thread 的 history；``file_read`` 读附件引用前据此做归属校验
            （ADR 0148）。不给则视为本 thread 不持有任何附件。
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
        INPUT_TAINT_EXTRAS_KEY: engine._taint_registry.snapshot(thread_id).to_dict(),
        "session_id": engine._session_id,  # noqa: SLF001
        ATTACHMENT_REFS_EXTRAS_KEY: partial(thread_attachment_refs, list(items or [])),
    }
    if submission_id is not None:
        extras["submission_id"] = submission_id
    return ToolContext(call_id=call_id, cancel=cancel, thread_id=thread_id, extras=extras)


async def settle_resumed_result(
    engine: AgentEngine, *, call_id: str, thread_id: str, name: str, result: ToolResult,
) -> ResponseItem:
    """重跑结果 → ``function_call_output``，与 turn 内结算同一套附件处理。

    此前这里只取 ``result.output``，工具返回的图片在审批后重跑时被丢掉；现在与
    ``TurnRunner`` 结算一样过准入、按需写入附件存储（ADR 0148），失败判
    ``tool_attachment_rejected`` 且不破坏调用配对。
    """
    try:
        attachments = await externalize_tool_attachments(
            result.attachments,
            resolver=engine._attachment_resolver,  # noqa: SLF001
            policy=engine._image_input_policy,  # noqa: SLF001
        )
    except LLMError as exc:
        return function_call_output(
            call_id=call_id, output=f"tool_attachment_rejected: {exc}",
            thread_id=thread_id, is_error=True,
        )
    out = function_call_output(
        call_id=call_id, output=result.output, thread_id=thread_id,
        is_error=result.is_error, attachments=attachments,
    )
    spec = engine._tool_runtime.spec_for(name)  # noqa: SLF001
    return tag_origin(out, tool_output_origin(
        result.origin, spec.output_trust if spec is not None else None, name,
    ))


__all__ = ["resumed_tool_context", "settle_resumed_result"]
