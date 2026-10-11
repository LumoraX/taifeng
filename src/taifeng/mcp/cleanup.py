"""MCP 清理的异常优先级与尽力收敛。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


async def cleanup_all(actions: Iterable[Callable[[], Awaitable[None]]]) -> None:
    """逐一尝试全部资源清理，最后抛出首个失败，避免后续错误覆盖原因。"""
    first_error: BaseException | None = None
    for action in actions:
        try:
            await action()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


async def close_after_failure(close: Callable[[], Awaitable[None]]) -> None:
    """已有握手异常时继续关闭；清理失败只记类型，不覆盖调用者的原始异常。"""
    try:
        await close()
    except BaseException as error:
        logger.debug("mcp: cleanup after failed handshake type=%s", type(error).__name__)
