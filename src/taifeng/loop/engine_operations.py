"""engine operation 生命周期：派发 / 守护 / 终结事件 / 遗忘 / 收敛

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `engine-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 engine 唯一持有。

**兄弟调用一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，兄弟模块
与测试按原名调用/打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from taifeng.llm.errors import classify_failure, suggested_action_for
from taifeng.llm.recovery import recommend_recovery
from taifeng.loop.event import EventMsg, TurnFailed

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine

logger = logging.getLogger(__name__)


class EngineOperations:
    """engine operation 生命周期协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供运行态与共享依赖。
        """
        self._engine = engine

    def start_operation(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
        submission_id: str | None = None,
    ) -> asyncio.Task[None]:
        """创建并登记 Engine-owned operation，终态总会检索异常。

        ``submission_id`` 非 None 时包一层 ``_guarded_operation``：operation 以未捕获
        异常退出也给该 submission 一个 ``turn_failed`` 终结事件并清理 ``_pending``
        （ADR 0029 终结信号完整），否则订阅者只能永久等待、introspect 留幽灵 turn。
        """
        body = (
            coroutine if submission_id is None
            else self._engine._guarded_operation(submission_id, coroutine)
        )
        task = asyncio.create_task(
            body,
            name=f"engine-operation:{self._engine._session_id}:{name}",
        )
        self._engine._operation_tasks.add(task)
        task.add_done_callback(self._engine._forget_operation)
        return task

    async def guarded_operation(
        self, submission_id: str, coroutine: Coroutine[Any, Any, None],
    ) -> None:
        """operation 崩溃 → 终结事件 + 清 _pending，再原样上抛（日志由 _forget_operation 记）。

        取消（收敛路径）原样传播：finalize 会对仍在订阅的 submission 统一投
        engine_shutdown 终结。
        """
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._engine._pending.pop(submission_id, None)
            await self._engine._emit_operation_terminal(submission_id, exc)
            raise

    async def emit_operation_terminal(
        self, submission_id: str, exc: BaseException | None, *, kind: str | None = None,
    ) -> None:
        """给一个 submission 发 engine 层面的 ``turn_failed`` 终结事件。

        exc 非 None → kind 取异常类名、failure_class 走 classify_failure；
        exc None + kind（如 ``engine_shutdown`` / ``cancelled``）→ failure_class=cancelled。
        """
        if exc is not None:
            failure_class, suggested_action = classify_failure(exc)
            error_kind = type(exc).__name__
            error_msg = str(exc) or error_kind
        else:
            failure_class = "cancelled"
            suggested_action = suggested_action_for("cancelled")
            error_kind = kind or "cancelled"
            error_msg = error_kind
        await self._engine._emit(
            EventMsg(
                submission_id=submission_id,
                msg=TurnFailed(
                    data={
                        "error": error_msg,
                        "kind": error_kind,
                        "failure_class": failure_class,
                        "suggested_action": suggested_action,
                        "recovery": recommend_recovery(failure_class).to_dict(),
                        "request_id": None,
                        "iterations": 0,
                        "is_root": True,
                    }
                ),
            )
        )

    def forget_operation(self, task: asyncio.Task[None]) -> None:
        """检索 operation 终态并释放 Engine 显式 ownership。"""
        self._engine._operation_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException as exc:  # noqa: BLE001
            logger.error(
                "engine operation task failed: %s",
                task.get_name(),
                exc_info=exc,
            )

    async def converge_operations(self) -> asyncio.CancelledError | None:
        """取消并等待所有 operation；actor 自身取消也不得截断收敛。"""
        actor_cancellation: asyncio.CancelledError | None = None
        while self._engine._operation_tasks:
            tasks = tuple(self._engine._operation_tasks)
            for task in tasks:
                task.cancel()
            waiter = asyncio.gather(*tasks, return_exceptions=True)
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError as exc:
                    actor_cancellation = actor_cancellation or exc
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    for task in tasks:
                        task.cancel()
            waiter.result()
            # Python 3.13 对全 done futures 的 gather 可 eager 完成，不保证让
            # _forget_operation callback 先运行；此处同步释放 ownership，避免
            # 因 done task 仍留在 set 中形成无 await 的 busy loop。
            for task in tasks:
                self._engine._operation_tasks.discard(task)
        return actor_cancellation

    # suspension-TTL 实现已下沉 suspension_ttl.py（Wave 4 模块切分）。
    # 以下薄委托保留白盒寻址名：spawn_driver / 多个测试按 engine._arm_ttl_timer、
    # engine._ttl_expire_after 调用，test_pool_operation_ownership_review 还对
    # AgentEngine._rearm_ttl_timers_cold 做 monkeypatch.setattr。
