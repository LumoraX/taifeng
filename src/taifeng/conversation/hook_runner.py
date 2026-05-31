"""IndexHook fire-and-forget 调度器 —— 管理后台 task 生命周期、捕获异常发事件、shutdown grace period。

行为契约（spec：index-hook capability）：

- ``spawn_*`` 方法返回前 SHALL 不阻塞调用方
- IndexHook 方法抛异常 SHALL 捕获 + 通过 sink 发 ``index_hook_failed``（含 method / thread_id / cause_repr）
- ``shutdown(grace_seconds)`` SHALL await pending tasks，超时未完成 SHALL cancel 每个 + 发 ``index_hook_abandoned``
- 主路径（writer / directory / engine）SHALL 不受 hook 失败影响
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import ResponseItem, ThreadMetadata
from taifeng.conversation.protocols import IndexHook
from taifeng.loop.event import (
    EventMsg,
    IndexHookAbandoned,
    IndexHookFailed,
)

if TYPE_CHECKING:
    from taifeng.telemetry.sink import TelemetrySink


class HookRunner:
    """IndexHook 后台任务调度器。

    用法：

        runner = HookRunner(hook=my_index_hook, sink=optional_sink)
        runner.spawn_on_thread_created(meta)
        ...
        await runner.shutdown(grace_seconds=5.0)
    """

    def __init__(self, *, hook: IndexHook, sink: "TelemetrySink | None" = None) -> None:
        # 构造期 Protocol 校验：失败立即 raise TypeError（spec 强制）
        if not isinstance(hook, IndexHook):
            raise TypeError(
                f"index_hook 对象 {type(hook).__name__} 不满足 IndexHook 协议"
                "（缺少 on_thread_created / on_message_appended / on_metadata_updated 之一）"
            )
        self._hook = hook
        self._sink = sink
        self._pending: set[asyncio.Task[None]] = set()

    # -----------------------------------------------------------------
    # spawn helpers
    # -----------------------------------------------------------------

    def spawn_on_thread_created(self, meta: ThreadMetadata) -> None:
        self._spawn("on_thread_created", meta.thread_id, self._hook.on_thread_created(meta))

    def spawn_on_message_appended(self, thread_id: str, items: list[ResponseItem]) -> None:
        self._spawn(
            "on_message_appended",
            thread_id,
            self._hook.on_message_appended(thread_id, items),
        )

    def spawn_on_metadata_updated(self, thread_id: str, patch: dict[str, Any]) -> None:
        self._spawn(
            "on_metadata_updated",
            thread_id,
            self._hook.on_metadata_updated(thread_id, patch),
        )

    def _spawn(self, method: str, thread_id: str | None, coro: Any) -> None:
        """通用 spawn：把 hook 协程包装成跟踪 task + 异常路径发事件。"""
        task = asyncio.create_task(self._invoke(method, thread_id, coro), name=f"hook:{method}:{thread_id}")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _invoke(self, method: str, thread_id: str | None, coro: Any) -> None:
        try:
            await coro
        except BaseException as e:  # noqa: BLE001 —— 全量捕获，包括 CancelledError
            # CancelledError：abandoned 路径会单独发 IndexHookAbandoned；此处只在非取消时报 failed
            if isinstance(e, asyncio.CancelledError):
                raise  # 让 cancel 信号正常传播给 shutdown 等待方
            await self._emit(
                IndexHookFailed(data={"method": method, "thread_id": thread_id, "cause": repr(e)})
            )

    # -----------------------------------------------------------------
    # shutdown
    # -----------------------------------------------------------------

    async def shutdown(self, *, grace_seconds: float = 5.0) -> None:
        """等待 pending task，最多 grace_seconds；超时未完成 SHALL cancel + 发事件。

        实现注意：用 ``asyncio.wait(..., timeout=)`` 而非 ``wait_for(gather(...))`` ——
        后者超时时会主动 cancel 内部 gather，导致 task 在我们决策前已被 cancel；
        ``wait`` 仅返回 done/pending 分组，不修改 task 状态，我们才能识别真正超时的 task。
        """
        if not self._pending:
            return
        pending_snapshot = list(self._pending)
        done, pending = await asyncio.wait(pending_snapshot, timeout=grace_seconds)
        if not pending:
            return
        # 超时未完成的 task：cancel + 发 abandoned
        for task in pending:
            name = task.get_name()
            method, thread_id = self._parse_task_name(name)
            task.cancel()
            await self._emit(
                IndexHookAbandoned(data={"method": method, "thread_id": thread_id})
            )
        # 等被 cancel 的 task 退出（最多 1s 防卡死）
        await asyncio.wait(pending, timeout=1.0)

    @staticmethod
    def _parse_task_name(name: str) -> tuple[str, str | None]:
        """从 ``hook:<method>:<thread_id>`` 反解。"""
        parts = name.split(":", 2)
        if len(parts) >= 3 and parts[0] == "hook":
            return parts[1], parts[2] if parts[2] != "None" else None
        return name, None

    # -----------------------------------------------------------------
    # 事件发射
    # -----------------------------------------------------------------

    async def _emit(self, msg: Any) -> None:
        if self._sink is None:
            return
        try:
            await self._sink.handle(EventMsg(submission_id="*", msg=msg))
        except Exception:
            # 事件发射失败不影响主路径（同 sqlite_directory._emit 语义）
            pass


__all__ = ["HookRunner"]
