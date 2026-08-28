"""SpawnResumeChain —— 挂起 detached spawn 的错峰续跑链（自 SpawnDriver 拆出，零行为变更）。

职责（detached-spawn 契约「各自独立 HITL — staggered Resume 路由」段实现体）：
  - 直接挂起续跑（resume_spawn / _resume_spawn_settled）：在 spawn 子 thread
    上核销 → 补 gap → 重建 detached 子 TurnRunner 续跑至终态
  - 嵌套挂起续跑（resume_spawn_nested）：CHILD_SKILL 内核态挂起 → 下探 leaf
    核销用户挂起 → 逐层回填 call_skill output → 重跑 spawn 根 turn

设计（对应 spawn-module-structure 契约）：本类**无自有状态**，持 driver 引用，
经 ``driver._spawn_handles`` / ``driver._spawn_cancels`` / ``driver._live_runners``
访问 SpawnDriver 单一持有的运行态；终态统一回 driver 的收敛点
（``_finalize_spawn`` / ``_settle_failed``）。公共入口（resume_spawn）由
SpawnDriver 同名转发器暴露，engine 的 Resume 路由调用点不变。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from taifeng.loop.event import (
    EventMsg,
    SuspensionPartiallyResolved,
    SuspensionResolved,
    SuspensionResolveRejected,
)
from taifeng.loop.submission import Resume, Submission

if TYPE_CHECKING:
    from taifeng.loop.spawn_driver import SpawnDriver
    from taifeng.loop.spawn_handle import SpawnHandle

logger = logging.getLogger(__name__)


class SpawnResumeChain:
    """错峰续跑协作器：直接挂起核销重跑 / 嵌套挂起下探回填链。

    无自有状态；运行态（句柄表 / 取消 token 表 / live runner 表）全部经
    driver 访问（状态单一持有，见 spawn-module-structure 契约）。
    """

    def __init__(self, driver: SpawnDriver) -> None:
        """
        Args:
            driver: 宿主 SpawnDriver —— 提供运行态表、终态收敛点
                （_finalize_spawn / _settle_failed）与 engine 共享依赖。
        """
        self._driver = driver

    async def resume_spawn(self, sub: Submission, handle: SpawnHandle) -> None:
        """续跑一个【挂起的 detached spawn 子 thread】至终态，独立回写句柄 + emit 终态。

        与 call_skill 子链 resume（_handle_child_resume）的根本差异：detached spawn 的
        父 turn 早已结束，不存在「父 call_skill 等回填」的续跑链 —— 子 thread 是独立根
        turn。因此**直接挂起**（DATA/FORM/permission 落在 spawn 子 thread 自身）只做三步：
          1. 在【子 thread 的 load_thread 历史】上找活跃挂起，核销 resolutions、补 gap
             （复用 SuspensionResolver + _apply_plan_on_thread，与 leaf resume 同语义；
             request 级核销:子集合法,全量达成才落 marker / 重跑）。
          2. 以补齐后的子 thread 历史重建 detached 子 TurnRunner（_build_child_runner，
             call_stack 仍空 → 仍是独立根 turn），续跑至终态。
          3. _finalize_spawn 回写句柄 + emit SpawnCompleted/SpawnSuspended/SpawnFailed
             —— 与 _drive_spawn 收尾完全一致：再次挂起则 handle 重新标 suspended（可再
             resume），从而支持多轮错峰 HITL。

        **嵌套挂起**（被 spawn 的 composite 子任务其子 skill 在执行中 request_user_input
        挂起 → spawn 子 thread 自身的活跃挂起 reason==CHILD_SKILL，内核内部态、非用户可直
        接 resolve）：转 ``resume_spawn_nested`` 走「以 spawn 子 thread 为根的续跑链」——
        下探 leaf 核销用户挂起、逐层回填父 call_skill output，再重跑 spawn 子 thread +
        finalize（详见该方法）。

        兜底：任何意外异常落 handle error + emit SpawnFailed（不静默、不卡 suspended）。
        K1 slot 不在此再占/再放 —— detached slot 由首发 _drive_spawn 的 finally 持有到
        本句柄真正离开运行态；本续跑只是同一句柄生命周期内的再驱动，不重复占用配额。

        Args:
            sub: 本次 Resume submission（事件归因 / Rejected emit）。
            handle: 命中的挂起 spawn 句柄（match_suspended_spawn 已校验 status）。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        assert isinstance(sub.op, Resume)
        child_tid = handle.child_thread_id
        try:
            # 1. 子 thread 上找活跃挂起 → 核销 resolutions → 补 gap（落 store）
            items = await eng._load_thread_items(child_tid)  # noqa: SLF001
            record = eng._find_active_suspension_in(items)  # noqa: SLF001
            if record is None:
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=sub.id,
                    msg=SuspensionResolveRejected(data={
                        "reason": "no_active_suspension",
                        "record_id": None, "detail": {}}),
                ))
                return
            # 在飞守卫(suspend-review-fixes,与根/leaf 路径同形):并发双 Resume
            # 拒后到者,TTL fire 对在飞 record 让位——异步 store(协议支持的业务
            # DB 实现)下的双结算窗口(双 fco / 双 marker / 双重跑)在此闭合。
            if record.record_id in eng._resolving_records:  # noqa: SLF001
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=sub.id,
                    msg=SuspensionResolveRejected(data={
                        "reason": "resolve_in_flight",
                        "record_id": record.record_id, "detail": {}}),
                ))
                return
            eng._resolving_records.add(record.record_id)  # noqa: SLF001
            try:
                await self._resume_spawn_settled(sub, handle, record)
            finally:
                eng._resolving_records.discard(record.record_id)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "detached spawn resume crashed: %s", handle.handle_id)
            # 失败终态单点收敛（barrier 故障抑制为日志，不覆盖原始异常路径）。
            await drv._settle_failed(  # noqa: SLF001
                handle.handle_id, str(e), suppress_barrier_errors=True)

    async def _resume_spawn_settled(
        self, sub: Submission, handle: SpawnHandle, record: Any
    ) -> None:
        """resume_spawn 的主体(在飞守卫占位后):嵌套分流 / 配对 / 结算 / 重跑。

        与 resume_spawn 拆分仅为守卫 try/finally 的作用域清晰;异常仍由调用方的
        宽 except 兜底(落 handle error,不静默不卡 suspended)。
        """
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver

        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        assert isinstance(sub.op, Resume)
        op = sub.op
        child_tid = handle.child_thread_id
        # 嵌套错峰 HITL：spawn 子 thread 的活跃挂起是 CHILD_SKILL（其子 skill 在执行中
        # request_user_input 挂起 → 本层随之内核态挂起）。用户可答的 DATA/FORM 挂起埋在
        # 更深的 leaf 子 thread，本层 record 不能直接交 SuspensionResolver（reason=
        # child_skill 非用户可 resolve）→ 转嵌套续跑链。
        if eng._next_child_link(record) is not None:  # noqa: SLF001
            await self.resume_spawn_nested(sub, handle)
            return
        # 到期哨兵与未核销 pending 求交;空 → 让位(已被并发人工核销)
        resolutions = eng._effective_resolutions(  # noqa: SLF001
            record, await eng._load_thread_items(child_tid),  # noqa: SLF001
            op.resolutions)
        if not resolutions:
            return
        try:
            plan = SuspensionResolver().plan(record, resolutions)
        except ResolveError as e:
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=sub.id,
                msg=SuspensionResolveRejected(data={
                    "reason": str(e),
                    "record_id": record.record_id, "detail": {}}),
            ))
            return
        # 会话级副作用(K2 增额)+ 谱系计数透传(suspend-review-fixes:spawn
        # 重跑不再丢计数 → failure_suspend_max_auto_retries 对 spawn 拓扑生效)
        auto_retries = eng._apply_plan_session_effects(plan, record)  # noqa: SLF001
        # 补 gap（复用与 leaf resume 同一机制；marker 由全量达成判定后签发）
        await eng._apply_plan_on_thread(  # noqa: SLF001
            child_tid, handle.skill_id, record, plan)
        # request 级核销:仍有未核销 pending → 部分核销,句柄保持 suspended
        items_after = await eng._load_thread_items(child_tid)  # noqa: SLF001
        remaining = [
            pend for pend in eng._unsettled_pendings(  # noqa: SLF001
                record, items_after)
            if pend.request_id not in resolutions]
        if remaining:
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=sub.id,
                msg=SuspensionPartiallyResolved(data={
                    "record_id": record.record_id, "thread_id": child_tid,
                    "resolved_request_ids": sorted(resolutions.keys()),
                    "remaining_request_ids": sorted(
                        pend.request_id for pend in remaining)}),
            ))
            return
        await eng._append_resolved_marker(  # noqa: SLF001
            child_tid, record.record_id)
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=sub.id,
            msg=SuspensionResolved(data={
                "record_id": record.record_id,
                "request_ids": sorted(record.request_ids())}),
        ))

        # 句柄回到 running（子 turn 又要跑了）；abort 则不续跑，直接落终态。
        drv._spawn_handles.set_result(  # noqa: SLF001
            handle.handle_id, status="running", result=None)
        if plan.abort:
            # abort 裁决（TTL 到期 / 人工）：子 turn 在挂起点终止 → 失败终态
            # 单点收敛（含 join-barrier 重查——否则被等待的句柄虽落终态但
            # barrier 永不触发，聚合 turn 挂死）。
            await drv._settle_failed(  # noqa: SLF001
                handle.handle_id, f"spawn_aborted: {record.record_id}")
            return

        # 2. 以补齐后的子 thread 历史重建 detached 子 TurnRunner，续跑至终态。
        target = eng._snapshot.get(handle.skill_id)  # noqa: SLF001
        if target is None:
            raise RuntimeError(
                f"spawn_resume_skill_missing: {handle.skill_id}")
        assert eng._root_cancel is not None  # engine.run 已启动  # noqa: SLF001
        cancel = eng._root_cancel.child(  # noqa: SLF001
            f"spawn_resume:{handle.handle_id}")
        # 续跑期间也登记取消 token：覆盖首发遗留的旧 token，使 kill_spawn 取消
        # 的是当前正在跑的续跑子树（而非已结束的首发子树）。
        drv._spawn_cancels[handle.handle_id] = cancel  # noqa: SLF001
        resumed_history = await eng._load_thread_items(child_tid)  # noqa: SLF001
        runner = eng._build_child_runner(  # noqa: SLF001
            target, child_tid, resumed_history[0], cancel,
            history=resumed_history,
            auto_retry_count=auto_retries,
            sample_scope_id=sub.id,
        )
        drv._live_runners[child_tid] = runner  # peer-mailbox：续跑期也可被投递  # noqa: SLF001
        try:
            outcome = await runner.run()
        finally:
            drv._live_runners.pop(child_tid, None)  # noqa: SLF001
        # 3. 与 _drive_spawn 收尾一致：回写句柄 + emit 终态（再挂起 → 可再 resume）。
        await drv._finalize_spawn(handle.handle_id, child_tid, outcome)  # noqa: SLF001

    async def resume_spawn_nested(
        self, sub: Submission, handle: SpawnHandle
    ) -> None:
        """续跑【嵌套挂起】的 detached spawn：被 spawn 的 composite 子任务其子 skill 挂起，
        spawn 子 thread 以 CHILD_SKILL 内核态挂起 —— 走「以 spawn 子 thread 为根的续跑链」。

        机制（对账 ``_handle_child_resume``，但根换成 spawn 子 thread、且根层重跑走 spawn
        驱动而非 engine 根续跑）：
          1. ``_build_spawn_resume_chain``：自 spawn 子 thread 沿 CHILD_SKILL pending 下探到
             持有用户 DATA/FORM 挂起的 leaf，得 ``[(spawn子tid, 子skill, None), …, (leaf, …)]``。
          2. ``_resume_leaf_thread``：用用户 resolutions 核销 leaf 真实挂起 + 续跑 leaf turn，
             拿回传父的结果串。leaf 又挂起 / 核销被拒 → 已各自 emit，句柄保持 suspended、不动。
          3. 中间各父层（若链 > 2）：``_resume_parent_level`` 逐层回填 call_skill output + 续跑。
          4. spawn 子 thread（链根）：``_settle_call_skill_output`` 回填其 call_skill output +
             核销其 CHILD_SKILL 挂起 → 句柄回 running → ``_build_child_runner`` 重跑（保持
             detached 独立根语义）→ ``_finalize_spawn`` 回写句柄 + emit 终态（再挂起 → 可再
             resume，支持多轮错峰 HITL）。

        归因：第 2、3 步续跑事件显式归到 **spawn 子 thread submission**（非本次 Resume sub.id），
        与首发一致 —— 业务侧按 submission_id 分轨，否则 leaf 子步文本会错挂到 Resume 轨（丢轨）。

        兜底：任何意外异常落 handle error + emit SpawnFailed（不静默、不卡 suspended）。
        """
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        assert isinstance(sub.op, Resume)
        op = sub.op
        child_tid = handle.child_thread_id
        try:
            # 1. 自 spawn 子 thread 为根，沿 CHILD_SKILL pending 下探到 leaf
            chain = await eng._build_spawn_resume_chain(  # noqa: SLF001
                child_tid, handle.skill_id, op.resolutions)
            if chain is None or len(chain) < 2:
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=sub.id,
                    msg=SuspensionResolveRejected(data={
                        "reason": "no_active_suspension",
                        "record_id": None, "detail": {}}),
                ))
                return

            assert eng._root_cancel is not None  # engine.run 已启动  # noqa: SLF001
            cancel = eng._root_cancel.child(  # noqa: SLF001
                f"spawn_resume:{handle.handle_id}")
            # 续跑期间登记取消 token：覆盖首发遗留旧 token（kill 命中当前续跑子树）。
            drv._spawn_cancels[handle.handle_id] = cancel  # noqa: SLF001

            # 2. leaf：核销用户挂起 + 续跑（事件归 spawn 子 thread submission，保持分轨一致）
            leaf_tid, leaf_skill, _ = chain[-1]
            child_result = await eng._resume_leaf_thread(  # noqa: SLF001
                sub, leaf_tid, leaf_skill, op.resolutions, cancel,
                submission_id=child_tid)
            if child_result is None:
                # leaf 核销被拒（已 emit Rejected）或 leaf 又挂起（已 emit turn_suspended）
                # → 句柄保持 suspended，等下一轮 Resume，不动状态、不上溯。
                return

            # 3. 中间父层（链 > 2 时）：逐层回填 call_skill output + 续跑父 turn，直到 spawn 子层
            for level in range(len(chain) - 2, 0, -1):
                parent_tid, parent_skill, _ = chain[level]
                call_id = chain[level + 1][2]  # 本层指向下一层的 call_skill call_id
                cont = await eng._resume_parent_level(  # noqa: SLF001
                    sub, parent_tid, parent_skill, call_id, child_result, cancel,
                    submission_id=child_tid)
                if cont is None:
                    return  # 中途某层又挂起 → 链中止，句柄保持 suspended
                child_result = cont

            # 4. spawn 子 thread（链根）：回填其 call_skill output（指向 chain[1]）+ 核销本层
            #    CHILD_SKILL 挂起 → 句柄回 running → 重跑 detached 根 turn → finalize。
            root_call_id = chain[1][2]  # spawn 子 thread 内指向下一层的 call_skill call_id
            if root_call_id is None:
                # CHILD_SKILL pending 缺 related_call_id（坏数据）→ 无法回填，显式拒绝。
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=sub.id,
                    msg=SuspensionResolveRejected(data={
                        "reason": "child_skill_missing_call_id",
                        "record_id": None, "detail": {}}),
                ))
                return
            settle = await eng._settle_call_skill_output(  # noqa: SLF001
                sub, child_tid, root_call_id, child_result)
            if settle == "missing":
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=sub.id,
                    msg=SuspensionResolveRejected(data={
                        "reason": "no_active_suspension",
                        "record_id": None, "detail": {}}),
                ))
                return
            if settle == "partial":
                # spawn 子层 record 还有其他挂起子未结(parallel 多子错峰):
                # 句柄保持 suspended,等后续 Resume 结清再重跑
                return
            drv._spawn_handles.set_result(  # noqa: SLF001
                handle.handle_id, status="running", result=None)
            target = eng._snapshot.get(handle.skill_id)  # noqa: SLF001
            if target is None:
                raise RuntimeError(f"spawn_resume_skill_missing: {handle.skill_id}")
            resumed_history = await eng._load_thread_items(child_tid)  # noqa: SLF001
            runner = eng._build_child_runner(  # noqa: SLF001
                target, child_tid, resumed_history[0], cancel,
                history=resumed_history, sample_scope_id=sub.id)
            drv._live_runners[child_tid] = runner  # peer-mailbox：续跑期也可被投递  # noqa: SLF001
            try:
                outcome = await runner.run()
            finally:
                drv._live_runners.pop(child_tid, None)  # noqa: SLF001
            # 与 _drive_spawn / resume_spawn 收尾一致：回写句柄 + emit 终态 + 检查 join-barrier。
            await drv._finalize_spawn(  # noqa: SLF001
                handle.handle_id, child_tid, outcome)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "detached spawn nested resume crashed: %s", handle.handle_id)
            # 失败终态单点收敛（barrier 故障抑制为日志，不覆盖原始异常路径）。
            await drv._settle_failed(  # noqa: SLF001
                handle.handle_id, str(e), suppress_barrier_errors=True)
