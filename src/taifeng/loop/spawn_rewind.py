"""SpawnRewindChain —— detached spawn 子 thread 的 rewind 截断重推链。

职责(thread-addressable-rewind 契约实现体):
  - 活性守卫:unknown_thread / thread_running / turn_suspended /
    unknown_node / mode_kind_mismatch(禁状态白名单 —— error 终态与
    中断遗留 running 均放行,见 design D4)
  - 截断:``[rewind]`` marker(cut_index)append 到子 thread store
    (append-only 不删,R5;冷恢复经 reconstruct 重放幂等)
  - 重推:reconstruct 后的逻辑 history 截断 → ``_build_child_runner``
    重建 detached 子 runner → ``_finalize_spawn`` 单点收敛(回写句柄 +
    emit 终态 + barrier 幂等重查)

设计(对应 spawn-module-structure 契约):本类**无自有状态**,持 driver 引用,
经 ``driver._spawn_handles`` / ``driver._spawn_cancels`` / ``driver._live_runners``
/ ``driver._rewinding_threads`` 访问 SpawnDriver 单一持有的运行态;与
``SpawnResumeChain`` 同范式(spawn_resume.py),公共入口由 SpawnDriver
同名转发器暴露,engine 的 Rewind 路由调用点单一。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from taifeng.conversation.models import function_call, system_injection
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.loop.event import EventMsg, RewindRejected, TurnRewound
from taifeng.loop.rewind import derive_rewind_log
from taifeng.loop.submission import Rewind, Submission

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.spawn_driver import SpawnDriver
    from taifeng.loop.spawn_handle import SpawnHandle

logger = logging.getLogger(__name__)


class SpawnRewindChain:
    """spawn 子 thread 的 rewind 协作器:活性守卫 → 截断落 marker → 重推收敛。

    无自有状态;运行态(句柄表 / 取消 token 表 / live runner 表 / rewind
    在飞集)全部经 driver 访问(状态单一持有,见 spawn-module-structure 契约)。
    """

    def __init__(self, driver: SpawnDriver) -> None:
        """
        Args:
            driver: 宿主 SpawnDriver —— 提供运行态表、终态收敛点
                (_finalize_spawn / _settle_failed)与 engine 共享依赖。
        """
        self._driver = driver

    async def rewind_spawn(self, sub: Submission) -> None:
        """对某 spawn 子 thread 执行 rewind:守卫 → 截断 → 重推至终态。

        守卫按活性判定(design D4,禁状态白名单):
          1. thread_id 不属于任何 spawn 句柄 → ``unknown_thread``;
          2. 子 runner 热跑中 / 已有 rewind 在飞 → ``thread_running``;
          3. 子 thread 逻辑 history 有活跃挂起 → ``turn_suspended``(走 Resume);
          4. node_id 不在节点表 → ``unknown_node``;mode/kind 不相容 →
             ``mode_kind_mismatch``(与根路径同形)。

        放行集合:error / done / cancelled 终态、中断遗留 running(不在
        _live_runners)——全部是「无并发写者」的安全态。重推失败由宽 except
        兜底落 ``_settle_failed``(不静默、不卡死)。

        Args:
            sub: 本次 Rewind submission(事件归因 / Rejected emit)。
        """
        drv = self._driver
        op = sub.op
        assert isinstance(op, Rewind)
        assert op.thread_id is not None  # 路由方已判:仅子 thread 寻址进入本链
        child_tid = op.thread_id

        # 1. thread_id → spawn 句柄(唯一可寻址的子 thread 形态,v1 边界)
        handle = next(
            (h for h in drv._spawn_handles.handles.values()  # noqa: SLF001
             if h.child_thread_id == child_tid),
            None,
        )
        if handle is None:
            await self._reject(sub.id, op.node_id, "unknown_thread")
            return
        # 2. 活性守卫:热跑中(首发/续跑/peer 唤醒)或 rewind 在飞 → 拒绝
        if (
            child_tid in drv._live_runners  # noqa: SLF001
            or child_tid in drv._rewinding_threads  # noqa: SLF001
        ):
            await self._reject(sub.id, op.node_id, "thread_running")
            return
        # 在飞占位(并发双 Rewind 拒后到者);守卫通过后立即占,finally 释放
        drv._rewinding_threads.add(child_tid)  # noqa: SLF001
        try:
            await self._rewind_spawn_guarded(sub, op, handle)
        except Exception as e:  # noqa: BLE001
            logger.exception("spawn rewind crashed: %s", handle.handle_id)
            # 失败终态单点收敛(与 resume 链同范式:不静默、不卡 suspended)
            await drv._settle_failed(  # noqa: SLF001
                handle.handle_id, str(e), suppress_barrier_errors=True)
        finally:
            drv._rewinding_threads.discard(child_tid)  # noqa: SLF001

    async def _rewind_spawn_guarded(
        self, sub: Submission, op: Rewind, handle: SpawnHandle
    ) -> None:
        """rewind_spawn 主体(在飞占位后):剩余守卫 → 截断 → 重推。

        与 rewind_spawn 拆分仅为守卫 try/finally 作用域清晰;异常由调用方
        宽 except 兜底(落 handle error)。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        child_tid = handle.child_thread_id

        # 3. 重建逻辑 history(raw → reconstruct,禁直接 derive raw,design D3)
        raw = await eng._load_thread_items(child_tid)  # noqa: SLF001
        logical = reconstruct_logical_history(raw)
        # 活跃挂起守卫:挂起态 rewind 与 Resume 职责重叠,显式拒绝(对称根路径)
        if eng._find_active_suspension_in(logical) is not None:  # noqa: SLF001
            await self._reject(sub.id, op.node_id, "turn_suspended")
            return
        # 4. 节点定位 + mode/kind 相容(与根路径 _handle_rewind 同形)
        nodes = derive_rewind_log(logical)
        cp = next((c for c in nodes if c.node_id == op.node_id), None)
        if cp is None:
            await self._reject(sub.id, op.node_id, "unknown_node")
            return
        if op.mode == "retry_tool" and (
            cp.kind != "dispatch" or cp.inner_history_len is None
        ):
            await self._reject(sub.id, op.node_id, "mode_kind_mismatch")
            return

        # 5. 选截点 + 落 marker(append-only;cut_index 供冷恢复 reconstruct 重放)
        cut = (
            cp.inner_history_len
            if op.mode == "retry_tool" and cp.inner_history_len is not None
            else cp.history_len
        )
        marker = system_injection(
            f"[rewind] node={op.node_id} kind={cp.kind} mode={op.mode}",
            thread_id=child_tid, source="rewind",
            extra={"cut_index": cut},
        )
        await eng._store.append(marker)  # noqa: SLF001

        # 6. emit turn_rewound(R3;带 thread_id 与根路径区分)
        await eng._emit(EventMsg(submission_id=sub.id, msg=TurnRewound(data={  # noqa: SLF001
            "thread_id": child_tid, "node_id": op.node_id,
            "node_kind": cp.kind, "mode": op.mode, "cut_index": cut,
        })))

        # 7. 截断内存 buffer;retry_tool + new_args → 改写悬空 fc(只改内存,
        #    store 原样保留 append-only;改写经 marker 留痕,与根路径同语义)
        buffer = list(logical[:cut])
        if op.mode == "retry_tool" and op.new_args is not None and cp.call_id:
            self._rewrite_buffer_args(buffer, cp.call_id, op.new_args, child_tid)

        # 8. 重推(对齐 _resume_spawn_settled 尾段范式):句柄回 running →
        #    派生取消 token(kill_spawn 可达,R4)→ 重建 detached 子 runner
        drv._spawn_handles.set_result(  # noqa: SLF001
            handle.handle_id, status="running", result=None)
        target = eng._snapshot.get(handle.skill_id)  # noqa: SLF001
        if target is None:
            raise RuntimeError(f"spawn_rewind_skill_missing: {handle.skill_id}")
        assert eng._root_cancel is not None  # engine.run 已启动  # noqa: SLF001
        cancel = eng._root_cancel.child(  # noqa: SLF001
            f"spawn_rewind:{handle.handle_id}")
        drv._spawn_cancels[handle.handle_id] = cancel  # noqa: SLF001
        runner = eng._build_child_runner(  # noqa: SLF001
            target, child_tid, buffer[0], cancel, history=buffer)
        if op.mode == "retry_tool":
            # 采样前先补跑被保留的悬空 call(与根路径 seed 注入同法)
            runner._seed_pending_call_id = cp.call_id  # noqa: SLF001
        # R2:rewind 蓄意回退上下文 → 重推首采样的 cache 失效记为 expected
        runner._next_cache_break_expected = True  # noqa: SLF001
        runner._next_cache_break_reason = "rewind"  # noqa: SLF001
        drv._live_runners[child_tid] = runner  # peer-mailbox:重推期也可被投递  # noqa: SLF001
        try:
            outcome = await runner.run()
        finally:
            drv._live_runners.pop(child_tid, None)  # noqa: SLF001
        # 9. 与 _drive_spawn / resume_spawn 收尾一致:回写句柄 + emit 终态 +
        #    barrier 幂等重查(已 fired 的不二次触发)。
        await drv._finalize_spawn(handle.handle_id, child_tid, outcome)  # noqa: SLF001

    def _rewrite_buffer_args(
        self,
        buffer: list[ResponseItem],
        call_id: str,
        new_args: dict[str, object],
        thread_id: str,
    ) -> None:
        """retry_tool new_args:把内存 buffer 中该 call_id 的 fc 换成新 args。

        只改内存(自洽 + 供重跑读新参);store 保持 append-only(与根路径
        ``_rewrite_seed_args`` 同语义,作用对象换成局部 buffer)。
        """
        for i, item in enumerate(buffer):
            if (
                item.kind == "function_call"
                and item.payload.get("call_id") == call_id
            ):
                buffer[i] = function_call(
                    call_id=call_id, name=item.payload["name"],
                    arguments=json.dumps(new_args, ensure_ascii=False),
                    thread_id=thread_id,
                )

    async def _reject(self, submission_id: str, node_id: str, reason: str) -> None:
        """rewind 守卫失败统一出口(禁 silent fallback,显式发事件)。"""
        eng = self._driver._engine  # noqa: SLF001
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=submission_id,
            msg=RewindRejected(data={"node_id": node_id, "reason": reason}),
        ))
