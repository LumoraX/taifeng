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
    SpawnCancelled,
    SpawnCompleted,
    SpawnFailed,
    SpawnStarted,
    SpawnSuspended,
)
from taifeng.loop.peer_mailbox import PeerMailbox
from taifeng.loop.spawn_barrier import JoinBarrierCoordinator
from taifeng.loop.spawn_handle import SpawnHandle, SpawnHandleRegistry
from taifeng.loop.spawn_resume import SpawnResumeChain
from taifeng.loop.spawn_rewind import SpawnRewindChain

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.submission import Submission

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
        # peer-mailbox：child_thread_id → 当前正在跑的 live TurnRunner。
        # QueueOnly 投运行中目标需要拿到其 pending_input(B1 steering 同一队列);
        # 各驱动路径(首发 / resume / 唤醒)run 前登记、finally 弹出。
        self._live_runners: dict[str, Any] = {}
        # thread-addressable rewind:rewind 在飞的 child_thread_id 集合。
        # 并发双 Rewind 拒后到者(占位期间二次 rewind 报 thread_running)。
        self._rewinding_threads: set[str] = set()
        # 子协调器（spawn-module-structure 契约:无自有状态,经本 driver 访问
        # 上述运行态表;公共入口由本类同名转发器暴露,外部契约不变）。
        self._peers = PeerMailbox(self)
        self._barriers = JoinBarrierCoordinator(self)
        self._resume = SpawnResumeChain(self)
        self._rewind = SpawnRewindChain(self)

    async def _await_root_cancel_ready(self) -> None:
        """有界让步等待根取消 token 就绪，超时显式抛错（不无限自旋、不静默跳过）。

        engine.run 由 pool 以 create_task 后台启动，``_root_cancel`` 在其首行赋值。
        紧随 ``get_or_create`` 的调用方（spawn_skill / rebuild_from_history）可能在
        run() 尚未被调度时触达，需短暂让步等其就绪——任何会派子 runner / 触发 barrier
        （都派生 ``_root_cancel.child(...)``，R4 要求挂在根取消树上）的入口都应先等。

        Raises:
            RuntimeError: 让步上限内根取消 token 仍未就绪（engine 未启动）。
        """
        eng = self._engine
        for _ in range(100):
            if eng._root_cancel is not None:  # noqa: SLF001
                return
            await asyncio.sleep(0)
        if eng._root_cancel is None:  # noqa: SLF001
            raise RuntimeError("engine not running: root cancel token unavailable")

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
        # detached spawn 把目标作为独立 child thread 分离发起（非嵌套子调用），调 entry
        # skill 是其正当用法（业务子任务 skill 多为 entry）→ allow_entry_target=True 跳过 entry 门。
        verdict = eng._dispatch_policy.check(  # noqa: SLF001
            stack, eng._entry_skill, target,  # noqa: SLF001
            allow_entry_target=True,
        )
        if not verdict.allowed:
            raise ValueError(f"dispatch_rejected: {verdict.reason}")

        # spawn_skill 紧随 get_or_create 调用时可能 run() 尚未被调度 → 有界让步等待
        # 根取消 token 就绪（R4：分离 task 必须挂在根取消树上，不可凭空造游离 token）。
        await self._await_root_cancel_ready()

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
            # peer-mailbox：登记 live runner（QueueOnly 投运行中目标用其 pending_input）
            self._live_runners[child_thread_id] = runner
            try:
                outcome = await runner.run()
            finally:
                self._live_runners.pop(child_thread_id, None)
            await self._finalize_spawn(handle_id, child_thread_id, outcome)
        except Exception as e:  # noqa: BLE001
            # 兜底：不让句柄卡死在 running。记日志（不静默）+ 单点收敛失败终态
            # （含 join-barrier 重查；barrier 自身故障抑制为日志，不逃出后台 task）。
            logger.exception("detached spawn driver crashed: %s", handle_id)
            await self._settle_failed(
                handle_id, str(e), suppress_barrier_errors=True)
        finally:
            # K1：detached 语义下手动占用的 slot 在子 task 收尾时释放。
            eng._spawn_registry.release_manual()  # noqa: SLF001

    async def _finalize_spawn(
        self, handle_id: str, child_thread_id: str, outcome: Any
    ) -> None:
        """按子 turn 的 end_reason 回写句柄状态并 emit 对应终态事件。

        - completed → done + SpawnCompleted(result=final_text)
        - suspended → suspended + SpawnSuspended（Resume 经 match_suspended_spawn 路由续跑）
        - cancelled → cancelled + SpawnCancelled
        - 其余（error / 未知）→ error + SpawnFailed

        **终态幂等（单点收敛）**：若句柄已处于终态（done/error/cancelled），直接
        no-op 返回——不覆盖状态、不重复 emit、不重复跑 _check_barriers。这使
        _finalize_spawn 成为唯一安全收敛点：kill 一个 running spawn 时，
        kill_spawn 已显式取消 token 但**不**内联落终态/emit（见 kill_spawn），
        由被取消的 live runner 退栈后唯一一次走到本方法 emit SpawnCancelled；
        而 kill 一个 suspended spawn（无 live runner 驱动本方法）由 kill_spawn
        内联收敛。两路径合计对同一句柄**恰好一次** SpawnCancelled。
        """
        eng = self._engine
        # 终态幂等：已收敛的句柄不再二次处理（防 running-kill 双发 spawn_cancelled）。
        if self._spawn_handles.is_terminal(handle_id):
            return
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
            # Resume(thread_id=child_thread_id) 经 match_suspended_spawn 命中后由
            # resume_spawn / resume_spawn_nested 续跑（支持多轮错峰 HITL）。
            self._spawn_handles.set_result(
                handle_id, status="suspended", result=None
            )
            # record_id 与 pending 同源派生：消费方按 (handle_id, record_id) 做幂等键
            # —— 首挂 / 每次二次挂起各带不同 record_id（新挂起点 = 新 record），
            # 同一 record_id 重放（冷恢复 / 部分核销后仍挂）视作同一逻辑挂起。
            # 与 turn_suspended 的 record_id 同源，便于跨事件对齐。
            suspension = outcome.suspension
            pending = (
                suspension.to_item().payload["pending"]
                if suspension is not None
                else []
            )
            record_id = suspension.record_id if suspension is not None else None
            await eng._emit(EventMsg(  # noqa: SLF001
                submission_id=handle_id,
                msg=SpawnSuspended(data={
                    "handle_id": handle_id,
                    "thread_id": child_thread_id,
                    "record_id": record_id,
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

    async def _settle_failed(
        self,
        handle_id: str,
        error: str,
        *,
        suppress_barrier_errors: bool = False,
    ) -> None:
        """失败终态的**唯一收敛点**:回写 error + emit SpawnFailed + barrier 重查。

        (spawn-terminal-single-convergence)任何使句柄进入 error 终态的路径
        ——abort 裁决 / 驱动·续跑·唤醒的宽 except 兜底——必须走本方法,禁止
        各自手写三件套。历史事故:abort 分支漏调 ``_check_barriers``,被等待的
        句柄虽落终态但 barrier 永不重查 → 聚合 turn 永不触发、下游挂死。

        终态幂等(对齐 ``_finalize_spawn`` 守卫):已终态句柄 no-op——不覆盖
        状态、不重复 emit、不重复 barrier 重查;终态事件对外恰好一次。

        Args:
            handle_id: 要收敛的 spawn 句柄 id。
            error: 失败原因串(落入句柄 result 与 SpawnFailed.error)。
            suppress_barrier_errors: True(仅限 except 兜底场景)时 barrier
                重查自身抛错只 ``logger.exception`` 记日志、不外抛——此时原始
                异常已记录、句柄终态与 SpawnFailed 已完成,barrier 配置故障
                (如聚合 skill 随 snapshot 热更消失)不得逃出后台 task 成为
                unhandled exception;False(正常控制流,如 abort 裁决分支)
                时自然向上传播,禁 silent fallback。
        """
        eng = self._engine
        # 终态幂等:已收敛句柄不二次处理(终态事件恰好一次)。
        if self._spawn_handles.is_terminal(handle_id):
            return
        self._spawn_handles.set_result(handle_id, status="error", result=error)
        await eng._emit(EventMsg(  # noqa: SLF001
            submission_id=handle_id,
            msg=SpawnFailed(data={"handle_id": handle_id, "error": error}),
        ))
        # join-barrier:本句柄进入 error 终态,可能凑齐某 barrier 的全终态条件。
        try:
            await self._check_barriers(handle_id)
        except Exception:
            if not suppress_barrier_errors:
                raise
            # 兜底场景:句柄已收敛、事件已发,仅 barrier 触发这一独立故障被
            # 显式记录(冷恢复 rebuild_from_history 末尾补查可兜底)。
            logger.exception(
                "join-barrier recheck failed after spawn settled error: %s",
                handle_id)

    def suspended_handles(self) -> list[SpawnHandle]:
        """当前 suspended 状态句柄的只读快照(suspension-ttl 冷重武装枚举用)。"""
        return [
            h for h in self._spawn_handles.handles.values()
            if h.status == "suspended"
        ]

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
        """转发到 ``SpawnResumeChain.resume_spawn``（错峰续跑公共入口签名不变）。

        直接挂起核销重跑与嵌套挂起下探回填链的实现体在
        loop/spawn_resume.py；终态统一回本类收敛点
        （_finalize_spawn / _settle_failed）。
        """
        await self._resume.resume_spawn(sub, handle)

    async def rewind_spawn(self, sub: Submission) -> None:
        """转发到 ``SpawnRewindChain.rewind_spawn``(thread-addressable rewind)。

        守卫(活性判定)、截断(marker 落子 thread store)与重推的实现体在
        loop/spawn_rewind.py;终态统一回本类收敛点
        (_finalize_spawn / _settle_failed)。
        """
        await self._rewind.rewind_spawn(sub)

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
          - **运行中句柄**（status=running，有 live 子树）→ 仅取消其专属 token，**不**
            在此内联落终态/emit；被取消的 runner 在迭代边界 raise CancelledError →
            退栈后由 _drive_spawn 调 _finalize_spawn 唯一一次 emit SpawnCancelled
            （_drive_spawn 的 finally 同时释放 K1 槽位）。如此 running-kill 恰好一次。
          - **挂起句柄**（status=suspended，无 live 子树驱动 finalize）→ 取消 token
            （空操作，首发 _drive_spawn 已退栈），并在此内联落 cancelled + emit +
            _check_barriers 收敛，确保挂起句柄状态确定对外可见。

        为何按 status 分流而非统一内联：running-kill 若也内联 emit，则 live runner
        退栈后 _finalize_spawn 会对同一句柄再 emit 第二条 spawn_cancelled（消费方
        重复计数）。把 running 的收敛权唯一交给 _finalize_spawn（已做终态幂等），
        suspended 因无 live runner 必须自行收敛——两者合计恰好一次 spawn_cancelled。

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
        token = self._spawn_cancels.get(handle_id)
        if token is not None:
            token.cancel()
        # running：有 live 子树，收敛权唯一交给被取消 runner 退栈后的 _finalize_spawn
        #   （已做终态幂等），此处**不**内联 emit，避免双发 spawn_cancelled。
        if h.status == "running":
            return
        # suspended：无 live 子树驱动 finalize（首发 _drive_spawn 已退栈、K1 已释放），
        #   故在此内联落 cancelled 终态 + emit + barrier 检查，保证确定收敛。
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
    # peer-mailbox：谱系内点对点投递 —— 实现体在 loop/peer_mailbox.py（PeerMailbox）
    # -----------------------------------------------------------------

    async def deliver_peer_message(
        self,
        *,
        target: str,
        text: str,
        mode: str = "queue_only",
        from_thread_id: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        """转发到 ``PeerMailbox.deliver_peer_message``（公共入口签名不变）。"""
        return await self._peers.deliver_peer_message(
            target=target, text=text, mode=mode,
            from_thread_id=from_thread_id, submission_id=submission_id)

    async def wait_spawn_terminal(
        self,
        *,
        handle_id: str,
        timeout_seconds: float,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """转发到 ``PeerMailbox.wait_spawn_terminal``（``wait_peer`` 工具实现体）。"""
        return await self._peers.wait_spawn_terminal(
            handle_id=handle_id, timeout_seconds=timeout_seconds, cancel=cancel)


    # -----------------------------------------------------------------
    # join-barrier + 冷恢复 —— 实现体在 loop/spawn_barrier.py（JoinBarrierCoordinator）
    # -----------------------------------------------------------------

    async def set_join_barrier(
        self,
        handle_ids: list[str],
        then_skill_id: str,
        then_args_template: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """转发到 ``JoinBarrierCoordinator.set_join_barrier``（公共入口签名不变）。"""
        return await self._barriers.set_join_barrier(
            handle_ids, then_skill_id, then_args_template)

    async def _check_barriers(self, changed_handle_id: str | None = None) -> None:
        """转发到 ``JoinBarrierCoordinator._check_barriers``。

        终态收敛点（_finalize_spawn / _settle_failed / kill_spawn）与登记 /
        冷恢复的 barrier 重查统一经本转发器——也是测试 monkeypatch 的注入点
        （实例属性遮蔽即可替换，见 test_settle_failed_barrier_error_isolation）。
        """
        await self._barriers._check_barriers(changed_handle_id)  # noqa: SLF001

    async def rebuild_from_history(self) -> None:
        """转发到 ``JoinBarrierCoordinator.rebuild_from_history``（冷恢复，R5）。"""
        await self._barriers.rebuild_from_history()
