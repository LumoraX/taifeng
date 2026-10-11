"""MCP 资源清理必须尽力继续并保留首个错误。"""

from __future__ import annotations

import pytest


async def test_cleanup_continues_after_errors() -> None:
    """前后两次错误不阻止池关闭，首个异常仍是调用者看到的原因。"""
    from taifeng.mcp.cleanup import cleanup_all

    closed: list[str] = []

    async def first() -> None:
        """第一个清理失败。"""
        raise ValueError("first")

    async def second() -> None:
        """另一个资源也失败。"""
        raise RuntimeError("second")

    async def pool() -> None:
        """连接池仍得到关闭机会。"""
        closed.append("pool")

    with pytest.raises(ValueError, match="first"):
        await cleanup_all((first, second, pool))
    assert closed == ["pool"]
