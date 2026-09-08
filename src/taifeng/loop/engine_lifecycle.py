"""engine run 收尾：memory 会话结束 / 生命周期收敛 / 孤儿 submission 终结

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `engine-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 engine 唯一持有。

**兄弟调用一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，兄弟模块
与测试按原名调用/打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from taifeng.loop.audit_admission import AcceptedUserMessage
from taifeng.loop.audit_mailbox import finalize_audited_mailbox
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EventMsg, Shutdown as ShutdownMsg

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine

logger = logging.getLogger(__name__)


class EngineLifecycle:
    """engine run 收尾协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供运行态与共享依赖。
        """
        self._engine = engine

    async def memory_session_end(self) -> None:
        """K3 teardown：shutdown 时调 memory_store.on_session_end。best-effort。"""
        if self._engine._memory_store is None:
            return
        try:
            await self._engine._memory_store.on_session_end(
                thread_id=self._engine._thread_id, items=list(self._engine._history)
            )
        except Exception:
            logger.exception("memory on_session_end failed (ignored)")

    async def finalize_run_lifecycle(
        self,
        cancel: CancellationToken,
        *,
        shutdown_requested: bool,
    ) -> None:
        """按原顺序收敛 actor、operation、持久化 flush 与订阅者终态。"""
        self._engine._running = False
        if self._engine._audit_state is not None:
            self._engine._audit_state.coordinator.cancel_session_root()
            await finalize_audited_mailbox(
                self._engine._audit_state,
                self._engine._audited_mailbox,
            )
        cancel.cancel()
        self._engine._cancel_ttl_timers()
        actor_cancellation = await self._engine._converge_operations()
        spawn_cancellation = await self._engine._spawn.converge_owned_tasks()
        actor_cancellation = actor_cancellation or spawn_cancellation
        if shutdown_requested:
            await self._engine._memory_session_end()
        # ADR 0029 终结信号完整：过滤订阅（按 submission_id 收事件）收不到全局
        # shutdown（id 不匹配），必须逐个投 turn_failed{engine_shutdown}；队列里
        # 尚未出队的 submission 同样终结，否则订阅者永久挂死。
        await self._engine._terminate_orphan_submissions()
        # 通知所有 subscriber 退出：经统一投递路径，shutdown 也获全局 seq +
        # per-subscriber delivery_seq（保持两个序号在退出事件上同样连续可自检）。
        shutdown_ev = EventMsg(submission_id="*", msg=ShutdownMsg())
        shutdown_ev.seq = self._engine._seq
        self._engine._seq += 1
        for subscriber in list(self._engine._all_subs):
            self._engine._deliver(subscriber, shutdown_ev)
        # 此后再来的过滤订阅直接拿到合成终结（见 subscribe_envelopes），不留竞态窗口
        self._engine._closed = True
        if actor_cancellation is not None:
            raise actor_cancellation

    async def terminate_orphan_submissions(self) -> None:
        """Shutdown 收尾：对仍在订阅的与队列残留的 submission 投 engine_shutdown 终结。

        只发事件、**不动队列**：audit 模式的 durable accepted token 必须留在队列里
        供复活的 actor 继续处理（application cancel 契约）；legacy Submission 随
        engine 一起消亡，但本进程的订阅者仍需终结信号。
        """
        terminated: set[str] = set()
        for sid in list(self._engine._event_subs):
            terminated.add(sid)
            await self._engine._emit_operation_terminal(sid, None, kind="engine_shutdown")
        for sub in list(self._engine._submissions._queue):  # noqa: SLF001 —— 只读遍历，不出队
            if sub.id in terminated or isinstance(sub, AcceptedUserMessage):
                continue
            terminated.add(sub.id)
            await self._engine._emit_operation_terminal(sub.id, None, kind="engine_shutdown")
        self._engine._pending.clear()
