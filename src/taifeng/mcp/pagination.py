"""MCP 列表分页（2025-06-18 §Server Utilities「Pagination」）：``tools/list`` 跟完 ``nextCursor``。

规范要点：

- 游标是**不透明**字符串，页大小由 server 决定，客户端不得假设固定页大小；
- 响应带 ``nextCursor`` 表示还有下一页，客户端把它原样放进下一次请求的 ``params.cursor``；
- 客户端 SHOULD 把缺失的 ``nextCursor`` 视为结束，并同时支持分页 / 不分页两种 server。

旧实现只取第一页：分页的 server 其余工具对模型永远不可见，``list_changed`` 重新同步时还会把
它们当成「已删除」。本模块把翻页收敛为一个与传输无关的循环，并对恶意 / 有缺陷的 server 设防：

| 情形 | 处置 |
| --- | --- |
| ``nextCursor`` 缺失 / ``null`` / 空串 | 结束（空串：Go / Java 等零值序列化常见，原样回传只会重取首页） |
| ``nextCursor`` 非字符串 | ``McpPaginationError``（不猜测、不截断） |
| server 回了已经回过的游标 | ``McpPaginationError``（再翻只会原地打转） |
| 页数超过 ``max_pages`` | ``McpPaginationError``（**不**静默返回已取到的部分） |
| result 非对象 / ``tools`` 非数组 | ``McpToolError``（规范必填字段，形状不对不当成空列表） |

参照：modelcontextprotocol python-sdk ``ClientSession.list_tools(cursor=)``（SDK 只暴露单页，
翻页交给调用方）；差异：taifeng 的 ``McpClient.list_tools`` 契约是「完整列表」，翻页在客户端内完成。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.mcp.bridge import McpToolError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# 单次 tools/list 最多翻多少页：按常见页大小 50～100 估算可容纳数千个工具，
# 远超正常 server 的规模；到顶即判定 server 失控（无限翻页）并显式报错
DEFAULT_MAX_LIST_PAGES = 100

# 客户端侧协议防护失败的错误码（JSON-RPC 实现自定义段，与传输错误同段）
_CLIENT_GUARD_ERROR = -32000


class McpPaginationError(McpToolError):
    """``tools/list`` 翻页失控：页数超限、游标重复或 ``nextCursor`` 形状非法。

    继承 ``McpToolError``：既有 ``except McpToolError`` 的调用方（``bind_mcp_tools``、
    ``McpToolBinding._on_list_changed``）无需改动即可接住。

    Attributes:
        pages: 出错前已取回的页数。
    """

    def __init__(self, message: str, *, pages: int) -> None:
        """构造错误。

        Args:
            message: 人类可读的失败原因（进异常消息）。
            pages: 出错前已取回的页数。
        """
        super().__init__(_CLIENT_GUARD_ERROR, message)
        self.pages = pages


def validate_max_pages(max_pages: int) -> int:
    """校验页数上限旋钮（≥1）并原样返回；客户端构造期调用，坏配置不等到首次翻页才暴露。

    Raises:
        ValueError: ``max_pages`` < 1。
    """
    if max_pages < 1:
        raise ValueError(f"max_list_pages must be >= 1, got {max_pages}")
    return max_pages


def _page_tools(result: Any, page: int) -> list[Any]:
    """取一页里的 ``tools`` 数组；形状不对显式报错（规范必填）。"""
    if not isinstance(result, dict):
        raise McpToolError(_CLIENT_GUARD_ERROR, f"tools/list page {page}: result is not an object")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise McpToolError(_CLIENT_GUARD_ERROR, f"tools/list page {page}: 'tools' is not an array")
    return tools


def _next_cursor(result: dict[str, Any], page: int) -> str | None:
    """取下一页游标；缺失 / null / 空串 = 结束，非字符串显式报错。"""
    cursor = result.get("nextCursor")
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str):
        raise McpPaginationError(
            f"tools/list page {page}: nextCursor must be a string, got {type(cursor).__name__}",
            pages=page)
    return cursor


async def list_all_tools(
    fetch_page: Callable[[str | None], Awaitable[Any]],
    *,
    max_pages: int = DEFAULT_MAX_LIST_PAGES,
) -> list[Any]:
    """跟完 ``nextCursor``，返回全部页的 ``tools`` 拼接结果（按页序）。

    Args:
        fetch_page: 发一次 ``tools/list`` 并返回其 JSON-RPC result；参数为游标
            （首页为 None，此时不带 ``params.cursor``）。
        max_pages: 页数上限（≥1）；超限抛 ``McpPaginationError``。

    Returns:
        各页 ``tools`` 数组按顺序拼接（条目形状由桥逐条校验，这里不过滤）。

    Raises:
        McpPaginationError: 页数超限 / 游标重复 / ``nextCursor`` 非字符串。
        McpToolError: 某页 result 非对象或 ``tools`` 非数组；以及 ``fetch_page`` 自身的失败。
        ValueError: ``max_pages`` < 1。
    """
    validate_max_pages(max_pages)
    tools: list[Any] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for page in range(1, max_pages + 1):
        result = await fetch_page(cursor)
        tools.extend(_page_tools(result, page))
        cursor = _next_cursor(result, page)
        if cursor is None:
            return tools
        # 同一游标出现第二次：继续翻只会重复取同一段，按失控处理而不是等到页数上限
        if cursor in seen_cursors:
            raise McpPaginationError(
                f"tools/list page {page}: server repeated cursor {cursor[:64]!r}", pages=page)
        seen_cursors.add(cursor)
    raise McpPaginationError(
        f"tools/list did not finish within {max_pages} pages (server keeps returning nextCursor)",
        pages=max_pages)


__all__ = [
    "DEFAULT_MAX_LIST_PAGES",
    "McpPaginationError",
    "list_all_tools",
    "validate_max_pages",
]
