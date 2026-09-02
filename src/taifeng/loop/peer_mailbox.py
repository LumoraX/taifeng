"""PeerMailbox —— 谱系内点对点投递协作器（从 SpawnDriver 拆出，零行为变更）。

职责（peer-mailbox-messaging 契约的实现体）：
  - 寻址解析（thread_id / handle_id / "parent" → 目标 thread_id）
  - 双模式投递（QueueOnly / TriggerTurn；运行中投 pending_input、空闲落史、
    终态 spawn child 可被 TriggerTurn 唤醒续跑）
  - ``wait_peer`` 工具实现体（turn 内阻塞等待句柄终态）

设计（对应 spawn-module-structure 契约）：本类**无自有状态**，持 driver 引用，
经 ``driver._spawn_handles`` / ``driver._live_runners`` / ``driver._spawn_cancels``
访问 SpawnDriver 单一持有的运行态；engine 共享内部经 ``driver._engine`` 触达。
公共入口（deliver_peer_message / wait_spawn_terminal）由 SpawnDriver 同名
转发器暴露，engine 薄转发层与 tools 的 ``ctx.extras['spawn_coordinator']``
契约不变。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import ResponseItem
from taifeng.loop.event import (
    EventMsg,
    PeerAgentWoken,
    PeerMessageSent,
    PeerWaitAnyResolved,
    PeerWaitAnyStarted,
    PeerWaitResolved,
    PeerWaitStarted,
)

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.spawn_driver import SpawnDriver
    from taifeng.loop.spawn_handle import SpawnHandle

logger = logging.getLogger(__name__)


class PeerMailbox:
    """peer-mailbox 协作器：寻址 / 双模式投递 / TriggerTurn 唤醒 / wait_peer。

    无自有状态；运行态（句柄表 / live runner 表 / 取消 token 表）全部经
    driver 访问（状态单一持有，见 spawn-module-structure 契约）。
    """

    def __init__(self, driver: SpawnDriver) -> None:
        """
        Args:
            driver: 宿主 SpawnDriver —— 提供句柄表 / live runner 表 / 取消
                token 表与 engine 共享依赖（store / emit / snapshot 等）。
        """
        self._driver = driver

    def _resolve_peer_target(self, target: str, from_thread_id: str) -> str:
        """寻址解析：thread_id / handle_id / "parent" → 目标 thread_id。

        - ``parent``：解析为本谱系 root thread（spawn_skill 固定把
          ``parent_thread_id`` 记为 engine root，嵌套 spawn 同样收敛到 root）。
        - handle_id：经句柄表解析到 child_thread_id。
        - thread_id：root 或任一已登记 spawn child。
        未知目标显式 ``ValueError``（不静默丢弃）；跨 engine 寻址天然失败于此。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        if target == "parent":
            return str(eng._thread_id)  # noqa: SLF001
        if target == eng._thread_id:  # noqa: SLF001
            return target
        h = drv._spawn_handles.get(target)  # noqa: SLF001
        if h is not None:
            return h.child_thread_id
        for hh in drv._spawn_handles.handles.values():  # noqa: SLF001
            if hh.child_thread_id == target:
                return target
        raise ValueError(f"unknown_peer_target: {target}")

    def _peer_item(
        self, target_tid: str, text: str, from_thread_id: str
    ) -> ResponseItem:
        """渲染 peer 消息为中性 ResponseItem（user_message 形态，不新增 kind）。

        payload 显式标注 ``source="peer"`` 与 ``from_thread``，prompt 渲染层 /
        业务审计可区分 peer 消息与真实用户输入。
        """
        return ResponseItem(
            kind="user_message",
            thread_id=target_tid,
            payload={
                "text": text,
                "attachments": [],
                "source": "peer",
                "from_thread": from_thread_id,
            },
        )

    async def deliver_peer_message(
        self,
        *,
        target: str,
        text: str,
        mode: str = "queue_only",
        from_thread_id: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        """谱系内点对点投递（``send_message`` 工具与 ``SendToPeer`` Op 的唯一路径）。

        分支（spec「双模式投递」）：
          - 目标运行中（live runner 在册）→ 投其 ``pending_input``（B1 steering
            同一队列，下迭代边界 drain 并入）；TriggerTurn 此时降级 QueueOnly，
            返回值与事件标 ``mode_downgraded=True``。
          - 目标空闲 → ``store.append`` 即时落目标 thread 历史（R5 持久）；
            TriggerTurn 且目标为空闲（终态）spawn child → 再以续跑范式唤醒新
            detached turn（``_wake_peer_turn``）。
          - 目标 suspended → 仅落史不唤醒（挂起只能由 Resume 解除）。
          - 目标为 root → TriggerTurn 拒绝（root 由用户驱动）；QueueOnly 正常。

        Returns:
            ``{"target_thread_id", "delivered_via", "mode_downgraded", "woken"}``

        Raises:
            ValueError: 未知目标 / TriggerTurn 打 root。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        sender = from_thread_id or str(eng._thread_id)  # noqa: SLF001
        target_tid = self._resolve_peer_target(target, sender)
        is_root = target_tid == eng._thread_id  # noqa: SLF001
        if is_root and mode == "trigger_turn":
            raise ValueError(
                "trigger_turn_root_forbidden: root turn 由用户驱动，不可被 peer 唤醒"
            )
        item = self._peer_item(target_tid, text, sender)

        delivered_via = "history"
        mode_downgraded = False
        woken = False
        if is_root:
            # root：有活跃 turn → steering pending_input；否则落 engine 历史。
            pending = next(
                iter(reversed(list(eng._pending.values()))), None  # noqa: SLF001
            )
            if pending is not None:
                pending.pending_input.append(item)
                delivered_via = "pending_input"
            else:
                async with eng._lock:  # noqa: SLF001
                    eng._history.append(item)  # noqa: SLF001
                await eng._store.append(item)  # noqa: SLF001
        else:
            handle = next(
                h for h in drv._spawn_handles.handles.values()  # noqa: SLF001
                if h.child_thread_id == target_tid
            )
            live = drv._live_runners.get(target_tid)  # noqa: SLF001
            if live is not None and handle.status == "running":
                # 运行中：投 pending_input（TriggerTurn 在此降级，不打断采样）
                live.pending_input.append(item)
                delivered_via = "pending_input"
                mode_downgraded = mode == "trigger_turn"
            else:
                # 空闲 / 挂起：即时落子 thread 历史（R5）
                await eng._store.append(item)  # noqa: SLF001
                if mode == "trigger_turn" and handle.status != "suspended":
                    # 空闲（终态）spawn child → 续跑范式唤醒；suspended 只落史。
                    woken = await self._wake_peer_turn(handle, submission_id)

        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=submission_id or sender,
            msg=PeerMessageSent(data={
                "from": sender,
                "to": target_tid,
                "mode": mode,
                "delivered_via": delivered_via,
                "mode_downgraded": mode_downgraded,
                "text_len": len(text),
                "text_preview": text[:80],
            }),
        ))
        return {
            "target_thread_id": target_tid,
            "delivered_via": delivered_via,
            "mode_downgraded": mode_downgraded,
            "woken": woken,
        }

    async def _wake_peer_turn(
        self, handle: SpawnHandle, submission_id: str | None
    ) -> bool:
        """TriggerTurn 唤醒：以续跑范式给空闲 spawn child 起新 detached turn。

        消息已由调用方落史；此处重载子 thread 完整历史 → ``_build_child_runner``
        → 后台分离 task 跑至终态（``_finalize_spawn`` 回写句柄，再挂起可再
        resume）。K1 slot 重新占用（与首发 _drive_spawn 同语义，finally 释放）。
        emit ``peer_agent_woken``。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        target = eng._snapshot.get(handle.skill_id)  # noqa: SLF001
        if target is None:
            raise ValueError(f"peer_wake_skill_missing: {handle.skill_id}")
        await drv._await_root_cancel_ready()  # noqa: SLF001
        await eng._spawn_registry.reserve_manual()  # noqa: SLF001
        try:
            # 句柄回 running（_finalize_spawn 的终态幂等以此放行新收敛）
            drv._spawn_handles.set_result(  # noqa: SLF001
                handle.handle_id, status="running", result=None)
            drv._start_owned_task(  # noqa: SLF001
                self._drive_woken_turn(handle),
                name=f"peer-wake:{handle.handle_id}",
            )
        except Exception:
            eng._spawn_registry.release_manual()  # noqa: SLF001
            raise
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=submission_id or handle.handle_id,
            msg=PeerAgentWoken(data={
                "thread_id": handle.child_thread_id,
                "handle_id": handle.handle_id,
            }),
        ))
        return True

    async def _drive_woken_turn(self, handle: SpawnHandle) -> None:
        """被唤醒 turn 的后台驱动（镜像 ``_drive_spawn``：兜底 + finalize + 释放 K1）。"""
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        child_tid = handle.child_thread_id
        try:
            assert eng._root_cancel is not None  # noqa: SLF001
            cancel = eng._root_cancel.child(  # noqa: SLF001
                f"peer_wake:{handle.handle_id}")
            drv._spawn_cancels[handle.handle_id] = cancel  # noqa: SLF001
            history = await eng._load_thread_items(child_tid)  # noqa: SLF001
            target = eng._snapshot.get(handle.skill_id)  # noqa: SLF001
            if target is None:
                # snapshot 热更后 skill 消失:显式失败(外层 except 落 error 终态)
                raise RuntimeError(f"peer_wake_skill_missing: {handle.skill_id}")
            runner = eng._build_child_runner(  # noqa: SLF001
                target=target,
                child_thread_id=child_tid,
                seed=history[0],
                cancel=cancel,
                history=history,
                sample_scope_id=history[-1].id,
            )
            drv._live_runners[child_tid] = runner  # noqa: SLF001
            try:
                outcome = await runner.run()
            finally:
                drv._live_runners.pop(child_tid, None)  # noqa: SLF001
            await drv._finalize_spawn(  # noqa: SLF001
                handle.handle_id, child_tid, outcome)
        except Exception as e:  # noqa: BLE001
            logger.exception("peer wake driver crashed: %s", handle.handle_id)
            # 失败终态单点收敛（barrier 故障抑制为日志，不逃出后台 task）。
            await drv._settle_failed(  # noqa: SLF001
                handle.handle_id, str(e), suppress_barrier_errors=True)
        finally:
            eng._spawn_registry.release_manual()  # noqa: SLF001

    async def wait_spawn_terminal(
        self,
        *,
        handle_id: str,
        timeout_seconds: float,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """turn 内阻塞等待一个 spawn 句柄到终态（``wait_peer`` 工具的实现体）。

        轮询句柄表（50ms 粒度）；到终态返回 ``{outcome:"terminal", status,
        result}``；超时返回 ``{outcome:"timeout", status}``（turn 不失败）；
        取消令牌触发即沿既有取消路径中止（R4）。emit wait 两事件。

        Raises:
            ValueError: 未知 handle_id。
            asyncio.CancelledError: 等待期间被取消。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        handle = drv._spawn_handles.get(handle_id)  # noqa: SLF001
        if handle is None:
            raise ValueError(f"unknown_handle: {handle_id}")
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=handle_id,
            msg=PeerWaitStarted(data={
                "handle_id": handle_id,
                "timeout_seconds": timeout_seconds,
            }),
        ))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            cancel.raise_if_cancelled()
            if drv._spawn_handles.is_terminal(handle_id):  # noqa: SLF001
                h = drv._spawn_handles.get(handle_id)  # noqa: SLF001
                assert h is not None
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=handle_id,
                    msg=PeerWaitResolved(data={
                        "handle_id": handle_id,
                        "outcome": "terminal",
                        "status": h.status,
                    }),
                ))
                return {
                    "outcome": "terminal",
                    "status": h.status,
                    "result": h.result,
                }
            remaining = deadline - loop.time()
            if remaining <= 0:
                h = drv._spawn_handles.get(handle_id)  # noqa: SLF001
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=handle_id,
                    msg=PeerWaitResolved(data={
                        "handle_id": handle_id,
                        "outcome": "timeout",
                        "status": h.status if h else "unknown",
                    }),
                ))
                return {
                    "outcome": "timeout",
                    "status": h.status if h else "unknown",
                }
            await asyncio.sleep(min(0.05, remaining))

    async def wait_spawn_any(
        self,
        *,
        handle_ids: list[str],
        timeout_seconds: float,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """turn 内阻塞等一组 spawn 句柄,**任一**到终态即唤醒(``wait_any`` 实现体)。

        补齐等待原语的中间档:``wait_spawn_terminal`` 等**一个**、join-barrier 等
        **全部**,本方法等**任一**。缺这一档时,面对错峰完成的 N 个子任务只能盯死
        一个或等最慢的,想「谁先好先处理谁」只能让 LLM 轮询 join_skill——每轮烧一次
        采样,正是 IterationBudget 要防的空转。对标 codex ``wait_agent`` 的 any-of-N。

        **唤醒时收全**:返回当时**全部**已终态句柄,不是只返回第一个——同一轮询周期
        内多个完成时,调用方不必反复调用。

        ``suspended``(HITL 等待中)**不算**唤醒条件,与 wait_peer / barrier 的终态口径
        一致;挂起期的等待由 ``timeout_seconds`` 兜底。轮询粒度与取消语义同
        ``wait_spawn_terminal``(50ms;每圈 raise_if_cancelled,R4)。

        Args:
            handle_ids: 待等待的 spawn 句柄集(非空;每个必须已注册;重复项按发起序去重)。
            timeout_seconds: 最长等待秒数(必填);到期仍零终态 → outcome=timeout。
            cancel: 取消令牌,每轮询圈检查(R4)。

        Returns:
            ``{"outcome": "terminal"|"timeout", "settled": {hid: {status, result}},
            "pending": [hid, ...]}``。

        Raises:
            ValueError: handle_ids 为空(``empty_handle_ids``),或含未注册句柄
                (``unknown_spawn_handle``)。
            asyncio.CancelledError: 等待期间被取消。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        # 空集永不可能被满足,等下去必然只能超时 —— 调用方明确错误,显式抛不静默
        if not handle_ids:
            raise ValueError("empty_handle_ids")
        # 去重保序(dict 有序):返回值的 pending 顺序对调用方可预期
        ordered = list(dict.fromkeys(handle_ids))
        for hid in ordered:
            if drv._spawn_handles.get(hid) is None:  # noqa: SLF001
                raise ValueError(f"unknown_spawn_handle: {hid}")
        # submission_id 取等待方所在 thread:any-of-N 没有单一句柄能代表这次等待,
        # 用其中任意一个当轨道键会误导订阅方(wait_peer 用 handle_id 是因为只等它一个)。
        track = eng._thread_id  # noqa: SLF001
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=track,
            msg=PeerWaitAnyStarted(data={
                "handle_ids": ordered,
                "timeout_seconds": timeout_seconds,
            }),
        ))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            cancel.raise_if_cancelled()
            settled = self._collect_settled(ordered)
            if settled:
                return await self._resolve_wait_any(
                    track, ordered, settled, "terminal")
            remaining = deadline - loop.time()
            if remaining <= 0:
                return await self._resolve_wait_any(track, ordered, {}, "timeout")
            await asyncio.sleep(min(0.05, remaining))

    def _collect_settled(self, handle_ids: list[str]) -> dict[str, dict[str, Any]]:
        """收走句柄集中**当时全部**已终态的 {status, result}(纯读,不改状态)。

        Args:
            handle_ids: 已去重、已校验存在的句柄集。

        Returns:
            ``{hid: {"status", "result"}}``;无终态时为空 dict。
        """
        drv = self._driver
        settled: dict[str, dict[str, Any]] = {}
        for hid in handle_ids:
            if not drv._spawn_handles.is_terminal(hid):  # noqa: SLF001
                continue
            h = drv._spawn_handles.get(hid)  # noqa: SLF001
            # is_terminal 已保证句柄在册
            assert h is not None
            settled[hid] = {"status": h.status, "result": h.result}
        return settled

    async def _resolve_wait_any(
        self,
        track: str,
        ordered: list[str],
        settled: dict[str, dict[str, Any]],
        outcome: str,
    ) -> dict[str, Any]:
        """emit wait_any 收尾事件并组装返回值(terminal / timeout 共用一条出口)。

        Args:
            track: 轨道键(等待方 thread_id)。
            ordered: 去重保序的完整句柄集。
            settled: 已终态句柄的 {status, result};timeout 时为空。
            outcome: ``"terminal"`` 或 ``"timeout"``。

        Returns:
            工具层直接可序列化的结果 dict。
        """
        pending = [h for h in ordered if h not in settled]
        await self._driver._engine._emit(EventMsg(  # noqa: SLF001
            submission_id=track,
            msg=PeerWaitAnyResolved(data={
                "settled_ids": list(settled),
                "pending_ids": pending,
                "outcome": outcome,
            }),
        ))
        return {"outcome": outcome, "settled": settled, "pending": pending}
