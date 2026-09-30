"""engine operation 生命周期：派发 / 守护 / 终结事件 / 遗忘 / 收敛

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `engine-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 engine 唯一持有。

**兄弟调用一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，兄弟模块
与测试按原名调用/打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from taifeng.llm.errors import classify_failure, suggested_action_for
from taifeng.loop.cancellation import CancelReason
from taifeng.loop.event import EventMsg, TurnFailed
from taifeng.loop.failure_policy import resolve_recovery

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from taifeng.loop.engine import AgentEngine

logger = logging.getLogger(__name__)

_COOPERATIVE_CANCEL_SECONDS = 2.0
"""审计模式关闭时给在飞 turn 的协作取消宽限期；须小于 pool 对 actor 收敛的上限（5s）。"""


class EngineOperations:
    """engine operation 生命周期协作器（持 engine 引用，自身无状态）。"""

    converging: bool = False
    """Engine 正在收敛：root gate 不再放行新的 turn（拿到 gate 的排队消息只应用不运行）。"""

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
                        "recovery": resolve_recovery(
                            self._engine._failure_policy, failure_class
                        ),
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
        """取消并等待所有 operation；actor 自身取消也不得截断收敛。

        审计模式先协作取消（ADR 0102）：让在飞的 turn 经自己的取消 token 收尾——工具给出确定的
        结果、意图收敛为 cancelled 终态——再对没有在期限内收敛的 task 做 raw cancel。raw cancel
        会截断意图落账与收敛之间的窗口，留下没有结果的意图。
        """
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

    async def converge_turns_cooperatively(self) -> asyncio.CancelledError | None:
        """审计模式：取消持有 root gate 的 turn，在宽限期内等 operation 自行退出（ADR 0102）。

        在 raw cancel 之前调用，且此时 Engine 根取消 token 尚未取消。只取消在飞的那个 turn；
        排队的消息不动它们的 token——它们在 gate 空出来之后照常拿到 gate，此时 ``converging``
        已置位，gate 拒绝它们运行 turn，于是「只应用不运行」，且应用发生在在飞 turn 收尾之后
        （对话顺序不被打乱）。没有 turn 在飞或排队时什么都不等。
        """
        engine = self._engine
        self.converging = True
        if engine._audit_state is None or not engine._pending or not engine._operation_tasks:
            return None
        owner = engine._root_gate_owner
        for pending in list(engine._pending.values()):
            if pending.submission_id == owner:
                pending.cancel.cancel(CancelReason.REQUESTED, "engine_shutdown")
        waiter = asyncio.gather(*engine._operation_tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), _COOPERATIVE_CANCEL_SECONDS)
        except TimeoutError:
            # 宽限期内没收敛：交给 raw cancel（冻结与否由落账路径自己判定）
            logger.warning("audited turns did not converge cooperatively before shutdown")
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            return exc
        return None

    # suspension-TTL 实现已下沉 suspension_ttl.py（Wave 4 模块切分）。
    # 以下薄委托保留白盒寻址名：spawn_driver / 多个测试按 engine._arm_ttl_timer、
    # engine._ttl_expire_after 调用，test_pool_operation_ownership_review 还对
    # AgentEngine._rearm_ttl_timers_cold 做 monkeypatch.setattr。
