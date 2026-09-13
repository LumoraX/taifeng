"""engine 根 thread Resume：配对 resolutions 核销 / 补齐 history gap / 续采样

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `engine-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 engine 唯一持有。

**兄弟调用一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，兄弟模块
与测试按原名调用/打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.models import function_call_output, system_injection
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.event import (
    EventMsg,
    SuspensionPartiallyResolved,
    SuspensionResolved,
    SuspensionResolveRejected,
)
from taifeng.loop.submission import Resume, Submission

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.engine import AgentEngine
    from taifeng.suspend.record import SuspensionRecord


class EngineResume:
    """engine 根 thread Resume协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供运行态与共享依赖。
        """
        self._engine = engine

    async def handle_resume(self, sub: Submission, root_cancel: CancellationToken) -> None:
        """续跑一个挂起的 thread：配对 resolutions → 补齐 history gap → 续采样。

        步骤：
          1. 在 self._engine._history 找"活跃挂起"（最后一条 kind=='suspension' 且其 record_id
             尚未被 resolved-marker 标记消费）。找不到 → SuspensionResolveRejected 返回。
          2. SuspensionRecord.from_item 还原；SuspensionResolver().plan(record, resolutions)。
             ResolveError → SuspensionResolveRejected(reason=str(e)) 返回（禁静默）。
          3. 应用 plan：回填 function_call_output(form/data/deny)、执行 tool(permission allow)。
          4. 落 resolved-marker（system_injection source='suspend_resolved'）标记消费（幂等）。
          5. emit SuspensionResolved。
          6. 非 abort → _build_and_run_runner 续采样；abort → 不续跑（turn 终止）。
        """
        assert isinstance(sub.op, Resume)
        op = sub.op

        # 子 thread resume：Resume.thread_id 指向 call_skill 派发的子 thread（≠ 根 thread）。
        # 挂起记录落在子 thread，根 self._engine._history 找不到 → 走专门的续跑链（先续跑子 thread
        # 拿结果，再逐层回填父 call_skill 的 output，最终根 turn 续跑完成）。
        if op.thread_id != self._engine._thread_id:
            await self._engine._handle_child_resume(sub, op, root_cancel)
            return

        # 1. 找活跃挂起 record（扫 history：最后一条未被 resolved-marker 消费的 suspension）
        record = self._engine._find_active_suspension()
        if record is None:
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "no_active_suspension", "record_id": None, "detail": {}})))
            return

        # 1.5 在飞守卫:同 record 已有 Resume 在处理(marker 未落)→ 显式拒绝,
        # 防双裁决(同 call_id 双 fco、双 marker、双续跑)
        if record.record_id in self._engine._resolving_records:
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": "resolve_in_flight",
                      "record_id": record.record_id, "detail": {}})))
            return
        self._engine._resolving_records.add(record.record_id)
        try:
            await self._engine._handle_resume_resolved(sub, op, record, root_cancel)
        finally:
            self._engine._resolving_records.discard(record.record_id)

    async def handle_resume_resolved(
        self, sub: Submission, op: Resume,
        record: SuspensionRecord, root_cancel: CancellationToken,
    ) -> None:
        """_handle_resume 的主体(在飞守卫占位后):配对 → 应用 → 结算 → 续跑。"""
        # 1.6 到期哨兵与未核销 pending 求交(陈旧快照不重复回填);空 → 让位
        resolutions = self._engine._effective_resolutions(
            record, list(self._engine._history), op.resolutions)
        if not resolutions:
            return
        # 2. 配对 + 计划（ResolveError 显式拒绝，不静默兜底）
        from taifeng.suspend.resolver import ResolveError, SuspensionResolver
        try:
            plan = SuspensionResolver().plan(record, resolutions)
        except ResolveError as e:
            await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolveRejected(
                data={"reason": str(e), "record_id": record.record_id, "detail": {}})))
            return

        # 3. 应用 plan：补齐 history gap（挂起点的 function_call 缺 function_call_output）
        import json
        async with self._engine._lock:
            # 3a. form/data 直接回填 output（payload 即工具结果，JSON 序列化）
            for call_id, payload in plan.direct_outputs.items():
                out = function_call_output(
                    call_id=call_id, output=json.dumps(payload, ensure_ascii=False),
                    thread_id=self._engine._thread_id, is_error=False)
                self._engine._history.append(out)
                await self._engine._store.append(out)
            # 3b. deny / 到期 → error output(前缀按 pending reason 渲染)
            for call_id, reason in plan.deny_outputs.items():
                out = function_call_output(
                    call_id=call_id,
                    output=self._engine._deny_output_text(record, call_id, reason),
                    thread_id=self._engine._thread_id, is_error=True)
                self._engine._history.append(out)
                await self._engine._store.append(out)
        # 3c. permission allow → 真正执行 tool（复用 runtime，不绕 RwLock）
        for call_id in plan.execute_tool_call_ids:
            await self._engine._execute_resumed_tool(call_id)

        # 3.5 + 4. record 级结算判定(per-record 锁串行化并发 Resume)+ 落 marker:
        # 仍有未核销 pending → 部分核销,不落 marker、不续跑(record 级 barrier)
        async with self._engine._settle_lock(record.record_id):
            active = self._engine._find_active_suspension()
            if active is None or active.record_id != record.record_id:
                # 并发 Resume 已抢先全量结算:补显式事件(消除观测空洞)
                await self._engine._emit(EventMsg(
                    submission_id=sub.id, msg=SuspensionResolveRejected(data={
                        "reason": "superseded_by_concurrent_settlement",
                        "record_id": record.record_id, "detail": {}})))
                return
            remaining = [
                p for p in self._engine._unsettled_pendings(record, list(self._engine._history))
                if p.request_id not in resolutions]
            if remaining:
                await self._engine._emit(EventMsg(
                    submission_id=sub.id,
                    msg=SuspensionPartiallyResolved(data={
                        "record_id": record.record_id, "thread_id": self._engine._thread_id,
                        "resolved_request_ids": sorted(resolutions.keys()),
                        "remaining_request_ids": sorted(
                            p.request_id for p in remaining)})))
                return
            marker = system_injection(
                text=f"suspend_resolved:{record.record_id}",
                thread_id=self._engine._thread_id, source="suspend_resolved")
            async with self._engine._lock:
                self._engine._history.append(marker)
            await self._engine._store.append(marker)

        # 5. emit resolved
        await self._engine._emit(EventMsg(submission_id=sub.id, msg=SuspensionResolved(
            data={"record_id": record.record_id, "request_ids": sorted(record.request_ids())})))

        # 6. 续跑（abort 则不续；turn 已在挂起点终止，gap 已补齐即收尾）
        auto_retries = self._engine._apply_plan_session_effects(plan, record)
        if plan.abort:
            return
        # Resume 续跑同样过 K2 闸门(resource-limit-retry-semantics):未经增额的
        # 续跑在会话已触顶时不得静默烧 token——按 policy 再裁决(挂起 / 终态)
        if await self._engine._gate_session_tokens(sub.id):
            return
        turn_cancel = root_cancel.child(f"sub:{sub.id}")
        self._engine._pending[sub.id] = _PendingTurn(submission_id=sub.id, cancel=turn_cancel)
        await self._engine._build_and_run_runner(
            sub.id, turn_cancel, list(self._engine._last_resolved or []),
            auto_retry_count=auto_retries)
