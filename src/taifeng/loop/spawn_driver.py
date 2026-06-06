"""SpawnDriver —— detached-spawn 协调器（从 AgentEngine 抽出，零行为变更）。

职责（与 call_skill 阻塞式派发互补）：
  - 句柄登记 / 状态查询 / 主动终止（spawn_skill / spawn_status / kill_spawn）
  - 后台分离驱动 + 收尾回写（_drive_spawn / _finalize_spawn）
  - 挂起 detached spawn 的错峰 resume（_match_suspended_spawn / resume_spawn）
  - join-barrier 登记 + 触发（set_join_barrier / _check_barriers / _fire_barrier）
  - 冷恢复重建句柄表 / barrier / 守卫集（rebuild_from_history / _infer_spawn_status_from_child）

设计：本类**持有 engine 引用**，复用其 store / emit / snapshot / lock / history /
_root_cancel / _spawn_registry(K1，与 call_skill 共享) / _build_child_runner /
_load_thread_items / _find_active_suspension_in / _apply_plan_on_thread 等内部，
不复制这些共享状态。AgentEngine 仅保留薄转发器（spawn_skill / spawn_status /
kill_spawn / has_live_spawns / set_join_barrier 等公共 API），保证现有调用点与
tools 的 ``ctx.extras['spawn_coordinator']`` 契约不变。

参照：codex / claw-code 的 detached task 范式（只学范式，不抄代码）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import (
    ResponseItem,
    user_message,
)
from taifeng.loop.event import (
    EventMsg,
    JoinBarrierFired,
    JoinBarrierRegistered,
    SpawnCancelled,
    SpawnCompleted,
    SpawnFailed,
    SpawnStarted,
    SpawnSuspended,
    SuspensionResolved,
    SuspensionResolveRejected,
)
from taifeng.loop.spawn_handle import JoinBarrier, SpawnHandle, SpawnHandleRegistry
from taifeng.loop.submission import Resume, Submission

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine

logger = logging.getLogger(__name__)


class SpawnDriver:
    """detached-spawn 协调器：句柄 / 分离驱动 / 错峰 resume / join-barrier / 冷恢复。

    持有 engine 引用，复用其 store / emit / snapshot / _build_child_runner /
    _root_cancel / _spawn_registry(K1) 等内部状态。本类自持 detached 专属运行态：
    句柄登记表、kill 取消 token 表、barrier 进程内幂等守卫集。
    """

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供 store / emit / snapshot / lock / history /
                _root_cancel / _spawn_registry / _build_child_runner 等共享依赖。
        """
        self._engine = engine
        # detached-spawn：分离式 spawn 的句柄登记表（≠ K1 的 SpawnSlotRegistry 配额表）。
        # 句柄记 handle_id ↔ child_thread_id ↔ 终态/结果；后台分离 task 跑完后回写。
        self._spawn_handles = SpawnHandleRegistry()
        # detached-spawn kill 支持：handle_id → 该 spawn 的取消子 token（从 _root_cancel
        # 派生）。kill_spawn 据此**只取消单个** spawn 子树，不波及兄弟 spawn / 父 turn。
        # 由 _drive_spawn / resume_spawn 在派生 token 时登记；spawn 收尾时不强制清理
        # （终态句柄不会被再 kill，留着无副作用，且便于幂等 no-op）。
        self._spawn_cancels: dict[str, CancellationToken] = {}
        # join-barrier 进程内幂等守卫:已触发过的 barrier_id 集合。保证每个 barrier
        # 在本进程内**至多触发一次**(配合 parent thread 落 join_barrier_fired 标记,
        # 冷恢复重建时也不重复起聚合 turn)。
        self._fired_barriers: set[str] = set()

    # -----------------------------------------------------------------
    # detached-spawn：分离式发起子 skill（立即返回句柄，后台独立跑完）
    # -----------------------------------------------------------------

    async def spawn_skill(
        self, *, skill_id: str, args: dict[str, Any], reason: str
    ) -> dict[str, str]:
        """分离式发起一个子 skill：立即返回句柄，子 skill 在后台分离 task 跑完。

        与 ``call_skill`` 阻塞父 turn 不同，本方法把子 skill 作为独立 child thread
        上的 detached ``asyncio.create_task`` 跑（非阻塞），登记句柄后立刻返回。
        后台 task（``_drive_spawn``）跑完后回写句柄状态并 emit 终态事件。

        准入门控与 ``call_skill`` 一致（除 reject 分类细化留待后续 task）：
          1. 目标 skill 必须存在（unknown_skill → ValueError）
          2. ``DispatchPolicy.check``（深度 / 环 / 白名单 / 不可调 entry）→ 拒绝即抛错
          3. K1 spawn 配额预留（``SpawnSlotRegistry`` 超限 → SpawnLimitError 上抛）

        Args:
            skill_id: 要分离发起的子 skill id（须在 entry skill 的 child_skills 白名单内）。
            args: 子 skill 的种子输入（序列化为子 thread 首条 user_message）。
            reason: LLM / 业务自陈的发起理由（透传到事件 / 审计，taifeng 不解析语义）。

        Returns:
            ``{"handle_id": ..., "child_thread_id": ...}`` —— 立即可用于 ``spawn_status``。

        Raises:
            ValueError: 目标 skill 不存在。
            DispatchRejectedError 语义：派发被策略拒绝（此处直接抛 ValueError 带 reason）。
            SpawnLimitError: K1 spawn 配额超限。
            RuntimeError: engine.run 尚未启动（根取消 token 未就绪）。
        """
        import json
        import secrets

        eng = self._engine

        # 1. 目标 skill 必须存在
        target = eng._snapshot.get(skill_id)  # noqa: SLF001
        if target is None:
            raise ValueError(f"unknown_skill: {skill_id}")

        # 2. DispatchPolicy 门控：以 entry skill 为唯一栈帧的调用栈做派发裁决
        from taifeng.skill.dispatch import CallStack

        stack = CallStack().push(
            skill_id=eng._entry_skill.id,  # noqa: SLF001
            call_id=f"spawn_entry_{secrets.token_hex(4)}",
        )
        verdict = eng._dispatch_policy.check(  # noqa: SLF001
            stack, eng._entry_skill, target  # noqa: SLF001
        )
        if not verdict.allowed:
            raise ValueError(f"dispatch_rejected: {verdict.reason}")

        # engine.run 由 pool 以 create_task 后台启动，``_root_cancel`` 在其首行赋值。
        # spawn_skill 紧随 get_or_create 调用时可能 run() 尚未被调度 → 短暂让步等待
        # 根取消 token 就绪（R4：分离 task 必须挂在根取消树上，不可凭空造游离 token）。
        for _ in range(100):
            if eng._root_cancel is not None:  # noqa: SLF001
                break
            await asyncio.sleep(0)
        if eng._root_cancel is None:  # noqa: SLF001
            raise RuntimeError("engine not running: root cancel token unavailable")

        # 3. K1 spawn 配额预留（贯穿整棵 turn 树）。注意：detached 语义下不能用
        #    ``async with reserve()``（那会在本方法返回时即释放 slot，而子 task 还在跑）；
        #    必须手动 reserve（占用）+ 在 _drive_spawn 收尾时释放（见 finally）。
        await eng._spawn_registry.reserve_manual()  # noqa: SLF001

        # C2 修复：reserve_manual 之后、create_task 之前的所有步骤若抛出，
        # _drive_spawn 永远不会启动（其 finally 不会执行），K1 槽位会永久泄漏。
        # 用 try/except 兜住预启动阶段：任何失败都先释放槽位再重抛。
        # 一旦 create_task 成功，子 task 的 finally 负责释放，本路径不再释放。
        try:
            # 4. 建 child thread + 落种子 user_message（与 turn.py::_spawn_sub_runner 对账）
            handle_id = f"sp_{secrets.token_hex(4)}"
            child_thread_id = await eng._store.create_thread(  # noqa: SLF001
                cwd=None,
                entry_skill_id=skill_id,
                source=f"spawn:{eng._entry_skill.id}",  # noqa: SLF001
                extra={
                    "parent_thread_id": eng._thread_id,  # noqa: SLF001
                    "spawn_handle_id": handle_id,
                    "reason": reason,
                },
            )
            # C1 修复：seed 只创建一次，此处落盘并传递给 _drive_spawn。
            # _drive_spawn 不再重建 seed（那会产生新 id，导致 store 里的 id 与
            # 内存中 history_buffer[0].id 不一致，冷恢复时会重建出不同的消息图谱）。
            seed = user_message(
                json.dumps(args, ensure_ascii=False), thread_id=child_thread_id
            )
            await eng._store.append(seed)  # noqa: SLF001

            # 5. 登记句柄 + 在 parent thread 落 spawn 锚（冷恢复可重建 registry）+ emit
            self._spawn_handles.register(
                handle_id=handle_id, skill_id=skill_id, child_thread_id=child_thread_id
            )
            from taifeng.conversation.models import spawn_item

            anchor = spawn_item(
                handle_id=handle_id,
                skill_id=skill_id,
                child_thread_id=child_thread_id,
                thread_id=eng._thread_id,  # noqa: SLF001
            )
            async with eng._lock:  # noqa: SLF001
                eng._history.append(anchor)  # noqa: SLF001
            await eng._store.append(anchor)  # noqa: SLF001
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle_id,
                msg=SpawnStarted(data={
                    "handle_id": handle_id,
                    "skill_id": skill_id,
                    "child_thread_id": child_thread_id,
                }),
            ))

            # 6. 分离 task：后台独立跑子 skill turn（非阻塞），跑完回写句柄 + emit 终态。
            #    seed 传入 _drive_spawn，确保 history_buffer[0] 与 store 里的同一对象。
            asyncio.create_task(
                self._drive_spawn(handle_id, target, child_thread_id, seed)
            )
        except Exception:
            # 预启动失败：子 task 未启动，释放 K1 槽位后重抛。
            eng._spawn_registry.release_manual()  # noqa: SLF001
            raise

        return {"handle_id": handle_id, "child_thread_id": child_thread_id}

    async def _drive_spawn(
        self,
        handle_id: str,
        target: Any,
        child_thread_id: str,
        seed: ResponseItem,
    ) -> None:
        """后台分离 task：跑子 skill turn 至完成/挂起/失败，回写句柄 + emit 终态。

        外层宽 except 兜底：任何意外异常都把句柄落 error + emit SpawnFailed，
        绝不让句柄卡在 running（也不静默吞错——记日志 + emit）。收尾必释放 K1 slot。

        C1：seed 由 spawn_skill 构造并落盘后传入，此处不重建——保证 store 与
        history_buffer[0] 使用完全相同的对象（相同 id），冷恢复时不会重建出不同图谱。

        Args:
            handle_id: 本次 spawn 的句柄 id。
            target: 子 skill 定义。
            child_thread_id: 子 thread id（句柄已登记的引用）。
            seed: 已落盘的种子 user_message（由 spawn_skill 构造并 append 到 store）。
        """
        eng = self._engine
        try:
            assert eng._root_cancel is not None  # spawn_skill 已校验  # noqa: SLF001
            cancel = eng._root_cancel.child(f"spawn:{handle_id}")  # noqa: SLF001
            # 登记本 spawn 的取消 token，供 kill_spawn 精确取消单个 spawn 子树。
            self._spawn_cancels[handle_id] = cancel
            runner = eng._build_child_runner(  # noqa: SLF001
                target, child_thread_id, seed, cancel
            )
            outcome = await runner.run()
            await self._finalize_spawn(handle_id, child_thread_id, outcome)
        except Exception as e:  # noqa: BLE001
            # 兜底：不让句柄卡死在 running。记日志（不静默）+ 落 error + emit。
            logger.exception("detached spawn driver crashed: %s", handle_id)
            self._spawn_handles.set_result(
                handle_id, status="error", result=str(e)
            )
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle_id,
                msg=SpawnFailed(data={"handle_id": handle_id, "error": str(e)}),
            ))
        finally:
            # K1：detached 语义下手动占用的 slot 在子 task 收尾时释放。
            eng._spawn_registry.release_manual()  # noqa: SLF001

    async def _finalize_spawn(
        self, handle_id: str, child_thread_id: str, outcome: Any
    ) -> None:
        """按子 turn 的 end_reason 回写句柄状态并 emit 对应终态事件。

        - completed → done + SpawnCompleted(result=final_text)
        - suspended → suspended + SpawnSuspended（resume 路由留待后续 task）
        - cancelled → cancelled + SpawnCancelled
        - 其余（error / 未知）→ error + SpawnFailed
        """
        eng = self._engine
        end = outcome.end_reason
        if end == "completed":
            self._spawn_handles.set_result(
                handle_id, status="done", result=outcome.final_text
            )
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle_id,
                msg=SpawnCompleted(data={
                    "handle_id": handle_id, "result": outcome.final_text,
                }),
            ))
        elif end == "suspended":
            # 子 thread 内已落 SuspensionRecord 并 emit turn_suspended；句柄标 suspended。
            # Resume 路由（据 child_thread_id 续跑）留待后续 task。
            self._spawn_handles.set_result(
                handle_id, status="suspended", result=None
            )
            pending = (
                outcome.suspension.to_item().payload["pending"]
                if outcome.suspension is not None
                else []
            )
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle_id,
                msg=SpawnSuspended(data={
                    "handle_id": handle_id,
                    "thread_id": child_thread_id,
                    "pending": pending,
                }),
            ))
        elif end == "cancelled":
            self._spawn_handles.set_result(
                handle_id, status="cancelled", result=outcome.error
            )
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle_id,
                msg=SpawnCancelled(data={"handle_id": handle_id}),
            ))
        else:
            # error / max_iterations / resource_limit 等非成功终态 → error
            err = outcome.error or end
            self._spawn_handles.set_result(
                handle_id, status="error", result=err
            )
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle_id,
                msg=SpawnFailed(data={"handle_id": handle_id, "error": err}),
            ))
        # join-barrier:本 spawn 进入终态(含 suspended——但 suspended 非终态,
        # all_terminal 不满足 → 不触发),检查是否凑齐某 barrier 的全终态条件。
        await self._check_barriers(handle_id)

    def match_suspended_spawn(self, thread_id: str) -> SpawnHandle | None:
        """若 thread_id 命中某个【当前挂起】的 detached spawn 句柄，返回该句柄。

        仅匹配 status=='suspended' 的句柄：running 的句柄子 turn 还在跑（无挂起可续），
        终态句柄已结束（不可再 resume）。命中即把 Resume 路由到 resume_spawn。
        无命中（根 thread / call_skill 子链 / 未挂起）→ None，交回既有 resume 路径。

        Args:
            thread_id: Resume.thread_id（业务侧从 SpawnSuspended.thread_id 拿到）。
        Returns:
            命中的挂起句柄；无命中 → None。
        """
        for h in self._spawn_handles.handles.values():
            if h.child_thread_id == thread_id and h.status == "suspended":
                return h
        return None

    async def resume_spawn(self, sub: Submission, handle: SpawnHandle) -> None:
        """续跑一个【挂起的 detached spawn 子 thread】至终态，独立回写句柄 + emit 终态。

        与 call_skill 子链 resume（_handle_child_resume）的根本差异：detached spawn 的
        父 turn 早已结束，不存在「父 call_skill 等回填」的续跑链 —— 子 thread 是独立根
        turn。因此本路径只做三步，不上溯父：
          1. 在【子 thread 的 load_thread 历史】上找活跃挂起，核销 resolutions、补 gap
             （复用 SuspensionResolver + _apply_plan_on_thread，与 leaf resume 同语义；
             SuspensionResolver 已强制全量 resume，杜绝部分 resume 违例）。
          2. 以补齐后的子 thread 历史重建 detached 子 TurnRunner（_build_child_runner，
             call_stack 仍空 → 仍是独立根 turn），续跑至终态。
          3. _finalize_spawn 回写句柄 + emit SpawnCompleted/SpawnSuspended/SpawnFailed
             —— 与 _drive_spawn 收尾完全一致：再次挂起则 handle 重新标 suspended（可再
             resume），从而支持多轮错峰 HITL。

        兜底：任何意外异常落 handle error + emit SpawnFailed（不静默、不卡 suspended）。
        K1 slot 不在此再占/再放 —— detached slot 由首发 _drive_spawn 的 finally 持有到
        本句柄真正离开运行态；本续跑只是同一句柄生命周期内的再驱动，不重复占用配额。

        Args:
            sub: 本次 Resume submission（事件归因 / Rejected emit）。
            handle: 命中的挂起 spawn 句柄（match_suspended_spawn 已校验 status）。
        """
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver

        eng = self._engine
        assert isinstance(sub.op, Resume)
        op = sub.op
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
            try:
                # SuspensionResolver.plan 强制全量配齐该 record 的全部 pending，
                # 任何部分 resume / 未知 request_id → ResolveError（禁静默兜底）。
                plan = SuspensionResolver().plan(record, op.resolutions)
            except ResolveError as e:
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=sub.id,
                    msg=SuspensionResolveRejected(data={
                        "reason": str(e),
                        "record_id": record.record_id, "detail": {}}),
                ))
                return
            # 补 gap + 落 resolved-marker 核销本 record（复用与 leaf resume 同一机制）
            await eng._apply_plan_on_thread(  # noqa: SLF001
                child_tid, handle.skill_id, record, plan)
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=sub.id,
                msg=SuspensionResolved(data={
                    "record_id": record.record_id,
                    "request_ids": sorted(record.request_ids())}),
            ))

            # 句柄回到 running（子 turn 又要跑了）；abort 则不续跑，直接落终态。
            self._spawn_handles.set_result(
                handle.handle_id, status="running", result=None)
            if plan.abort:
                # system_retry abort：子 turn 在挂起点终止 → 视为失败终态（与 leaf 一致）。
                self._spawn_handles.set_result(
                    handle.handle_id, status="error",
                    result=f"spawn_aborted: {record.record_id}")
                await eng._emit(EventMsg(  # noqa: SLF001
                    submission_id=handle.handle_id,
                    msg=SpawnFailed(data={
                        "handle_id": handle.handle_id,
                        "error": f"spawn_aborted: {record.record_id}"}),
                ))
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
            self._spawn_cancels[handle.handle_id] = cancel
            resumed_history = await eng._load_thread_items(child_tid)  # noqa: SLF001
            runner = eng._build_child_runner(  # noqa: SLF001
                target, child_tid, resumed_history[0], cancel,
                history=resumed_history)
            outcome = await runner.run()
            # 3. 与 _drive_spawn 收尾一致：回写句柄 + emit 终态（再挂起 → 可再 resume）。
            await self._finalize_spawn(handle.handle_id, child_tid, outcome)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "detached spawn resume crashed: %s", handle.handle_id)
            self._spawn_handles.set_result(
                handle.handle_id, status="error", result=str(e))
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle.handle_id,
                msg=SpawnFailed(data={
                    "handle_id": handle.handle_id, "error": str(e)}),
            ))

    def spawn_status(self, handle_ids: list[str]) -> dict[str, dict[str, Any]]:
        """查询一批 spawn 句柄的状态 / 结果（业务侧轮询 / join 检查用）。

        未知 handle_id 返回 ``{"status": "unknown", "result": None}``（显式可识别，
        不静默忽略）；已知句柄返回其当前 ``status`` 与 ``result``。

        Args:
            handle_ids: 要查询的句柄 id 列表。

        Returns:
            ``{handle_id: {"status": ..., "result": ...}}``。
        """
        out: dict[str, dict[str, Any]] = {}
        for hid in handle_ids:
            h = self._spawn_handles.get(hid)
            if h is None:
                out[hid] = {"status": "unknown", "result": None}
            else:
                out[hid] = {"status": h.status, "result": h.result}
        return out

    async def kill_spawn(self, handle_id: str) -> None:
        """主动终止一个 detached spawn —— 只取消该 spawn 子树，不波及兄弟/父 turn。

        语义（与 spawn_status 的"批量只读、未知不报错"不同：kill 是单点写操作，
        未知句柄是调用方明确错误，必须显式 KeyError 暴露，杜绝静默 no-op）：
          - 未知 handle_id → 抛 ``KeyError(handle_id)``。
          - 已终态句柄（done/error/cancelled）→ 良性 no-op：杀一个已结束的 spawn
            无意义但无害，直接返回，**不**报错、**不**重复 emit。
          - 运行中 / 挂起句柄 → 取消其专属取消子 token（若有 live 子树则 runner 在
            迭代边界 raise CancelledError → _drive_spawn 的 finally 释放 K1 槽位，
            _finalize_spawn 落 cancelled；挂起句柄已无 live 子树，token 取消是空操作，
            故此处再显式落 cancelled 终态 + emit SpawnCancelled 保证状态确定收敛）。

        Args:
            handle_id: 要终止的 spawn 句柄 id。

        Raises:
            KeyError: handle_id 未注册（调用方传错）。
        """
        eng = self._engine
        h = self._spawn_handles.get(handle_id)
        if h is None:
            raise KeyError(handle_id)
        # 已终态：良性 no-op（不报错、不重复落终态、不重复 emit）。
        if self._spawn_handles.is_terminal(handle_id):
            return
        # 取消该 spawn 的专属子树 token（精确隔离，不动兄弟 spawn）。
        # 运行中：live 子树会在迭代边界 raise → 走 _drive_spawn finally 释放 K1 +
        #   _finalize_spawn 落 cancelled（与本处落 cancelled 同值，幂等）。
        # 挂起：无 live 子树（首发 _drive_spawn 已收尾、K1 已释放），cancel 为空操作。
        token = self._spawn_cancels.get(handle_id)
        if token is not None:
            token.cancel()
        # 显式落 cancelled 终态 + emit —— 确保挂起句柄（无 live 子树驱动 finalize）也
        # 立即收敛到 cancelled，状态对外可见。
        self._spawn_handles.set_result(handle_id, status="cancelled", result=None)
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=handle_id,
            msg=SpawnCancelled(data={"handle_id": handle_id}),
        ))
        # join-barrier:kill 使本句柄进入 cancelled 终态,可能凑齐某 barrier → 检查。
        await self._check_barriers(handle_id)

    def has_live_spawns(self) -> bool:
        """是否存在未终结（running / suspended）的 detached spawn —— 引用计数保活。

        EnginePool 释放/淘汰 engine 前据此判定：有 live spawn 时**不得**释放
        engine（否则 detached 子任务 / 挂起待 resume 的 spawn 会随 engine 一起被
        取消、丢失）。全部 spawn 进入终态后才允许释放。

        Returns:
            True iff 至少一个句柄状态不在 done/error/cancelled 中。
        """
        return any(
            not self._spawn_handles.is_terminal(hid)
            for hid in self._spawn_handles.handles
        )

    # -----------------------------------------------------------------
    # join-barrier：登记「{句柄集}全终态 → 自动起聚合(联合会诊)turn」
    # -----------------------------------------------------------------

    async def set_join_barrier(
        self,
        handle_ids: list[str],
        then_skill_id: str,
        then_args_template: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """登记一个 join-barrier:等 handle_ids 全终态后自动起 then_skill_id 聚合 turn。

        聚合 turn 走 ``_build_child_runner``(call_stack 空 → 独立根 turn,**不**过
        DispatchPolicy 的 entry 门控),故 ``then_skill_id`` 可以是 entry 也可以非 entry,
        校验仅要求其**存在于 snapshot**。失败/取消的专家终态不被丢弃 —— 触发时默认
        把每个 handle 的 {status, result} 带入聚合输入(见 _check_barriers)。

        登记后**立即检查一次**:handle 可能在登记前就已全终态(本测试/业务常见),
        此时同步触发,不需要再等新的终态事件。

        Args:
            handle_ids: 本 barrier 等待的全部 spawn 句柄 id(必须均已注册)。
            then_skill_id: 全终态后续接执行的聚合 skill id(须存在于 snapshot)。
            then_args_template: 聚合 skill 的自定义参数模板;None → 用默认(各 handle 终态)。

        Returns:
            ``{"barrier_id": ...}``。

        Raises:
            ValueError: 某 handle_id 未注册,或 then_skill_id 不存在于 snapshot。
        """
        import secrets

        eng = self._engine
        # 1. 校验:每个 handle 必须已注册(未注册是调用方明确错误,显式抛,不静默)
        for hid in handle_ids:
            if self._spawn_handles.get(hid) is None:
                raise ValueError(f"unknown_spawn_handle: {hid}")
        # 2. 校验:聚合 skill 必须存在(不门控 entry——聚合走独立根 turn 无 entry 门)
        if eng._snapshot.get(then_skill_id) is None:  # noqa: SLF001
            raise ValueError(f"unknown_skill: {then_skill_id}")

        barrier_id = f"jb_{secrets.token_hex(4)}"
        barrier = JoinBarrier(
            barrier_id=barrier_id,
            handle_ids=tuple(handle_ids),
            then_skill_id=then_skill_id,
            then_args_template=then_args_template,
        )
        self._spawn_handles.barriers[barrier_id] = barrier

        # 3. parent thread 落登记锚(冷恢复可重建 barrier)+ emit 登记事件
        from taifeng.conversation.models import join_barrier_item

        anchor = join_barrier_item(
            barrier_id=barrier_id,
            handle_ids=list(handle_ids),
            then_skill_id=then_skill_id,
            then_args_template=then_args_template,
            thread_id=eng._thread_id,  # noqa: SLF001
        )
        async with eng._lock:  # noqa: SLF001
            eng._history.append(anchor)  # noqa: SLF001
        await eng._store.append(anchor)  # noqa: SLF001
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=barrier_id,
            msg=JoinBarrierRegistered(data={
                "barrier_id": barrier_id,
                "handle_ids": list(handle_ids),
                "then_skill_id": then_skill_id,
            }),
        ))

        # 4. 立即检查:handle 可能登记前已全终态 → 同步触发(否则永不再有终态事件唤醒)
        await self._check_barriers()
        return {"barrier_id": barrier_id}

    async def _check_barriers(self, changed_handle_id: str | None = None) -> None:
        """扫描所有 barrier:句柄集全终态且未触发过 → 起聚合 turn(幂等)。

        每个 spawn 进终态(_finalize_spawn / kill_spawn)及 barrier 登记时调用。
        ``changed_handle_id`` 仅作日志/未来优化用,当前实现全量扫描(barrier 数量极小)。

        幂等:``self._fired_barriers`` 进程内守卫保证每个 barrier 至多触发一次。

        Args:
            changed_handle_id: 触发本次检查的 handle(可选,仅记录)。
        """
        for barrier in list(self._spawn_handles.barriers.values()):
            if barrier.barrier_id in self._fired_barriers:
                continue  # 已触发,幂等跳过
            if not self._spawn_handles.all_terminal(list(barrier.handle_ids)):
                continue  # 尚未全终态,等下一次终态事件
            await self._fire_barrier(barrier)

    async def _fire_barrier(self, barrier: JoinBarrier) -> None:
        """触发一个 barrier:起 then_skill 独立聚合 turn,落 fired 标记 + emit。

        聚合输入:then_args_template(若给)否则默认 = {handle_id: {status, result}},
        **含失败/取消句柄**(非 done 终态不丢弃,会诊需看到全部专家结局)。
        聚合 turn 经 ``_build_child_runner``(call_stack 空)以独立根 turn 跑,
        取消 token 自根派生(R4),其完成不回写本 engine 的 history/句柄表。

        Args:
            barrier: 已全终态、待触发的 barrier。
        """
        import json

        eng = self._engine
        # 默认聚合输入:每个 handle 的终态 {status, result}(含取消/失败,不丢)
        if barrier.then_args_template is not None:
            args: dict[str, Any] = dict(barrier.then_args_template)
        else:
            args = {}
            for hid in barrier.handle_ids:
                h = self._spawn_handles.get(hid)
                # all_terminal 已保证 h 存在;终态句柄一律带入
                assert h is not None
                args[hid] = {"status": h.status, "result": h.result}

        target = eng._snapshot.get(barrier.then_skill_id)  # noqa: SLF001
        if target is None:
            raise RuntimeError(
                f"join_barrier_skill_missing: {barrier.then_skill_id}")

        # 起独立聚合 child thread + 种子 user_message(聚合输入 JSON)
        then_thread_id = await eng._store.create_thread(  # noqa: SLF001
            cwd=None,
            entry_skill_id=barrier.then_skill_id,
            source=f"join_barrier:{barrier.barrier_id}",
            extra={
                "parent_thread_id": eng._thread_id,  # noqa: SLF001
                "barrier_id": barrier.barrier_id,
            },
        )
        seed = user_message(
            json.dumps(args, ensure_ascii=False), thread_id=then_thread_id)
        await eng._store.append(seed)  # noqa: SLF001

        assert eng._root_cancel is not None  # barrier 仅在 run 启动后可触发  # noqa: SLF001
        cancel = eng._root_cancel.child(f"barrier:{barrier.barrier_id}")  # noqa: SLF001
        runner = eng._build_child_runner(  # noqa: SLF001
            target, then_thread_id, seed, cancel, history=[seed])
        # 聚合 turn 后台独立跑(不阻塞主 actor / 不回写本 engine 状态)
        asyncio.create_task(runner.run())

        # 幂等:先入守卫集再落 fired 标记 + emit(防止并发 _check_barriers 重入触发)
        self._fired_barriers.add(barrier.barrier_id)
        from taifeng.conversation.models import join_barrier_fired_item

        marker = join_barrier_fired_item(
            barrier_id=barrier.barrier_id,
            then_thread_id=then_thread_id,
            thread_id=eng._thread_id,  # noqa: SLF001
        )
        async with eng._lock:  # noqa: SLF001
            eng._history.append(marker)  # noqa: SLF001
        await eng._store.append(marker)  # noqa: SLF001
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=barrier.barrier_id,
            msg=JoinBarrierFired(data={
                "barrier_id": barrier.barrier_id,
                "then_thread_id": then_thread_id,
            }),
        ))

    # -----------------------------------------------------------------
    # detached-spawn 冷恢复:从 parent thread 持久项重建句柄表 + barrier + 守卫集
    # -----------------------------------------------------------------

    async def rebuild_from_history(self) -> None:
        """冷恢复:扫描已物化的 parent thread 历史,重建 spawn 句柄/barrier/守卫集。

        engine 释放后同 session 重载时,内存中的 ``_spawn_handles`` / ``_fired_barriers``
        全空——但 parent thread 的 JSONL 里留有 spawn / join_barrier / join_barrier_fired
        三类锚。本方法据此重建运行态(R5 可 resume):

          1. ``spawn`` 锚 → ``register`` 句柄,再 load 子 thread 推断终态:
             - 子 thread 有活跃挂起(``_find_active_suspension_in``)→ suspended
             - 否则有 assistant_message → done(result = 最后一条 assistant 文本)
             - 否则(无终态标记,跑到一半被中断)→ 保持 running(best-effort,不崩)
          2. ``join_barrier`` 锚 → 重建 ``JoinBarrier`` 进 barriers 表
          3. ``join_barrier_fired`` 锚 → 把 barrier_id 加入 ``_fired_barriers`` 守卫集
             (幂等:已触发过的 barrier 重载后不二次起聚合 turn)

        重建后调一次 ``_check_barriers``:某 barrier 全终态却尚未触发(进程在触发前
        崩溃)→ 此刻补触发;已 fired 的 barrier 因守卫集存在被跳过(no-op)。

        仅在 engine 持有 prior history(resume 场景)时由 pool 调用,且每次重载恰好一次。
        """
        eng = self._engine
        # 扫一遍 parent history,按 kind 分类处理三类锚
        for item in list(eng._history):  # noqa: SLF001
            if item.kind == "spawn":
                handle_id = item.payload["handle_id"]
                skill_id = item.payload["skill_id"]
                child_thread_id = item.payload["child_thread_id"]
                # 重复 register 是安全的(同 id 覆盖),但正常每个 handle 只一条 spawn 锚
                self._spawn_handles.register(
                    handle_id=handle_id,
                    skill_id=skill_id,
                    child_thread_id=child_thread_id,
                )
                await self._infer_spawn_status_from_child(
                    handle_id, child_thread_id
                )
            elif item.kind == "join_barrier":
                barrier = JoinBarrier(
                    barrier_id=item.payload["barrier_id"],
                    handle_ids=tuple(item.payload["handle_ids"]),
                    then_skill_id=item.payload["then_skill_id"],
                    then_args_template=item.payload.get("then_args_template"),
                )
                self._spawn_handles.barriers[barrier.barrier_id] = barrier
            elif item.kind == "join_barrier_fired":
                # 幂等守卫集重建:已触发的 barrier 重载后不再二次触发聚合 turn
                self._fired_barriers.add(item.payload["barrier_id"])

        # 重建后补触发:全终态但尚未 fired 的 barrier 此刻起聚合 turn;
        # 已 fired 的因守卫集存在被 _check_barriers 跳过(幂等 no-op)。
        await self._check_barriers()

    async def _infer_spawn_status_from_child(
        self, handle_id: str, child_thread_id: str
    ) -> None:
        """据子 thread 的持久态推断单个 spawn 句柄的终态,best-effort 回写句柄。

        分类(与 _drive_spawn/_finalize_spawn 的终态语义对齐):
          - 活跃挂起记录 → suspended(可后续 resume)
          - 无挂起 + 有 assistant_message → done(result = 最后一条 assistant 文本)
          - 既无挂起也无 assistant_message → 跑到一半被中断 → 保持 register 的 running
            (v1 best-effort:不臆断为 error,留 running 表"需重跑";绝不崩)

        Args:
            handle_id: 已 register 的句柄 id。
            child_thread_id: 该句柄对应的子 thread id。
        """
        eng = self._engine
        items = await eng._load_thread_items(child_thread_id)  # noqa: SLF001
        # 优先判挂起:活跃(未被 resolved-marker 核销)的挂起记录 → suspended
        if eng._find_active_suspension_in(items) is not None:  # noqa: SLF001
            self._spawn_handles.set_result(
                handle_id, status="suspended", result=None
            )
            return
        # 无挂起:找最后一条 assistant_message 作为完成结果
        last_text: str | None = None
        for it in items:
            if it.kind == "assistant_message":
                last_text = it.payload.get("text")
        if last_text is not None:
            self._spawn_handles.set_result(
                handle_id, status="done", result=last_text
            )
            return
        # 既无挂起也无 assistant_message → 中断态,保持 running(best-effort,不回写)
