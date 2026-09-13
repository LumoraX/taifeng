"""call_skill 子链续跑协作器：leaf 子 thread 核销 → 逐层回填父 call_skill → 根完成。

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。这组行为围绕同一入口
序列（``handle_child_resume``）彼此紧密调用，按 `engine-module-structure` 契约落为
**协作者类**。

与 ``spawn_resume.SpawnResumeChain`` 的分工：本类管 **call_skill 子链**——父 turn
仍在等 ``function_call_output`` 回填，故必须逐层回传；``SpawnResumeChain`` 管
**detached spawn**——父 turn 早已结束，子 thread 是独立根 turn，无回填链。两者
互不替代。

与既有协作者一致：**本类无自有状态**，运行态全部经 engine 引用访问。**兄弟调用
一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，`spawn_*` 兄弟
模块与测试按 ``eng._load_thread_items`` / ``eng._find_active_suspension_in`` 等原名
调用与打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import ResponseItem, function_call_output, system_injection
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.event import (
    EventMsg,
    SuspensionPartiallyResolved,
    SuspensionResolved,
    SuspensionResolveRejected,
)
from taifeng.loop.submission import Resume, Submission
from taifeng.loop.tool_batch import parse_tool_arguments
from taifeng.loop.turn import TurnRunner
from taifeng.suspend.record import SuspensionRecord
from taifeng.suspend.resolver import CHAIN_CANCELLED_RESULT
from taifeng.tool.spec import ToolResult

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine

logger = logging.getLogger(__name__)


class ChildResumeChain:
    """call_skill 子链续跑协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供 store / history / 挂起记录 / runner 构造。
        """
        self._engine = engine

    async def handle_child_resume(
        self, sub: Submission, op: Resume, root_cancel: CancellationToken
    ) -> None:
        """续跑一个【子 thread】的挂起，并把结果逐层回传父 call_skill 直到根完成。

        机制（对账 call_skill 正常非挂起回传路径 turn.py::_spawn_sub_runner）：
          1. 自根 self._engine._thread_id 沿 CHILD_SKILL pending（detail.sub_thread_id）向下
             串出 [根, …, leaf] 链，每层记 (thread_id, entry_skill_id, 父 call_id)。
             —— 不依赖 store.get_metadata（MessageStore 协议无元数据查询）：根 thread /
             entry skill 由 engine 自持，子层谱系由父挂起 record 的 pending detail 携带。
          2. leaf 子 thread：用用户 resolutions 核销真实挂起（permission/form/data），
             补 gap → 重建 TurnRunner 续跑 → 拿 final_text（= 正常子 turn 完成）。
          3. 自 leaf 向上：把每个父 call_skill 的 function_call_output 回填为子结果
             （= 正常 run_sub_skill 的 ToolResult.ok），续跑父 turn；根用既有
             self._engine._history / _build_and_run_runner 收尾。

        任一层续跑若又挂起（再触发挂起点），该层各自 emit turn_suspended，续跑链在该层
        中止（上层 call_skill 仍挂起，等下一次 Resume）—— 与单层 resume 语义一致。

        Args:
            sub: 本次 Resume submission（事件归因）。
            op: Resume(thread_id=<子 thread>, resolutions=...)。
            root_cancel: 根取消 token（派生各层 turn 的子 token）。
        """
        # 1. 自根向下串链至 leaf（op.thread_id）。链元素 = (thread_id, entry_skill_id, 父 call_id)
        chain = await self._engine._build_resume_chain(op.thread_id)
        if chain is None:
            # 根/中途某层无活跃 CHILD_SKILL 挂起指向目标 leaf → 找不到挂起，拒绝
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "no_active_suspension", "record_id": None, "detail": {}})))
            return

        # 链级取消 token(wave2b D5):整条续跑链是一个可取消的整体——各层 turn 的 token
        # 派生自它,CancelTurn(sub.id) 无论打在哪一层还是层间都能让链停下。登记为
        # 非根 pending(链上没有在飞的根 runner,InjectSystemMessage 仍走 engine 直写);
        # 沿用 gate 登记项的注入队列引用,不丢排队期间的注入。
        chain_cancel = root_cancel.child(f"resume:{sub.id}")
        gate_pending = self._engine._pending.get(sub.id)
        self._engine._pending[sub.id] = _PendingTurn(
            submission_id=sub.id, cancel=chain_cancel, is_root=False,
            pending_input=gate_pending.pending_input if gate_pending is not None else [],
        )

        # 2. leaf：核销用户挂起 + 续跑，拿到回传给父的结果字符串
        leaf_tid, leaf_skill_id, _ = chain[-1]
        leaf_result = await self._engine._resume_leaf_thread(
            sub, leaf_tid, leaf_skill_id, op.resolutions, chain_cancel)
        if leaf_result is None:
            # leaf 核销失败（已 emit Rejected）或又挂起（已 emit turn_suspended）→ 不上溯
            return

        # 3. 自 leaf 向上逐层回填父 call_skill output + 续跑父 turn，直到根完成或中途再挂起
        child_result = leaf_result
        # 倒序遍历父链（去掉 leaf 自身）：[..., 祖父, 父]
        for level in range(len(chain) - 2, -1, -1):
            parent_tid, parent_skill_id, _ = chain[level]
            # 本层 call_skill 的 call_id = 子层元素携带的"父 call_id"
            call_id = chain[level + 1][2]
            cont = await self._engine._resume_parent_level(
                sub, parent_tid, parent_skill_id, call_id, child_result, chain_cancel)
            if cont is None:
                # 父是根（根分支已收尾）/ 父又挂起 → 链终止
                return
            child_result = cont
        # 链因取消解到根(各层 gap 已回填 + 结算,根未重跑):以 turn_failed{cancelled}
        # 终结本 Resume submission(2a 终结信号语义)。根此刻已无活跃挂起,可接新
        # UserMessage / Rewind——不会卡在 CHILD_SKILL 上。
        if child_result == CHAIN_CANCELLED_RESULT:
            await self._engine._emit_operation_terminal(sub.id, None, kind="cancelled")

    async def build_resume_chain(
        self, leaf_thread_id: str
    ) -> list[tuple[str, str, str | None]] | None:
        """自根 self._engine._thread_id 沿 CHILD_SKILL pending 向下串出到 leaf 的续跑链。

        每层元素 = (thread_id, entry_skill_id, 该 thread 在【父】里对应的 call_skill call_id)；
        根层的"父 call_id"为 None。逐层用父挂起 record 的 CHILD_SKILL pending
        （detail.sub_thread_id / skill_id / related_call_id）确定下一层。

        Returns:
            [(根tid, 根skill, None), …, (leaftid, leafskill, 父callid)]；
            根/中途无指向 leaf 的活跃 CHILD_SKILL 挂起 → None（找不到挂起，调用方拒绝）。

        多 pending record(parallel 批多子同挂,multi-pending-partial-resume):
        按 DFS 遍历**全部** CHILD_SKILL 分支寻址 leaf——已核销分支的子 thread 无
        活跃挂起 → 自然死路回溯,不再"只取首个 pending 单路下探"。
        """
        from taifeng.suspend.reason import SuspendReason

        async def descend(
            tid: str, items: list[ResponseItem], depth: int,
        ) -> list[tuple[str, str, str | None]] | None:
            """返回自 tid 之下到 leaf 的链段(不含 tid 本层);找不到 → None。"""
            if tid == leaf_thread_id:
                return []
            if depth <= 0:
                return None  # 超出深度守卫(异常链)
            record = self._engine._find_active_suspension_in(items)
            if record is None:
                return None  # 本层无活跃挂起 → 链断
            for pend in record.pending:
                if pend.reason is not SuspendReason.CHILD_SKILL:
                    continue
                child_tid = pend.detail.get("sub_thread_id")
                child_skill = pend.detail.get("skill_id")
                if not (isinstance(child_tid, str) and isinstance(child_skill, str)):
                    continue
                child_items = await self._engine._load_thread_items(child_tid)
                rest = await descend(child_tid, child_items, depth - 1)
                if rest is not None:
                    return [(child_tid, child_skill, pend.related_call_id), *rest]
            return None  # 全部分支均不含 leaf

        rest = await descend(
            self._engine._thread_id, list(self._engine._history), self._engine._max_total_spawns_guard())
        if rest is None:
            return None
        return [(self._engine._thread_id, self._engine._entry_skill.id, None), *rest]

    def max_total_spawns_guard(self) -> int:
        """续跑链 DFS 下探的最大层数守卫(防坏数据成环)。

        必须低于 Python 默认递归限(1000):descend 系 async 递归逐帧压栈,守卫
        高于递归限时坏数据会先炸 RecursionError(create_task 中静默)而非由守卫
        终止。正常链深受 max_call_depth 约束(个位数),128 余量充足。
        """
        return 128

    @staticmethod
    def next_child_link(
        record: SuspensionRecord,
    ) -> tuple[str, str, str | None] | None:
        """从一个挂起 record 里取首个 CHILD_SKILL pending → (子tid, 子skill_id, 父callid)。

        正常单链下探每层至多一条 CHILD_SKILL pending（并发多子各自挂起属另一形态，
        本续跑链按"用户指向的 leaf"单路下探）。无 CHILD_SKILL pending → None。
        """
        from taifeng.suspend.reason import SuspendReason
        for p in record.pending:
            if p.reason is SuspendReason.CHILD_SKILL:
                tid = p.detail.get("sub_thread_id")
                skill_id = p.detail.get("skill_id")
                if isinstance(tid, str) and isinstance(skill_id, str):
                    return tid, skill_id, p.related_call_id
        return None

    async def resume_leaf_thread(
        self, sub: Submission, leaf_tid: str, leaf_skill_id: str,
        resolutions: dict[str, Any], root_cancel: CancellationToken,
        *, submission_id: str | None = None,
    ) -> str | None:
        """核销 leaf 子 thread 的用户挂起 + 续跑该子 turn，返回回传父的结果字符串。

        复用既有 resume 语义（SuspensionResolver + gap 补齐 + 续采样），但作用在
        【子 thread 的 load_thread 历史】而非 self._engine._history。

        Returns:
            子 turn 续跑后的 final_text（成功）/ 错误串（失败）；核销被拒或子又挂起 → None。
        """
        items = await self._engine._load_thread_items(leaf_tid)
        record = self._engine._find_active_suspension_in(items)
        if record is None:
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "no_active_suspension", "record_id": None, "detail": {}})))
            return None
        # 在飞守卫(与根路径同理):同 leaf record 并发 Resume 拒后到者
        if record.record_id in self._engine._resolving_records:
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "resolve_in_flight",
                      "record_id": record.record_id, "detail": {}})))
            return None
        self._engine._resolving_records.add(record.record_id)
        try:
            return await self._engine._resume_leaf_settled(
                sub, leaf_tid, leaf_skill_id, resolutions, record, root_cancel,
                submission_id=submission_id)
        finally:
            self._engine._resolving_records.discard(record.record_id)

    async def resume_leaf_settled(
        self, sub: Submission, leaf_tid: str, leaf_skill_id: str,
        resolutions: dict[str, Any], record: SuspensionRecord,
        root_cancel: CancellationToken, *, submission_id: str | None = None,
    ) -> str | None:
        """_resume_leaf_thread 的主体(在飞守卫占位后):配对 → 应用 → 结算 → 续跑。"""
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver

        # 到期哨兵与未核销 pending 求交;空 → 让位(已被并发人工核销)
        resolutions = self._engine._effective_resolutions(
            record, await self._engine._load_thread_items(leaf_tid), resolutions)
        if not resolutions:
            return None
        try:
            plan = SuspensionResolver().plan(record, resolutions)
        except ResolveError as e:
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": str(e), "record_id": record.record_id, "detail": {}})))
            return None

        # 补 gap（在子 thread 上）：form/data 直填、permission deny 填 error、allow 执行 tool
        await self._engine._apply_plan_on_thread(leaf_tid, leaf_skill_id, record, plan)
        # request 级核销:leaf record 仍有未核销 pending → 部分核销,不落 marker、
        # 不续跑 leaf(链中止,句柄/父层保持挂起等后续 Resume)
        items_after = await self._engine._load_thread_items(leaf_tid)
        remaining = [p for p in self._engine._unsettled_pendings(record, items_after)
                     if p.request_id not in resolutions]
        if remaining:
            await self._engine._emit(EventMsg(
                submission_id=sub.id,
                msg=SuspensionPartiallyResolved(data={
                    "record_id": record.record_id, "thread_id": leaf_tid,
                    "resolved_request_ids": sorted(resolutions.keys()),
                    "remaining_request_ids": sorted(
                        p.request_id for p in remaining)})))
            return None
        await self._engine._append_resolved_marker(leaf_tid, record.record_id)
        await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        auto_retries = self._engine._apply_plan_session_effects(plan, record)
        if plan.abort:
            # system_retry abort：子 turn 在挂起点终止，不续跑 → 视为失败回传父
            return f"sub_skill_aborted: {record.record_id}"
        outcome = await self._engine._run_thread_turn(
            sub, leaf_tid, leaf_skill_id, root_cancel, submission_id=submission_id,
            auto_retry_count=auto_retries)
        if outcome.end_reason == "suspended":
            # 子续跑又挂起：本层 emit 了 turn_suspended，续跑链中止（等下次 Resume）
            return None
        if outcome.end_reason == "cancelled":
            # 链取消(wave2b D5):向上只解链(回填 + 结算),不重跑任何上层
            return CHAIN_CANCELLED_RESULT
        return outcome.final_text if outcome.success else (
            f"sub_skill_failed: {outcome.error or outcome.end_reason}")

    async def resume_parent_level(
        self, sub: Submission, parent_tid: str, parent_skill_id: str,
        call_id: str | None, child_result: str, root_cancel: CancellationToken,
        *, submission_id: str | None = None,
    ) -> str | None:
        """回填父 thread 中 call_id 对应 call_skill 的 output，续跑父 turn。

        Returns:
            父 turn 续跑后的 final_text（需继续上溯时非 None）；父是根 / 父又挂起 → None。
        """
        is_root = parent_tid == self._engine._thread_id
        items = (list(self._engine._history) if is_root
                 else await self._engine._load_thread_items(parent_tid))
        record = self._engine._find_active_suspension_in(items)
        if record is None or call_id is None:
            logger.warning("child_resume: parent %s missing active CHILD_SKILL link",
                           parent_tid)
            return None
        # 回填父 call_skill 的 function_call_output（= 正常 run_sub_skill 的成功回传）
        is_error = (child_result.startswith("sub_skill_failed:")
                    or child_result.startswith("sub_skill_aborted:"))
        # 链取消解链(wave2b D5):本层照常回填 + 结算,但不重跑,哨兵继续向上传
        cancelled = child_result == CHAIN_CANCELLED_RESULT
        out = function_call_output(
            call_id=call_id, output=child_result,
            thread_id=parent_tid, is_error=is_error)
        # 1) 先回填本子的 fco(call_id 唯一,并发链各回各的,无冲突)
        if is_root:
            async with self._engine._lock:
                self._engine._history.append(out)
        await self._engine._store.append(out)

        # 2) record 级结算判定:per-record 锁串行化并发续跑链(双子同时 Resume/
        #    到期),判定基于锁内 fresh 状态——否则可能双双判 partial(无人续跑父)
        #    或双双 settle(双重续跑)
        async with self._engine._settle_lock(record.record_id):
            fresh = (list(self._engine._history) if is_root
                     else await self._engine._load_thread_items(parent_tid))
            active = self._engine._find_active_suspension_in(fresh)
            if active is None or active.record_id != record.record_id:
                # 并发链已抢先全量结算并续跑 → 本链到此为止(补显式事件)
                await self._engine._emit(EventMsg(
                    submission_id=sub.id, msg=SuspensionResolveRejected(data={
                        "reason": "superseded_by_concurrent_settlement",
                        "record_id": record.record_id, "detail": {}})))
                return CHAIN_CANCELLED_RESULT if cancelled else None
            remaining = self._engine._unsettled_pendings(record, fresh)
            if remaining:
                # request 级核销:仍有未核销 pending → 不落 marker、不续跑父 turn
                # (record 级 barrier,等错峰 Resume 结清)
                await self._engine._emit(EventMsg(
                    submission_id=sub.id,
                    msg=SuspensionPartiallyResolved(data={
                        "record_id": record.record_id, "thread_id": parent_tid,
                        "resolved_request_ids": [
                            p.request_id for p in record.pending
                            if p.related_call_id == call_id],
                        "remaining_request_ids": sorted(
                            p.request_id for p in remaining)})))
                return CHAIN_CANCELLED_RESULT if cancelled else None
            # 全量达成:锁内落 marker(并发链经 fresh 重读可见,不会二次结算)
            marker = system_injection(
                text=f"suspend_resolved:{record.record_id}",
                thread_id=parent_tid, source="suspend_resolved")
            if is_root:
                async with self._engine._lock:
                    self._engine._history.append(marker)
            await self._engine._store.append(marker)
        await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        if cancelled:
            # 用户已喊停:本层 gap 已回填 + 结算(根不再挂在 CHILD_SKILL 上),上层继续
            # 采样是 R4 违约 → 不重跑,哨兵继续向上,由链根终结 / 收敛句柄。
            return CHAIN_CANCELLED_RESULT
        if is_root:
            # 根：续跑(重入重放已回填的全部子输出);沿用链级登记项的注入队列引用
            turn_cancel = root_cancel.child(f"sub:{sub.id}")
            chain_pending = self._engine._pending.get(sub.id)
            self._engine._pending[sub.id] = _PendingTurn(
                submission_id=sub.id, cancel=turn_cancel,
                pending_input=(chain_pending.pending_input
                               if chain_pending is not None else []),
            )
            await self._engine._build_and_run_runner(
                sub.id, turn_cancel, list(self._engine._last_resolved or []))
            return None  # 根是终点，链结束
        # 非根祖先：续跑该祖先 turn
        outcome = await self._engine._run_thread_turn(
            sub, parent_tid, parent_skill_id, root_cancel,
            submission_id=submission_id)
        if outcome.end_reason == "suspended":
            return None
        if outcome.end_reason == "cancelled":
            # 中间层被取消:同样只解链不重跑(wave2b D5)
            return CHAIN_CANCELLED_RESULT
        return outcome.final_text if outcome.success else (
            f"sub_skill_failed: {outcome.error or outcome.end_reason}")

    async def build_spawn_resume_chain(
        self, root_tid: str, root_skill_id: str,
        resolutions: dict[str, Any] | None = None,
    ) -> list[tuple[str, str, str | None]] | None:
        """自 spawn 子 thread 沿 CHILD_SKILL pending 向下串到最深 leaf 的续跑链。

        与 ``_build_resume_chain``（自 engine 根下探到指定 leaf）的差异：detached spawn
        子 thread **不挂在 engine 根的 CHILD_SKILL 链上**（它是独立根），故续跑链须**以
        spawn 子 thread 为根**重建，并一路下探到「无 CHILD_SKILL pending」的那层（即真正
        持有用户 DATA/FORM 挂起的 leaf）——业务侧只知道 spawn 子 thread（SpawnSuspended.
        thread_id），不知道 leaf 具体是哪个，故按链下探自动定位。

        多 pending record(multi-pending-partial-resume):resolutions 非 None 时
        以「resolutions 的 request_id 子集落在哪层 record」为 leaf 判据 DFS 寻址
        (多个挂起分支错峰 Resume 时按提交内容精确定位);匹配不到则回退
        「首个可达的无 CHILD_SKILL 层」旧语义,核销不符由 ResolveError 显式拒绝。

        Returns:
            ``[(根tid, 根skill, None), …, (leaftid, leafskill, 父callid)]``；
            root_tid 自身无活跃挂起 → None（无可续）。
        """
        from taifeng.suspend.reason import SuspendReason

        async def descend(
            tid: str, depth: int, *, match: bool,
        ) -> list[tuple[str, str, str | None]] | None:
            """返回自 tid 之下到 leaf 的链段(不含 tid);本层即 leaf → []。"""
            items = await self._engine._load_thread_items(tid)
            record = self._engine._find_active_suspension_in(items)
            if record is None or depth <= 0:
                return None  # 无活跃挂起(死路/已核销分支)或超深度
            if (match and resolutions is not None
                    and set(resolutions) <= record.request_ids()):
                return []  # 本层 record 即裁决目标
            branches = [
                pend for pend in record.pending
                if pend.reason is SuspendReason.CHILD_SKILL
                and isinstance(pend.detail.get("sub_thread_id"), str)
                and isinstance(pend.detail.get("skill_id"), str)
            ]
            for pend in branches:
                child_tid = str(pend.detail["sub_thread_id"])
                rest = await descend(child_tid, depth - 1, match=match)
                if rest is not None:
                    return [(child_tid, str(pend.detail["skill_id"]),
                             pend.related_call_id), *rest]
            if not match and not branches:
                return []  # 旧语义:无 CHILD_SKILL pending 即 leaf
            return None

        guard = self._engine._max_total_spawns_guard()
        rest = await descend(root_tid, guard, match=True)
        if rest is None:
            # 回退旧语义(resolutions 与任何层都不符 → 让 leaf 层 ResolveError 显式拒)
            rest = await descend(root_tid, guard, match=False)
        if rest is None:
            return None
        return [(root_tid, root_skill_id, None), *rest]

    async def settle_call_skill_output(
        self, sub: Submission, thread_id: str, call_id: str, child_result: str
    ) -> str:
        """在 thread 上回填 call_id 对应 call_skill 的 function_call_output + 落 resolved-marker
        核销该层 CHILD_SKILL 挂起。

        供 spawn 嵌套续跑链在**重跑 spawn 子 thread 前**补齐其 call_skill gap（= 正常
        run_sub_skill 的成功回传）。与 ``_resume_parent_level`` 非根分支同语义，但**不**重跑
        该 thread（重跑交由 spawn 驱动经 ``_build_child_runner`` + ``_finalize_spawn`` 完成，
        以保持 detached 根语义 + 句柄终态回写 + join-barrier 触发）。

        Returns:
            "settled" 全量核销(可重跑)/ "partial" 部分核销(其余 pending 未结,
            句柄保持挂起)/ "missing" 该 thread 无活跃挂起(调用方拒绝,禁静默)。
        """
        items = await self._engine._load_thread_items(thread_id)
        record = self._engine._find_active_suspension_in(items)
        if record is None:
            return "missing"
        is_error = child_result.startswith(
            ("sub_skill_failed:", "sub_skill_aborted:"))
        out = function_call_output(
            call_id=call_id, output=child_result,
            thread_id=thread_id, is_error=is_error)
        await self._engine._store.append(out)
        # record 级结算判定(per-record 锁 + fresh 重读,与 _resume_parent_level 同理)
        async with self._engine._settle_lock(record.record_id):
            fresh = await self._engine._load_thread_items(thread_id)
            active = self._engine._find_active_suspension_in(fresh)
            if active is None or active.record_id != record.record_id:
                return "partial"  # 并发链已抢先结算 → 本链不再重跑
            remaining = self._engine._unsettled_pendings(record, fresh)
            if remaining:
                await self._engine._emit(EventMsg(
                    submission_id=sub.id,
                    msg=SuspensionPartiallyResolved(data={
                        "record_id": record.record_id, "thread_id": thread_id,
                        "resolved_request_ids": [
                            p.request_id for p in record.pending
                            if p.related_call_id == call_id],
                        "remaining_request_ids": sorted(
                            p.request_id for p in remaining)})))
                return "partial"
            await self._engine._append_resolved_marker(thread_id, record.record_id)
        await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id,
                  "request_ids": sorted(record.request_ids())})))
        return "settled"

    async def apply_plan_on_thread(
        self, thread_id: str, entry_skill_id: str,
        record: SuspensionRecord, plan: Any
    ) -> None:
        """在指定 thread 上应用 ResolvePlan 的 gap 补齐（form/data/deny/allow-execute）。

        与根路径 _handle_resume 第 3 步同语义，但作用在子 thread（落 store；子 turn
        续跑时由 load_thread 读回）。permission allow 走 _execute_resumed_tool_on_thread。
        entry_skill_id 用于该 thread 内执行被批准 tool 时构造 ToolContext 的 skill 上下文。
        """
        for call_id, payload in plan.direct_outputs.items():
            out = function_call_output(
                call_id=call_id, output=json.dumps(payload, ensure_ascii=False),
                thread_id=thread_id, is_error=False)
            await self._engine._store.append(out)
        for call_id, reason in plan.deny_outputs.items():
            out = function_call_output(
                call_id=call_id,
                output=self._engine._deny_output_text(record, call_id, reason),
                thread_id=thread_id, is_error=True)
            await self._engine._store.append(out)
        for call_id in plan.execute_tool_call_ids:
            await self._engine._execute_resumed_tool_on_thread(
                thread_id, entry_skill_id, call_id)
        # resolved-marker 不在此签发:request 级核销下由调用方在
        # 全部 pending 核销后经 _append_resolved_marker 落定(单一签发点)。

    async def append_resolved_marker(self, thread_id: str, record_id: str) -> None:
        """落 record 级 resolved-marker(request 级核销全量达成时的唯一非根签发点)。"""
        marker = system_injection(
            text=f"suspend_resolved:{record_id}",
            thread_id=thread_id, source="suspend_resolved")
        await self._engine._store.append(marker)

    async def run_thread_turn(
        self, sub: Submission, thread_id: str, entry_skill_id: str,
        root_cancel: CancellationToken, *, submission_id: str | None = None,
        auto_retry_count: int = 0,
    ) -> Any:
        """为指定（非根）thread 构造 TurnRunner 并续跑一轮，返回 TurnOutcome。

        history_buffer 从 load_thread 重建（含本次 resume 补的 gap）；entry_skill 由
        续跑链携带的 entry_skill_id 经 snapshot 解析（不依赖 store.get_metadata）。
        turn 内若再挂起会 emit turn_suspended 并落新 SuspensionRecord
        （续跑链据 end_reason=='suspended' 中止）。

        ``submission_id``：续跑事件的归因 submission。默认 None → 用本次 Resume 的
        ``sub.id``（根 thread 续跑链语义）。**spawn 嵌套续跑链**显式传 spawn 子 thread id：
        使被 spawn 的子任务 subtree 续跑事件归因到与首发一致的 child_thread（业务侧按
        submission_id 分轨，否则 leaf 子步文本会错挂到 Resume submission，丢轨）。
        """
        from taifeng.skill.dispatch import CallStack

        entry = self._engine._snapshot.get(entry_skill_id)
        if entry is None:
            raise RuntimeError(f"child_resume_entry_skill_missing: {entry_skill_id}")
        items = await self._engine._load_thread_items(thread_id)
        turn_cancel = root_cancel.child(f"sub:{sub.id}:thr:{thread_id}")
        # 子 thread 续跑必须标记为非根 turn（is_root=False，由 call_stack 非空判定）：
        # 否则其 turn_completed 会误带 is_root=True，业务桥接层会把子完成当成 submission
        # 终结。push 子 skill 自身一帧即可（栈非空 → run() 不再补 entry 帧）。
        sub_stack = CallStack().push(
            skill_id=entry.id, call_id=f"resume_{thread_id}")
        runner = TurnRunner(
            entry_skill=entry,
            snapshot=self._engine._snapshot,
            model_client=self._engine._model_client,
            tool_runtime=self._engine._tool_runtime,
            store=self._engine._store,
            compressors=self._engine._compressors,
            dispatch_policy=self._engine._dispatch_policy,
            outcome_judge=self._engine._outcome_judge,
            budget=self._engine._budget,
            thread_id=thread_id,
            submission_id=submission_id or sub.id,
            emit=self._engine._emit,
            cancel=turn_cancel,
            image_input_policy=self._engine._image_input_policy,
            input_cost_estimator=self._engine._input_cost_estimator,
            hooks=self._engine._hooks,
            script_executors=self._engine._script_executors,
            max_iterations=self._engine._max_iterations,
            denial_breaker_config=self._engine._denial_breaker_config,
            doom_loop_config=self._engine._doom_loop_config,
            failure_policy=self._engine._failure_policy,
            failure_suspend_ttl_seconds=self._engine._failure_suspend_ttl_seconds,
            failure_suspend_on_expire=self._engine._failure_suspend_on_expire,
            auto_retry_count=auto_retry_count,
            # K2 执法(suspend-review-fixes):leaf/父层续跑注入会话预算——
            # 增额后有执法;续跑用量不回写 engine 计量为既有缺口(文档声明)
            session_tokens_used=self._engine._session_tokens,
            max_session_tokens=self._engine._max_session_tokens,
            max_parallel_tool_calls=self._engine._max_parallel_tool_calls,
            sample_scope_id=sub.id,
            reasoning_passback=self._engine._reasoning_passback,
            enable_request_capture=self._engine._enable_request_capture,
            history_buffer=list(items),
            permission_policy=self._engine._permission_policy,
            request_metadata=self._engine._request_metadata,
            turn_index=self._engine._turn_index,
            capabilities=self._engine._capabilities,
            # T6: deferred 暴露阈值（驱动 child 列表 inline/deferred + 工具裁剪）
            recall_threshold=self._engine._recall_threshold,
            # 召回后端存在性：无后端恒 inline（与阈值同口径透传）
            has_recall_backend=self._engine._has_recall_backend,
            spawn_registry=self._engine._spawn_registry,
            memory_store=self._engine._memory_store,
            memory_query_builder=self._engine._memory_query_builder,
            pinned_states=self._engine._pinned_states,
            call_stack=sub_stack,
        )
        # 子 thread 续跑登记 _pending（is_root=False）：CancelTurn(sub.id) 才能触达（R4）。
        # 链级登记项(wave2b D5)可能已占同一 key:本层以自己的 turn token 遮蔽,退栈后
        # 还原——否则层与层之间的 CancelTurn 找不到目标,链在层间不可取消。
        pending_key = submission_id or sub.id
        outer = self._engine._pending.get(pending_key)
        self._engine._pending[pending_key] = _PendingTurn(
            submission_id=pending_key, cancel=turn_cancel, is_root=False,
        )
        try:
            return await runner.run()
        finally:
            if outer is not None:
                self._engine._pending[pending_key] = outer
            else:
                self._engine._pending.pop(pending_key, None)

    async def execute_resumed_tool_on_thread(
        self, thread_id: str, entry_skill_id: str, call_id: str
    ) -> None:
        """在指定 thread 上执行一个被批准的挂起 tool call，回填 function_call_output。

        与 _execute_resumed_tool 同语义（预批准 + dispatch + 回填），但作用在子 thread：
        从 load_thread 找原 function_call，落 output 到 store（子 turn 续跑时读回）。
        entry_skill_id 由续跑链携带（不依赖 store.get_metadata）。
        """
        from taifeng.tool.spec import ToolContext

        items = await self._engine._load_thread_items(thread_id)
        fc: ResponseItem | None = None
        for item in items:
            if item.kind == "function_call" and item.payload.get("call_id") == call_id:
                fc = item
        if fc is None:
            raise RuntimeError(f"resumed_tool_call_not_found: {call_id}@{thread_id}")
        name = fc.payload["name"]
        # 与派发层同一解析入口:坏参数不退化为 {} 执行(下方按 args_error 结算)
        args, args_error = parse_tool_arguments(fc.payload.get("arguments") or "{}")
        entry = self._engine._snapshot.get(entry_skill_id) or self._engine._entry_skill
        cancel = self._engine._resume_tool_cancel(call_id)
        ctx = ToolContext(
            call_id=call_id, cancel=cancel, thread_id=thread_id,
            extras={
                "skill_snapshot": self._engine._snapshot,
                "visible_skills": self._engine._snapshot.reachable_from(entry.id),
                "dispatch_policy": self._engine._dispatch_policy,
                "outcome_judge": self._engine._outcome_judge,
                "current_skill": entry,
                "entry_skill_id": entry.id,
                "permission_policy": self._engine._permission_policy,
                "hook_runner": self._engine._hooks,
                "request_metadata": self._engine._request_metadata,
                "turn_index": self._engine._turn_index,
                "script_executors": self._engine._script_executors,
            },
        )
        if args_error is not None:
            # 参数非法 → 不执行 handler,以 invalid_arguments error 结算(同派发层规则)
            result = ToolResult.error(
                f"invalid_arguments: {args_error}", reason="invalid_arguments"
            )
        else:
            if self._engine._permission_policy is not None:
                self._engine._permission_policy.preapprove(call_id)
            result = await self._engine._tool_runtime.dispatch(
                name=name, arguments=args, ctx=ctx
            )
        out = function_call_output(
            call_id=call_id, output=result.output,
            thread_id=thread_id, is_error=result.is_error)
        await self._engine._store.append(out)
