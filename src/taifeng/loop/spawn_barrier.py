"""JoinBarrierCoordinator —— barrier 生命周期 + 冷恢复（自 SpawnDriver 拆出，零行为变更）。

职责（detached-spawn 契约的 barrier / 冷恢复段实现体）：
  - join-barrier 登记 + 全终态重查 + 触发聚合 turn
    （set_join_barrier / _check_barriers / _fire_barrier）
  - 冷恢复:从 parent thread 持久锚重建句柄表 / barrier / 守卫集
    （rebuild_from_history / _infer_spawn_status_from_child）

设计（对应 spawn-module-structure 契约）：本类**无自有状态**，持 driver 引用，
经 ``driver._spawn_handles`` / ``driver._fired_barriers`` 访问 SpawnDriver
单一持有的运行态。公共入口（set_join_barrier / rebuild_from_history）与终态
收敛回调点（_check_barriers）由 SpawnDriver 同名转发器暴露——engine 薄转发层、
测试 monkeypatch 点（``driver._check_barriers``）契约不变。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import user_message
from taifeng.loop.event import (
    EventMsg,
    JoinBarrierFired,
    JoinBarrierRegistered,
)
from taifeng.loop.spawn_handle import JoinBarrier

if TYPE_CHECKING:
    from taifeng.loop.spawn_driver import SpawnDriver


class JoinBarrierCoordinator:
    """join-barrier 协作器：登记 / 全终态重查 / 触发聚合 / 冷恢复重建。

    无自有状态；barriers 表与 fired 守卫集均经 driver 访问
    （状态单一持有，见 spawn-module-structure 契约）。
    """

    def __init__(self, driver: SpawnDriver) -> None:
        """
        Args:
            driver: 宿主 SpawnDriver —— 提供句柄表（含 barriers 表）/ fired
                守卫集与 engine 共享依赖（store / emit / snapshot / lock 等）。
        """
        self._driver = driver

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

        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        # 1. 校验:每个 handle 必须已注册(未注册是调用方明确错误,显式抛,不静默)
        for hid in handle_ids:
            if drv._spawn_handles.get(hid) is None:  # noqa: SLF001
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
        drv._spawn_handles.barriers[barrier_id] = barrier  # noqa: SLF001

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
        # 经 driver 转发器走(而非直调自身):保持 monkeypatch 注入点单一。
        await drv._check_barriers()  # noqa: SLF001
        return {"barrier_id": barrier_id}

    async def _check_barriers(self, changed_handle_id: str | None = None) -> None:
        """扫描所有 barrier:句柄集全终态且未触发过 → 起聚合 turn(幂等)。

        每个 spawn 进终态(_finalize_spawn / _settle_failed / kill_spawn)及
        barrier 登记时经 driver 同名转发器调用。``changed_handle_id`` 仅作
        日志/未来优化用,当前实现全量扫描(barrier 数量极小)。

        幂等:``driver._fired_barriers`` 进程内守卫保证每个 barrier 至多触发一次。

        Args:
            changed_handle_id: 触发本次检查的 handle(可选,仅记录)。
        """
        drv = self._driver
        for barrier in list(drv._spawn_handles.barriers.values()):  # noqa: SLF001
            if barrier.barrier_id in drv._fired_barriers:  # noqa: SLF001
                continue  # 已触发,幂等跳过
            if not drv._spawn_handles.all_terminal(  # noqa: SLF001
                    list(barrier.handle_ids)):
                continue  # 尚未全终态,等下一次终态事件
            await self._fire_barrier(barrier)

    async def _fire_barrier(self, barrier: JoinBarrier) -> None:
        """触发一个 barrier:起 then_skill 独立聚合 turn,落 fired 标记 + emit。

        聚合输入:then_args_template(若给)否则默认 = {handle_id: {status, result}},
        **含失败/取消句柄**(非 done 终态不丢弃,聚合需看到全部子任务结局)。
        聚合 turn 经 ``_build_child_runner``(call_stack 空)以独立根 turn 跑,
        取消 token 自根派生(R4),其完成不回写本 engine 的 history/句柄表。

        Args:
            barrier: 已全终态、待触发的 barrier。
        """
        import json

        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        # 默认聚合输入:每个 handle 的终态 {status, result}(含取消/失败,不丢)
        if barrier.then_args_template is not None:
            args: dict[str, Any] = dict(barrier.then_args_template)
        else:
            args = {}
            for hid in barrier.handle_ids:
                h = drv._spawn_handles.get(hid)  # noqa: SLF001
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
        # 先广播 fired，再启动聚合 turn：订阅方要用 then_thread_id 预先开会诊轨。
        # 若先启动 child runner，极快的模型可能抢在 fired 事件前发 assistant_text，
        # 下游只能把这段文本归到未知/root 轨。
        drv._fired_barriers.add(barrier.barrier_id)  # noqa: SLF001
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=barrier.barrier_id,
            msg=JoinBarrierFired(data={
                "barrier_id": barrier.barrier_id,
                "then_thread_id": then_thread_id,
            }),
        ))

        # 聚合 turn 后台独立跑(不阻塞主 actor / 不回写本 engine 状态)。
        drv._start_owned_task(  # noqa: SLF001
            runner.run(),
            name=f"barrier:{barrier.barrier_id}",
        )

        # 持久化 fired 标记，供冷恢复幂等重建。
        from taifeng.conversation.models import join_barrier_fired_item

        marker = join_barrier_fired_item(
            barrier_id=barrier.barrier_id,
            then_thread_id=then_thread_id,
            thread_id=eng._thread_id,  # noqa: SLF001
        )
        async with eng._lock:  # noqa: SLF001
            eng._history.append(marker)  # noqa: SLF001
        await eng._store.append(marker)  # noqa: SLF001

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
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        # 冷恢复加固：rebuild 末尾的 _check_barriers 可能补触发 barrier，而 _fire_barrier
        # 派生 _root_cancel.child(...) 并 assert 其非 None。pool.get_or_create 调本方法时
        # run() 可能尚未被调度（_root_cancel 未赋值）——故先有界让步等其就绪再继续，
        # 不依赖偶然的 await 时序（与 spawn_skill 同一就绪保障）。
        await drv._await_root_cancel_ready()  # noqa: SLF001
        # 扫一遍 parent history,按 kind 分类处理三类锚
        for item in list(eng._history):  # noqa: SLF001
            if item.kind == "spawn":
                handle_id = item.payload["handle_id"]
                skill_id = item.payload["skill_id"]
                child_thread_id = item.payload["child_thread_id"]
                # 重复 register 是安全的(同 id 覆盖),但正常每个 handle 只一条 spawn 锚
                drv._spawn_handles.register(  # noqa: SLF001
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
                drv._spawn_handles.barriers[barrier.barrier_id] = barrier  # noqa: SLF001
            elif item.kind == "join_barrier_fired":
                # 幂等守卫集重建:已触发的 barrier 重载后不再二次触发聚合 turn
                drv._fired_barriers.add(item.payload["barrier_id"])  # noqa: SLF001

        # 重建后补触发:全终态但尚未 fired 的 barrier 此刻起聚合 turn;
        # 已 fired 的因守卫集存在被 _check_barriers 跳过(幂等 no-op)。
        # 经 driver 转发器走:保持 monkeypatch 注入点单一。
        await drv._check_barriers()  # noqa: SLF001

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
        drv = self._driver
        eng = drv._engine  # noqa: SLF001
        items = await eng._load_thread_items(child_thread_id)  # noqa: SLF001
        # 优先判挂起:活跃(未被 resolved-marker 核销)的挂起记录 → suspended
        if eng._find_active_suspension_in(items) is not None:  # noqa: SLF001
            drv._spawn_handles.set_result(  # noqa: SLF001
                handle_id, status="suspended", result=None
            )
            return
        # 无挂起:找最后一条 assistant_message 作为完成结果
        last_text: str | None = None
        for it in items:
            if it.kind == "assistant_message":
                last_text = it.payload.get("text")
        if last_text is not None:
            drv._spawn_handles.set_result(  # noqa: SLF001
                handle_id, status="done", result=last_text
            )
            return
        # 既无挂起也无 assistant_message → 中断态,保持 running(best-effort,不回写)
