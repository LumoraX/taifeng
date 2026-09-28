"""memory —— 让模型主动检索 / 写入长期记忆的薄工具（opt-in 内置工具，ADR 0064）。

定位：K3 ``MemoryStore`` 协议的**模型侧入口**。内核在每个 turn 前后被动调用
``prefetch`` / ``writeback``（page-in / 脏页写回）；本工具让模型在 turn 中途按自己的判断
「查一下记忆」「把这条记下来」。读写**全部委托**注入的 ``MemoryStore``，内核不内置任何
存储后端（ADR 0017 规则③）。

动作集合只取协议已支持的：

| 动作 | 委托 | 说明 |
| --- | --- | --- |
| ``search`` | ``prefetch(query, thread_id=...)`` | 以模型给的 query 检索；返回文本按字符上限截断 |
| ``save`` | ``writeback(thread_id=..., items=[<assistant_message>])`` | 写入一条模型撰写的要点 |

协议没有删除 / 更新语义，本工具也不提供（遗忘策略属于后端，见 ADR 0064）。

``save`` 写入的 item：``kind="assistant_message"``、``payload={"text": 要点, "model": ""}``、
``metadata={"source": "memory_tool", "call_id": <本次调用 id>}``——按 user/assistant 文本
沉淀的既有后端无需改动即可收下；需要区分「模型主动记忆」与「turn 结束的脏页写回」、
或按 call_id 去重的后端读 ``metadata`` 即可。

装配 = 同一 store 实例双注入（与 todo builtin 同一范式）::

    store = MyMemory(...)
    pool = await EnginePool.create(
        ..., memory_store=store, extra_tools=[make_memory_tool(store)],
    )

只读知识库（继承 ``NullMemoryStore`` 仅覆写 prefetch）应传 ``actions=("search",)``：
否则 ``save`` 会落到 no-op 的 writeback，模型以为记住了、实际什么都没存。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from taifeng.conversation.models import ResponseItem
from taifeng.loop.cancellation import interrupt_on_cancel
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from taifeng.context.memory import MemoryStore

logger = logging.getLogger(__name__)

MemoryAction = Literal["search", "save"]
MEMORY_ACTIONS: tuple[MemoryAction, ...] = ("search", "save")

#: save 写入 item 的 ``metadata["source"]`` 取值（后端据此识别模型主动记忆）
MEMORY_TOOL_SOURCE = "memory_tool"

#: 检索语句字符上限：防模型把整段上下文当 query 塞给后端
_MAX_QUERY_CHARS = 2000


def _bad_args(message: str) -> ToolResult:
    """参数错误的统一形状。"""
    return ToolResult.error(f"bad_args: {message}", reason="bad_args")


def _cancelled(ctx: ToolContext) -> ToolResult:
    """取消的统一形状（与 shell_exec / 搜索工具一致）。"""
    return ToolResult.error(f"cancelled ({ctx.cancel.reason})", reason="cancelled")


async def _call_store[T](
    ctx: ToolContext,
    action: MemoryAction,
    op: Callable[[], Awaitable[T]],
    on_ok: Callable[[T], ToolResult],
) -> ToolResult:
    """执行一次 store 调用：取消原地打断（R4），后端异常显式返回错误结果。

    与内核被动钩子（best-effort、吞异常只记日志）不同：这里是模型**主动**发起的动作，
    失败必须让模型看见，否则它会以为已检索 / 已记住。
    """
    try:
        async with interrupt_on_cancel(ctx.cancel):
            value = await op()
    except asyncio.CancelledError:
        if not ctx.cancel.is_cancelled:
            raise  # 外部 task 取消：照常外抛（K5）
        return _cancelled(ctx)
    except Exception as exc:
        logger.warning("memory tool %s failed", action, exc_info=True)
        return ToolResult.error(
            f"memory_error: {action} failed: {type(exc).__name__}: {exc}",
            reason="memory_error",
            action=action,
        )
    return on_ok(value)


def _normalize_actions(actions: Sequence[MemoryAction]) -> tuple[MemoryAction, ...]:
    """去重保序并校验动作集合。

    Raises:
        ValueError: 空集合或含协议不支持的动作。
    """
    enabled = tuple(dict.fromkeys(actions))
    if not enabled:
        raise ValueError("actions must not be empty")
    unknown = [a for a in enabled if a not in MEMORY_ACTIONS]
    if unknown:
        raise ValueError(f"unsupported memory actions: {unknown} (supported: {MEMORY_ACTIONS})")
    return enabled


def _schema(enabled: tuple[MemoryAction, ...]) -> dict[str, Any]:
    """按启用的动作生成 input_schema：未启用动作的参数不出现在 schema 里。"""
    properties: dict[str, Any] = {
        "action": {"type": "string", "enum": list(enabled), "description": "要执行的动作"},
    }
    if "search" in enabled:
        properties["query"] = {"type": "string", "description": "search 用：检索语句"}
    if "save" in enabled:
        properties["content"] = {
            "type": "string",
            "description": "save 用：要长期记住的要点（自成一句，脱离上下文也能看懂）",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["action"],
        "additionalProperties": False,
    }


def _description(enabled: tuple[MemoryAction, ...], max_result_chars: int) -> str:
    """LLM 可见描述：只描述已启用的动作。"""
    parts = ["读写长期记忆（跨会话保留，由宿主的记忆后端存储）。"]
    if "search" in enabled:
        parts.append(
            f"action=search + query：检索相关记忆，结果最多 {max_result_chars} 字符"
            "（超出截断并注明）。"
        )
    if "save" in enabled:
        parts.append("action=save + content：记下一条值得长期保留的要点（偏好、结论、约定）。")
    return "".join(parts)


def _render_search(text: str, max_result_chars: int) -> ToolResult:
    """search 结果渲染：空串 = 无相关记忆（正常结果，不是错误）；超长按字符截断并注明。"""
    if not text.strip():
        return ToolResult.ok("no relevant memory found", action="search", chars=0)
    truncated = len(text) > max_result_chars
    body = text[:max_result_chars]
    if truncated:
        body += f"\n\n[memory result truncated to {max_result_chars} chars]"
    return ToolResult.ok(body, action="search", chars=len(text), truncated=truncated)


async def _search(
    store: MemoryStore, args: dict[str, Any], ctx: ToolContext, *, max_result_chars: int,
) -> ToolResult:
    """action=search：校验 query 后委托 ``store.prefetch``（thread_id 取当前工具上下文）。"""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _bad_args("search requires a non-empty string `query`")
    if len(query) > _MAX_QUERY_CHARS:
        return _bad_args(f"query longer than {_MAX_QUERY_CHARS} chars")
    return await _call_store(
        ctx, "search",
        lambda: store.prefetch(query, thread_id=ctx.thread_id),
        lambda text: _render_search(text, max_result_chars),
    )


async def _save(
    store: MemoryStore, args: dict[str, Any], ctx: ToolContext, *, max_save_chars: int,
) -> ToolResult:
    """action=save：把一条要点包装成 assistant_message 委托 ``store.writeback``。

    超长以 ``too_large`` 拒绝而不是截断写入——半截要点写进长期记忆比不写更糟。
    """
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return _bad_args("save requires a non-empty string `content`")
    if len(content) > max_save_chars:
        return ToolResult.error(
            f"too_large: content exceeds {max_save_chars} chars; save a shorter summary",
            reason="too_large",
        )
    item = ResponseItem(
        kind="assistant_message",
        thread_id=ctx.thread_id,
        payload={"text": content, "model": ""},
        # 后端据 source 区分「模型主动记忆」与 turn 结束的脏页写回；call_id 可作去重键
        metadata={"source": MEMORY_TOOL_SOURCE, "call_id": ctx.call_id},
    )
    return await _call_store(
        ctx, "save",
        lambda: store.writeback(thread_id=ctx.thread_id, items=[item]),
        lambda _: ToolResult.ok(
            f"saved to memory ({len(content)} chars)", action="save", chars=len(content),
        ),
    )


def make_memory_tool(
    store: MemoryStore,
    *,
    actions: Sequence[MemoryAction] = MEMORY_ACTIONS,
    max_result_chars: int = 4000,
    max_save_chars: int = 2000,
    timeout_seconds: float = 30.0,
) -> ToolSpec:
    """构造 memory 工具（opt-in：经 ``EnginePool.create(extra_tools=[...])`` 注册）。

    Args:
        store: 业务实现的 ``MemoryStore``；通常与 ``EnginePool.create(memory_store=)`` 同一实例。
        actions: 启用的动作子集（``"search"`` / ``"save"``）；只读后端传 ``("search",)``。
        max_result_chars: search 返回文本的字符上限（协议返回单段文本，没有条目概念，
            故上限按字符计）。
        max_save_chars: save 单条要点的字符上限；超出以 ``too_large`` 拒绝，不截断写入。
        timeout_seconds: 单次调用超时（ToolSpec 级，超时由 runtime 统一处理）。

    Returns:
        ``ToolSpec(name="memory")``。副作用分类取**已启用动作中最保守**的一档：
        含 save → ``parallel_safe=False`` / ``external_non_idempotent`` / ``manual``
        （后端写入是否幂等内核无从得知，崩溃后交人裁决而不是引导重发）；
        仅 search → ``parallel_safe=True`` / ``pure`` / ``none``。

    Raises:
        ValueError: 动作集合为空或含未知动作；上限参数非正。
    """
    enabled = _normalize_actions(actions)
    if min(max_result_chars, max_save_chars) <= 0:
        raise ValueError("max_result_chars / max_save_chars must be > 0")

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按 action 分派；未启用的动作以 bad_args 拒绝。"""
        action = args.get("action")
        if action not in enabled:
            return _bad_args(f"action must be one of {list(enabled)}")
        if ctx.cancel.is_cancelled:
            return _cancelled(ctx)
        if action == "search":
            return await _search(store, args, ctx, max_result_chars=max_result_chars)
        return await _save(store, args, ctx, max_save_chars=max_save_chars)

    writes = "save" in enabled
    return ToolSpec(
        name="memory",
        description=_description(enabled, max_result_chars),
        input_schema=_schema(enabled),
        handler=handler,
        # 多动作工具的副作用分类取最保守的一档（见 Returns 说明）
        parallel_safe=not writes,
        effect_kind="external_non_idempotent" if writes else "pure",
        reconciliation="manual" if writes else "none",
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "MEMORY_ACTIONS",
    "MEMORY_TOOL_SOURCE",
    "MemoryAction",
    "make_memory_tool",
]
