"""Anthropic prompt cache 标记：TTL 映射 + 尾部滚动断点（anthropic-cache）。

Anthropic 的 prompt cache 由请求里的 ``cache_control`` 标记决定：标记处（含）之前的
tools + system + messages 前缀被写入缓存，后续请求前缀一致即按缓存价读取。

两处补强：

1. **TTL 透传**：``CacheBreakpoint.ttl_seconds`` 以前是死字段，恒按默认 5 分钟。现映射
   300 → 默认（不写 ttl）、3600 → ``"ttl": "1h"``；Anthropic 只支持这两档，其他值显式
   报错而非就近取整。同一请求内所有标记用同一 TTL（Anthropic 要求长 TTL 标记排在短 TTL
   之前，统一 TTL 天然满足）。
2. **尾部滚动断点**：内核只在 cache anchor（已缓存前缀的末条）打一个标记，anchor 之后的
   尾部每次请求都按全价计费；工具循环里尾部逐轮变长，重复计费可观。在最后一条消息上再打
   一个标记，下一次请求即可按缓存价读取本次的完整前缀，只有新增部分按写入计费。
   总标记数 ≤ 2，低于 Anthropic 的 4 个上限。

参照：Claude Code 在最后一条消息打 ``cache_control`` 的做法；Anthropic prompt caching 文档
（incremental caching / 1-hour TTL）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.llm.errors import InvalidRequestError

if TYPE_CHECKING:
    from taifeng.llm.types import CacheBreakpoint

# Anthropic 支持的两档 TTL（秒）→ cache_control 里的 ttl 值（None = 用服务端默认 5m）
_TTL_VALUES: dict[int, str | None] = {300: None, 3600: "1h"}
# 不能挂 cache_control 的块类型：thinking 块由服务端签名，原样回传不得附加字段
_UNMARKABLE_BLOCKS = frozenset({"thinking", "redacted_thinking"})


def cache_control_for(ttl_seconds: int) -> dict[str, Any]:
    """TTL（秒）→ Anthropic ``cache_control`` 对象。

    Raises:
        InvalidRequestError: 不是 300 / 3600（Anthropic 只有 5m / 1h 两档）。
    """
    if ttl_seconds not in _TTL_VALUES:
        raise InvalidRequestError(
            f"anthropic prompt cache supports ttl_seconds 300 or 3600, got {ttl_seconds}")
    ttl = _TTL_VALUES[ttl_seconds]
    return {"type": "ephemeral"} if ttl is None else {"type": "ephemeral", "ttl": ttl}


def resolve_cache_control(
    breakpoints: list[CacheBreakpoint], ttl_override: int | None,
) -> dict[str, Any]:
    """本次请求统一使用的 ``cache_control``。

    ``ttl_override``（客户端配置）优先；否则取断点声明的 TTL，无断点时默认 300。

    Raises:
        InvalidRequestError: 断点 TTL 不一致，或 TTL 不是 Anthropic 支持的两档。
    """
    if ttl_override is not None:
        return cache_control_for(ttl_override)
    ttls = {bp.ttl_seconds for bp in breakpoints}
    if len(ttls) > 1:
        raise InvalidRequestError(
            f"anthropic prompt cache requires one ttl per request, got {sorted(ttls)}")
    return cache_control_for(ttls.pop() if ttls else 300)


def mark_tail(messages: list[dict[str, Any]], control: dict[str, Any]) -> None:
    """在最后一条消息的最后一个可标记块上打 ``cache_control``（原地修改）。

    该块已有标记（恰为 anchor）时不重复打；全是 thinking 块（极少见）则不打。
    """
    if not messages:
        return
    for block in reversed(messages[-1]["content"]):
        if block.get("type") in _UNMARKABLE_BLOCKS:
            continue
        block.setdefault("cache_control", dict(control))
        return
