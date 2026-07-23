"""父子级联取消 token。

参照：docs/architecture/agent-loop.md §Cancellation 父子化
对标：codex tokio_util::CancellationToken
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class CancellationToken:
    """父子级联取消 token。

    设计要点：
        - ``parent.cancel()`` 触发后，所有 child 同步标记 cancelled
        - ``child.cancel()`` 只影响自身和孙级，不影响 parent
        - 通过 ``wait_cancelled()`` 与 anyio task group 集成

    示例：
        >>> root = CancellationToken(name="engine")
        >>> sub = root.child(name="sub:abc123")
        >>> tool = sub.child(name="tool:read_file")
        >>> root.cancel()  # sub 和 tool 同步 cancelled
    """

    __slots__ = ("_name", "_parent", "_children", "_event", "_cancelled")

    def __init__(self, *, name: str = "root", parent: CancellationToken | None = None) -> None:
        self._name = name
        self._parent = parent
        self._children: list[CancellationToken] = []
        self._event = asyncio.Event()
        self._cancelled = False
        if parent is not None:
            parent._children.append(self)

    @property
    def name(self) -> str:
        """token 名称，用于调试和 telemetry。"""
        return self._name

    @property
    def is_cancelled(self) -> bool:
        """是否已被取消。"""
        return self._cancelled

    def child(self, name: str = "") -> CancellationToken:
        """派生子 token，绑定父子关系。

        如果父 token 已取消，新子 token 立即标记为 cancelled。
        """
        full_name = f"{self._name}/{name}" if name else f"{self._name}/anon"
        child = CancellationToken(name=full_name, parent=self)
        if self._cancelled:
            child._mark_cancelled()
        return child

    def detach(self) -> bool:
        """安全解除当前 token 与直接 parent 的边，不改变自身或 subtree 取消状态。

        root、已 detach，或 parent 已不再持有该 identity 时返回 ``False``。
        """
        parent = self._parent
        if parent is None:
            return False
        for index, child in enumerate(parent._children):
            if child is self:
                parent._children.pop(index)
                self._parent = None
                return True
        self._parent = None
        return False

    def cancel(self) -> None:
        """取消本 token 与所有后代。"""
        if self._cancelled:
            return
        self._mark_cancelled()
        for child in self._children:
            child.cancel()

    def _mark_cancelled(self) -> None:
        self._cancelled = True
        self._event.set()

    async def wait_cancelled(self) -> None:
        """等待 token 被取消。可与 ``anyio.race`` / ``asyncio.wait`` 组合。"""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """如已取消则抛出 ``asyncio.CancelledError``。"""
        if self._cancelled:
            raise asyncio.CancelledError(f"token cancelled: {self._name}")

    def descendants(self) -> Iterator[CancellationToken]:
        """遍历所有后代（用于调试）。"""
        for child in self._children:
            yield child
            yield from child.descendants()

    def __repr__(self) -> str:
        status = "cancelled" if self._cancelled else "active"
        return f"<CancellationToken {self._name} {status}>"
