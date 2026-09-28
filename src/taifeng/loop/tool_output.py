"""tool_output —— 工具结果进入历史前的两道收口：PostToolUse 改写 + 统一大小上限。

两者都作用在「工具已执行完、结果尚未回填历史」这一点上，且顺序固定：
先跑 PostToolUse（宿主可清洗 / 改写模型看到的输出），再按上限截断（钩子也可能把
输出变长，上限须对最终文本生效）。

- **PostToolUse 改写**：handler 返回 ``HookDecision.ok(output_override="...")`` 即替换
  ``ToolResult.output``；多个 handler 链式生效（后者看到前者改写后的输出）。此前返回值
  被丢弃，宿主无处做 prompt injection 清洗、敏感信息脱敏。PostToolUse 不可否决（工具已
  执行）；handler 抛异常或给出非字符串 override 记错误日志、输出保持不变——与既有
  审计型钩子同一约定。需要「失败即不放行」的清洗钩子应自行捕获异常并返回安全的 override。
- **统一上限**：``ContextBudget.max_tool_result_bytes`` 以 UTF-8 字节计，超限保头尾、
  省中间并写明省略量。此前只有个别内置工具自带截断，MCP / 业务工具的输出可无限长地
  进入历史。配置了 ``OffloadStrategy`` 时不截断：offload 会把大结果无损落盘，先截断
  反而让落盘内容残缺。

参照：codex ``utils/output-truncation``（按预算保头尾）；claw-code PostToolUse 的
``updatedOutput``。差异：上限挂在 ContextBudget（上下文预算的一部分），不按工具逐个配置。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from taifeng.hooks.types import PostToolUseHook

if TYPE_CHECKING:
    from taifeng.context.budget import ContextBudget
    from taifeng.context.compressor import CompressionOrchestrator
    from taifeng.hooks.types import HookContext
    from taifeng.tool.spec import ToolResult

logger = logging.getLogger(__name__)

# 截断标记（模型可见）：说明被省略的字节数与应对方式
_CAP_MARKER = (
    "\n…[tool output exceeded the {cap}-byte limit; {omitted} bytes elided from the "
    "middle — narrow the request to see them]…\n"
)
# 头部占可用预算的比例：头部常是命令 / 起始上下文，尾部常是错误与结论
_HEAD_RATIO = 0.6


def tool_result_cap(
    budget: ContextBudget, compressors: CompressionOrchestrator | None,
) -> int | None:
    """本 turn 生效的工具结果字节上限；None = 不截断。

    配置了 ``OffloadStrategy`` 时返回 None：大结果交给 offload 无损落盘。
    """
    cap = budget.max_tool_result_bytes
    if cap is None or compressors is None:
        return cap
    # 局部 import：loop → context.strategies 仅此一处需要，避免模块级依赖面扩大
    from taifeng.context.strategies.offload import OffloadStrategy

    if any(isinstance(s, OffloadStrategy) for s in compressors.strategies):
        return None
    return cap


def cap_tool_result(
    result: ToolResult, max_bytes: int | None,
) -> tuple[ToolResult, dict[str, int] | None]:
    """按字节上限截断 ``result.output``（保头尾、省中间）。

    Returns:
        ``(结果, 截断信息)``；未截断时截断信息为 None，否则为
        ``{"original_bytes", "cap_bytes"}``（供 ToolCallCompleted 事件观测）。
        切点落在多字节字符中间时丢弃该字符的残片（``errors="ignore"``），
        最终文本仍是合法 UTF-8。附件（图片）不受影响，另有图片策略限额。
    """
    if max_bytes is None:
        return result, None
    raw = result.output.encode("utf-8")
    if len(raw) <= max_bytes:
        return result, None
    # 用最大可能的标记长度预留预算，保证截断后总长不超上限
    marker_budget = len(_CAP_MARKER.format(cap=max_bytes, omitted=len(raw)).encode("utf-8"))
    content_budget = max(0, max_bytes - marker_budget)
    head_len = int(content_budget * _HEAD_RATIO)
    tail_len = content_budget - head_len
    head = raw[:head_len].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - tail_len:].decode("utf-8", errors="ignore") if tail_len else ""
    omitted = len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    output = head + _CAP_MARKER.format(cap=max_bytes, omitted=omitted) + tail
    return replace(result, output=output), {"original_bytes": len(raw), "cap_bytes": max_bytes}


async def apply_post_tool_hooks(
    hooks: Any,  # HookRunner
    *,
    tool_name: str,
    call_id: str,
    arguments: dict[str, Any],
    result: ToolResult,
    duration_ms: int,
    hook_ctx: HookContext,
) -> tuple[ToolResult, bool]:
    """串行跑全部 PostToolUse handler，应用 ``output_override``。

    直接遍历 registry handlers：``HookRunner.run`` 在全部放行时返回新的
    ``HookDecision.ok()``，会丢失 handler 给出的 metadata（与 PreToolUse 同处理）。

    Returns:
        ``(最终结果, 是否被改写)``。
    """
    rewritten = False
    for handler in hooks.registry.handlers("post_tool_use"):
        hook = PostToolUseHook(
            tool_name=tool_name, arguments=arguments, output=result.output,
            is_error=result.is_error, duration_ms=duration_ms, call_id=call_id,
        )
        try:
            decision = await handler(hook, hook_ctx)
        except Exception:
            # 工具已执行，钩子失败不能撤销结果；记日志后继续后续 handler
            logger.exception("post_tool_use hook raised for %s; output unchanged", tool_name)
            continue
        override = decision.metadata.get("output_override")
        if override is None:
            continue
        if not isinstance(override, str):
            logger.error(
                "post_tool_use hook returned non-str output_override (%s) for %s; ignored",
                type(override).__name__, tool_name,
            )
            continue
        result = replace(result, output=override)
        rewritten = True
    return result, rewritten
