"""挂起访问与已批准工具执行：thread 逻辑历史读取 / 活跃挂起定位 / 核销副作用 / 手动压缩。

从 ``child_resume_chain.py`` 二次拆出（Wave 4）——这组 helper 并不属于 call_skill
子链本身，而是被 ``spawn_driver`` / ``spawn_barrier`` / ``spawn_rewind`` /
``peer_mailbox`` / ``spawn_resume`` 与子链**共同**依赖的挂起访问层，混在子链里既让
该文件超 800 行红线，也掩盖了这条依赖边界。

按 `engine-module-structure` 契约落为协作者类：自身无状态，运行态经 engine 引用
访问；兄弟调用一律经 ``self._engine._x(...)`` 回弹，保住白盒注入点。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.models import function_call_output
from taifeng.loop.tool_batch import parse_tool_arguments
from taifeng.tool.spec import ToolResult

import asyncio
from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import ResponseItem, system_injection
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EngineLog, EventMsg
from taifeng.loop.rewind import derive_rewind_log
from taifeng.loop.submission import CompactNow
from taifeng.loop.turn import TurnRunner
from taifeng.suspend.record import SuspensionRecord
from typing import Any

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine


class SuspensionAccess:
    """挂起访问层协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供 store / history / 挂起记录与锁表。
        """
        self._engine = engine

    async def load_thread_items(self, thread_id: str) -> list[ResponseItem]:
        """非根 thread 的**逻辑 history 单一入口**:load_thread → reconstruct。

        store 是 append-only 转录(压缩占位追加在尾、rewind 只落 marker),直接拿
        raw 当 history 会把被替换原文 / 被截断旧圈重新塞回 prompt,冷推断也会据
        废弃项误判(wave2b 复现 a / f)。所有子 thread 的重载 / 推断 / 路由 / TTL
        活跃性验证都经此取逻辑 history;``reconstruct_logical_history`` 对逻辑
        history 是恒等映射,调用方不必也不应再次 reconstruct。
        """
        raw = [it async for it in await self._engine._store.load_thread(thread_id)]
        return reconstruct_logical_history(raw)

    async def cancel_active_suspension(
        self, cancel_sub_id: str, target_sub_id: str
    ) -> None:
        """R4：若存在 submission_id 匹配的活跃挂起，追加 resolved-marker 丢弃之。

        与 _handle_resume 第 4 步同机制（同 marker text 格式 'suspend_resolved:<id>'），
        保证两条丢弃路径被 _find_active_suspension 一致识别。丢弃后该 record 不再被
        _find_active_suspension 返回，后续 Resume 命中 no_active_suspension 被拒。
        无匹配挂起则 no-op（保持 CancelTurn 既有宽容语义：找不到目标不报错）。

        参数：
            cancel_sub_id: 本次 CancelTurn submission 的 id（EngineLog 归属）。
            target_sub_id: CancelTurn 要取消的目标 submission id（挂起 turn 的 sub）。
        副作用：向 history + store 追加一条 resolved-marker；emit 一条 EngineLog。
        """
        record = self._engine._find_active_suspension()
        # 仅当存在活跃挂起且其 submission_id 与取消目标一致时才丢弃
        if record is None or record.submission_id != target_sub_id:
            return
        marker = system_injection(
            text=f"suspend_resolved:{record.record_id}",
            thread_id=self._engine._thread_id,
            source="suspend_resolved",
        )
        async with self._engine._lock:
            self._engine._history.append(marker)
        await self._engine._store.append(marker)
        await self._engine._emit(
            EventMsg(
                submission_id=cancel_sub_id,
                msg=EngineLog(
                    data={
                        "level": "info",
                        "message": (
                            f"cancelled suspended turn {target_sub_id} "
                            f"(record {record.record_id})"
                        ),
                        "extra": {},
                    }
                ),
            )
        )

    def find_active_suspension(self) -> SuspensionRecord | None:
        """扫 self._engine._history，返回最后一条尚未被 resolved-marker 消费的 suspension record。

        resolved-marker = source=='suspend_resolved' 的 system_injection，
        其 text 形如 'suspend_resolved:<record_id>'。
        """
        return self._engine._find_active_suspension_in(self._engine._history)

    @staticmethod
    def find_active_suspension_in(
        items: list[ResponseItem],
    ) -> SuspensionRecord | None:
        """在任意 items 序列中找最后一条未被 resolved-marker 消费的 suspension record。

        从 _find_active_suspension 泛化而来 —— 子 thread resume 时对【子 thread 的
        load_thread 结果】复用同一识别逻辑（resolved-marker 同 text 格式）。

        Args:
            items: 一个 thread 的 ResponseItem 序列（根 = engine._history；子 = load_thread）。
        Returns:
            活跃挂起的 SuspensionRecord；无挂起或已被核销 → None。
        """
        resolved_ids: set[str] = set()
        last_suspension: ResponseItem | None = None
        for item in items:
            if item.kind == "system_injection" and item.payload.get("source") == "suspend_resolved":
                rid = (item.payload.get("text") or "").removeprefix("suspend_resolved:")
                resolved_ids.add(rid)
            elif item.kind == "suspension":
                last_suspension = item
        if last_suspension is None:
            return None
        record = SuspensionRecord.from_item(last_suspension)
        if record.record_id in resolved_ids:
            return None
        return record

    @staticmethod
    def deny_output_text(
        record: SuspensionRecord, call_id: str, reason_text: str,
    ) -> str:
        """按 pending reason 渲染 deny 回填文案(suspension-ttl-hardening)。

        PERMISSION → ``permission_denied: ...``(用户拒绝语义不变);
        其余(DATA/FORM/CHILD_SKILL 的到期 abort)→ ``suspension_expired: ...``——
        数据问询超时不再被模型/业务误读为权限拒绝。
        """
        from taifeng.suspend.reason import SuspendReason

        pend = next(
            (p for p in record.pending if p.related_call_id == call_id), None)
        if pend is not None and pend.reason is not SuspendReason.PERMISSION:
            return f"suspension_expired: {reason_text}"
        return f"permission_denied: {reason_text}"

    def apply_plan_session_effects(self, plan: Any, record: SuspensionRecord) -> int:
        """应用 ResolvePlan 的会话级副作用,返回续跑 runner 的 auto_retry_count。

        - K2 retry 增额:extend_session_tokens > 0 → 抬升 `_max_session_tokens`
          (显式抬顶;触顶条件随之清除,retry 真实有效)。
        - 谱系计数:plan 来自 TTL 到期自动 retry(expired_retry)→ 续跑计数 =
          record 内既有计数 + 1(人工 Resume 恒 0,不计数)。
        """
        if (getattr(plan, "extend_session_tokens", 0)
                and self._engine._max_session_tokens is not None):
            self._engine._max_session_tokens += plan.extend_session_tokens
        if not getattr(plan, "expired_retry", False):
            return 0
        prior = max(
            (int(p.detail.get("auto_retry_count", 0) or 0) for p in record.pending),
            default=0)
        return prior + 1

    def effective_resolutions(
        self, record: SuspensionRecord, items: list[ResponseItem],
        resolutions: dict[str, Any],
    ) -> dict[str, Any]:
        """到期哨兵 resolutions 与未核销 pending 求交;人工 payload 原样返回。

        fire 快照与哨兵 Resume 实际处理之间存在陈旧窗口:期间被人工部分核销的
        pending 再收哨兵会对已配对 call_id 重复落 deny fco(suspend-review-fixes)。
        仅对**纯哨兵**提交过滤(内核签发形态);空交集 → 调用方 no-op 让位。
        """
        from taifeng.suspend.resolver import EXPIRE_SENTINEL

        if not resolutions or not all(
            isinstance(v, dict) and v.get(EXPIRE_SENTINEL) is True
            for v in resolutions.values()
        ):
            return dict(resolutions)
        unsettled = {p.request_id for p in self._engine._unsettled_pendings(record, items)}
        return {rid: v for rid, v in resolutions.items() if rid in unsettled}

    def settle_lock(self, record_id: str) -> asyncio.Lock:
        """取 record 级结算锁(惰性创建;record 终结后残留的空锁可忽略不计)。"""
        lock = self._engine._settle_locks.get(record_id)
        if lock is None:
            lock = asyncio.Lock()
            self._engine._settle_locks[record_id] = lock
        return lock

    @staticmethod
    def unsettled_pendings(
        record: SuspensionRecord, items: list[ResponseItem],
    ) -> list[Any]:
        """返回 record 中尚未核销的 pending(request 级核销的推导真相,R5)。

        判据:pending 的 related_call_id 在该 record 的 suspension item **之后**
        已有配对 function_call_output 即视为已核销(gap 回填即核销凭据)——
        以 suspension item 为锚而非全量扫描,避免历史轮次同 call_id(编排合成
        call_id 跨 turn 相同)误判。related_call_id=None 的 pending(护栏挂起,
        设计上独占 record)无 fco 凭据,恒视为未核销(由整批裁决一次性结算)。
        """
        pos = -1
        for i, it in enumerate(items):
            if (it.kind == "suspension"
                    and it.payload.get("record_id") == record.record_id):
                pos = i
        settled_call_ids = {
            str(it.payload.get("call_id")) for it in items[pos + 1:]
            if it.kind == "function_call_output"
        }
        return [p for p in record.pending
                if p.related_call_id is None
                or p.related_call_id not in settled_call_ids]

    async def execute_resumed_tool(self, call_id: str) -> None:
        """resume 时对一个被批准的挂起 tool call 真正执行，回填 function_call_output。

        从 history 找到该 call_id 的 function_call（取 name + arguments）→ 经
        tool_runtime.dispatch 执行 → 追加 function_call_output。

        Args:
            call_id: permission allow 后需真正执行的挂起 tool call id。

        Raises:
            RuntimeError: history 中找不到该 call_id 的 function_call（断点不一致）。
        """
        from taifeng.tool.spec import ToolContext

        # 找原 function_call（取最后一条匹配，与 turn.py 落盘序一致）
        fc: ResponseItem | None = None
        for item in self._engine._history:
            if item.kind == "function_call" and item.payload.get("call_id") == call_id:
                fc = item
        if fc is None:
            raise RuntimeError(f"resumed_tool_call_not_found: {call_id}")
        name = fc.payload["name"]
        # 与派发层同一解析入口:坏参数不退化为 {} 执行(下方按 args_error 结算)
        args, args_error = parse_tool_arguments(fc.payload.get("arguments") or "{}")
        # 构造 ToolContext：resume 续跑发生在 engine 层（无 TurnRunner），extras 提供
        # 工具运行所需的最小上下文（snapshot / 可见 skill / 权限策略 / 元数据）。
        # 关键：permission_policy 不再注入 ask prompter 的挂起语义——本次执行是"已批准"
        # 的二次放行，工具内若再次走 check 应按业务策略放行（业务侧据 resolutions 调整）。
        cancel = self._engine._resume_tool_cancel(call_id)
        ctx = ToolContext(
            call_id=call_id,
            cancel=cancel,
            thread_id=self._engine._thread_id,
            extras={
                "skill_snapshot": self._engine._snapshot,
                "visible_skills": self._engine._snapshot.reachable_from(self._engine._entry_skill.id),
                "dispatch_policy": self._engine._dispatch_policy,
                "outcome_judge": self._engine._outcome_judge,
                "current_skill": self._engine._entry_skill,
                "entry_skill_id": self._engine._entry_skill.id,
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
            # resume：人类已批准该挂起 call → 预批准，避免重跑时再次触发 prompter（防无限挂起）
            if self._engine._permission_policy is not None:
                self._engine._permission_policy.preapprove(call_id)
            result = await self._engine._tool_runtime.dispatch(
                name=name, arguments=args, ctx=ctx
            )
        out = function_call_output(
            call_id=call_id, output=result.output,
            thread_id=self._engine._thread_id, is_error=result.is_error)
        async with self._engine._lock:
            self._engine._history.append(out)
        await self._engine._store.append(out)

    async def run_compact_now(
        self,
        submission_id: str,
        op: CompactNow,
        root_cancel: CancellationToken,
    ) -> None:
        if self._engine._compressors is None:
            await self._engine._emit(
                EventMsg(
                    submission_id=submission_id,
                    msg=EngineLog(
                        data={
                            "level": "warn",
                            "message": "compactor not configured",
                            "extra": {},
                        }
                    ),
                )
            )
            return
        # 若 op 提供了临时 budget 覆盖，用临时 budget；否则用 engine budget
        budget = self._engine._budget
        if op.target_tokens is not None or op.preserve_tail is not None:
            budget = ContextBudget(
                context_window=self._engine._budget.context_window,
                soft_limit_ratio=(
                    op.target_tokens / max(self._engine._budget.context_window, 1)
                    if op.target_tokens is not None
                    else self._engine._budget.soft_limit_ratio
                ),
                hard_limit_ratio=self._engine._budget.hard_limit_ratio,
                preserve_tail_messages=(
                    op.preserve_tail
                    if op.preserve_tail is not None
                    else self._engine._budget.preserve_tail_messages
                ),
            )

        cancel = root_cancel.child(f"sub:{submission_id}")
        runner = TurnRunner(
            entry_skill=self._engine._entry_skill,
            snapshot=self._engine._snapshot,
            model_client=self._engine._model_client,
            tool_runtime=self._engine._tool_runtime,
            store=self._engine._store,
            compressors=self._engine._compressors,
            dispatch_policy=self._engine._dispatch_policy,
            outcome_judge=self._engine._outcome_judge,
            budget=budget,
            thread_id=self._engine._thread_id,
            submission_id=submission_id,
            emit=self._engine._emit,
            cancel=cancel,
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
            max_parallel_tool_calls=self._engine._max_parallel_tool_calls,
            reasoning_passback=self._engine._reasoning_passback,
            enable_request_capture=self._engine._enable_request_capture,
            history_buffer=list(self._engine._history),
            cache_anchor_index=self._engine._cache_anchor_index,
            compaction_count=self._engine._compaction_count,
            pinned_states=self._engine._pinned_states,
            # T6: 一致性透传（CompactNow runner 不采样，阈值无实效但保字段齐整）
            recall_threshold=self._engine._recall_threshold,
            has_recall_backend=self._engine._has_recall_backend,
        )
        await runner._maybe_compress(phase="manual", force=op.force)  # noqa: SLF001
        async with self._engine._lock:
            self._engine._history = list(runner.history_buffer)
            self._engine._cache_anchor_index = runner.cache_anchor_index
            # 与 _writeback_turn_runner 同步回写压缩计数，否则 G1c 降级告警跨 turn 少计
            self._engine._compaction_count = runner.compaction_count
            # turn-rewind：对当前全量逻辑 history 重算节点表(derive 为唯一产出方)。
            # CompactNow 路径：在压缩后 history 上重算，折叠语义与冷加载推导一致。
            self._engine._rewind_checkpoints = derive_rewind_log(self._engine._history)

    # -----------------------------------------------------------------
