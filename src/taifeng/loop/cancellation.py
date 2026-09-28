"""父子级联取消 token（含取消原因与墙钟截止时间）。

参照：docs/architecture/agent-loop.md §Cancellation 父子化
对标：codex tokio_util::CancellationToken；Go ``context.WithDeadline`` / ``context.Cause``

cancel-reason-deadline：
    - ``cancel(reason=..., detail=...)`` 记录**为什么**被取消，级联到全部后代（后代看到的
      是祖先的原因）——终态据此区分「用户中止」「超时」「关停」；
    - ``child(deadline_seconds=...)`` / ``set_deadline(...)`` 设墙钟上限，到点以
      ``DEADLINE_EXCEEDED`` 自动取消。父的 deadline 经级联天然覆盖整棵子树，宿主只需在
      子树根上设一次；``deadline_remaining()`` 返回祖先链上最早的那个。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator


class CancelReason(StrEnum):
    """取消原因（随取消级联到后代）。"""

    REQUESTED = "requested"                  # 显式取消（CancelTurn / kill_spawn / 业务调用）
    DEADLINE_EXCEEDED = "deadline_exceeded"  # 墙钟截止时间到
    SHUTDOWN = "shutdown"                    # engine / pool 关停


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

    __slots__ = (
        "_name", "_parent", "_children", "_event", "_cancelled",
        "_reason", "_detail", "_deadline_at", "_deadline_handle", "_callbacks",
    )

    def __init__(self, *, name: str = "root", parent: CancellationToken | None = None) -> None:
        self._name = name
        self._parent = parent
        self._children: list[CancellationToken] = []
        self._event = asyncio.Event()
        self._cancelled = False
        # cancel-reason-deadline：取消原因 / 说明；未取消时为 None
        self._reason: CancelReason | None = None
        self._detail: str | None = None
        # 本 token 自身的截止时刻（event loop 时钟）与到期定时器
        self._deadline_at: float | None = None
        self._deadline_handle: asyncio.TimerHandle | None = None
        # 取消时同步调用的回调（interrupt_on_cancel 用来原地打断阻塞中的 await）
        self._callbacks: list[Callable[[], None]] = []
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

    @property
    def reason(self) -> CancelReason | None:
        """取消原因；未取消为 None。被祖先级联取消时为祖先的原因。"""
        return self._reason

    @property
    def detail(self) -> str | None:
        """取消说明（可选，随原因级联）。"""
        return self._detail

    def child(self, name: str = "", *, deadline_seconds: float | None = None) -> CancellationToken:
        """派生子 token，绑定父子关系。

        如果父 token 已取消，新子 token 立即以父的原因标记为 cancelled。

        Args:
            name: 子 token 名（拼到父名之后）。
            deadline_seconds: 可选墙钟上限（秒），见 ``set_deadline``。
        """
        full_name = f"{self._name}/{name}" if name else f"{self._name}/anon"
        child = CancellationToken(name=full_name, parent=self)
        if self._cancelled:
            child._mark_cancelled(self._reason, self._detail)
        elif deadline_seconds is not None:
            child.set_deadline(deadline_seconds)
        return child

    def set_deadline(self, seconds: float) -> None:
        """设墙钟截止时间：``seconds`` 秒后以 ``DEADLINE_EXCEEDED`` 取消本 token 及其子树。

        重复设置取更早者（截止时间只能收紧，不能放宽——放宽会让已承诺的上限失效）。
        必须在 event loop 内调用。

        Raises:
            ValueError: ``seconds`` 不为正。
        """
        if seconds <= 0:
            raise ValueError(f"deadline seconds must be positive, got {seconds}")
        if self._cancelled:
            return
        loop = asyncio.get_running_loop()
        at = loop.time() + seconds
        if self._deadline_at is not None and self._deadline_at <= at:
            return
        if self._deadline_handle is not None:
            self._deadline_handle.cancel()
        self._deadline_at = at
        self._deadline_handle = loop.call_at(
            at, self.cancel, CancelReason.DEADLINE_EXCEEDED, f"deadline {seconds:g}s")

    def deadline_remaining(self) -> float | None:
        """距最早截止时间（本 token 与全部祖先中）还剩多少秒；无截止时间为 None。"""
        earliest: float | None = None
        token: CancellationToken | None = self
        while token is not None:
            if token._deadline_at is not None and (
                    earliest is None or token._deadline_at < earliest):
                earliest = token._deadline_at
            token = token._parent
        if earliest is None:
            return None
        return max(0.0, earliest - asyncio.get_running_loop().time())

    def _detach_from_parent(self) -> bool:
        """供 package owner 解除直接 parent 边，不改变自身或 subtree 取消状态。

        这是 coordinator 生命周期清理边界，不是稳定 public cancellation API。
        root、已脱链，或 parent 已不再持有该 identity 时返回 ``False``。
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

    def cancel(
        self, reason: CancelReason = CancelReason.REQUESTED, detail: str | None = None,
    ) -> None:
        """取消本 token 与所有后代；首次取消的原因生效（重复取消不改写原因）。

        Args:
            reason: 取消原因，级联给全部后代。
            detail: 可选说明（如 ``"kill_spawn"`` / ``"deadline 30s"``）。
        """
        if self._cancelled:
            return
        self._mark_cancelled(reason, detail)
        for child in self._children:
            child.cancel(reason, detail)

    def _mark_cancelled(self, reason: CancelReason | None, detail: str | None) -> None:
        self._cancelled = True
        self._reason = reason or CancelReason.REQUESTED
        self._detail = detail
        # 已取消则到期定时器作废（防止到点再触发一次、也释放 loop 引用）
        if self._deadline_handle is not None:
            self._deadline_handle.cancel()
            self._deadline_handle = None
        self._event.set()
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback()

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """登记取消回调（取消时同步调用一次）；返回注销函数。

        已取消时立即调用。回调应当轻量且不抛异常（在取消方的调用栈里执行）。
        """
        if self._cancelled:
            callback()
            return lambda: None
        self._callbacks.append(callback)

        def _remove() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return _remove

    async def wait_cancelled(self) -> None:
        """等待 token 被取消。可与 ``anyio.race`` / ``asyncio.wait`` 组合。"""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """如已取消则抛出 ``asyncio.CancelledError``（消息含原因）。"""
        if self._cancelled:
            raise asyncio.CancelledError(f"token cancelled: {self._name} ({self._reason})")

    def descendants(self) -> Iterator[CancellationToken]:
        """遍历所有后代（用于调试）。"""
        for child in self._children:
            yield child
            yield from child.descendants()

    def __repr__(self) -> str:
        status = f"cancelled:{self._reason}" if self._cancelled else "active"
        return f"<CancellationToken {self._name} {status}>"


@asynccontextmanager
async def interrupt_on_cancel(token: CancellationToken) -> AsyncIterator[None]:
    """在块内让 token 取消**原地打断**当前 task 正阻塞的 await（R4，cancel-reason-deadline）。

    只在收到流事件时 ``raise_if_cancelled`` 不够：provider 在首字节前长时间阻塞时，
    取消 / 截止时间要等到下一个事件才生效。本上下文在 token 取消时对**当前 task**
    调 ``cancel()``，把阻塞中的读取就地打断——读取始终在所属 task 里进行（审计路径的
    observed session 要求流只能在 owner task 迭代，另起 task 读取会被拒）。

    退出时把由本上下文引起的 task 取消 ``uncancel`` 掉，改抛带原因的
    ``asyncio.CancelledError``：外层据 ``token.is_cancelled`` 按「token 取消 → 优雅终结」
    处理（K5），而不会被误判为外部 ``task.cancel``。参照 ``asyncio.timeout`` 的同一手法。
    """
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("interrupt_on_cancel requires a running task")
    fired = False

    def _interrupt() -> None:
        nonlocal fired
        fired = True
        task.cancel()

    remove = token.on_cancel(_interrupt)
    try:
        yield
    except asyncio.CancelledError:
        # 仅当取消完全由本上下文引起（uncancel 后计数归零）才改写为 token 取消；
        # 同时存在外部取消时照常外抛，保持外部 task.cancel 语义
        if fired and task.uncancel() == 0:
            raise asyncio.CancelledError(
                f"token cancelled: {token.name} ({token.reason})") from None
        raise
    finally:
        remove()
