"""engine turn runner：构造 / 残留注入排空 / 回写 / 构建并跑 / post-turn 钩子

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `engine-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 engine 唯一持有。

**兄弟调用一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，兄弟模块
与测试按原名调用/打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.instructions.types import ResolvedInstruction
from taifeng.loop.audit_history import (
    AuditedHistoryConflictError,
    audited_history_conflict_failure,
    merge_audited_history,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.engine_types import _PendingTurn
from taifeng.loop.event import EventMsg, PostTurnHookFired
from taifeng.loop.injection import injection_event
from taifeng.loop.rewind import derive_rewind_log
from taifeng.loop.turn import TurnOutcome, TurnRunner

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine


class EngineRunner:
    """engine turn runner协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供运行态与共享依赖。
        """
        self._engine = engine

    def active_root_pending(self) -> _PendingTurn | None:
        """返回当前根 thread 在飞 turn 的 pending 记录（无则 None）。

        `_pending` 也会登记子 thread 续跑 turn，这里只认 is_root 的那一个。
        """
        for pending in self._engine._pending.values():
            if pending.is_root:
                return pending
        return None

    async def drain_residual_injections(
        self, runner: TurnRunner, submission_id: str,
    ) -> None:
        """turn 退出后把 runner 未消费的 pending 注入并入 buffer + store（R5）。

        正常路径 runner 已在迭代边界 drain 完毕，这里见空列表直接返回；取消 / 异常
        路径才有残留。事件与 runner 侧同形，但 delivered=False + reason=turn_ended，
        让宿主知道这段文本没有进入本 turn 的 prompt。
        """
        residual = list(runner.pending_input)
        runner.pending_input.clear()
        for item in residual:
            runner.history_buffer.append(item)
            await self._engine._store.append(item)
            await self._engine._emit(
                EventMsg(
                    submission_id=submission_id,
                    msg=injection_event(
                        item, submission_id, delivered=False, reason="turn_ended",
                    ),
                )
            )

    async def writeback_turn_runner(self, runner: TurnRunner) -> None:
        """完整验证 audited history 后原子回写 runner 派生状态。"""
        async with self._engine._lock:
            runner_history = list(runner.history_buffer)
            if self._engine._audit_state is None:
                self._engine._history = runner_history
            else:
                try:
                    merged_history = merge_audited_history(
                        self._engine._history,
                        runner_history,
                    )
                except AuditedHistoryConflictError:
                    raise self._engine._audit_state.coordinator.freeze(
                        audited_history_conflict_failure()
                    ) from None
                self._engine._history = merged_history
            self._engine._cache_anchor_index = runner.cache_anchor_index
            self._engine._rewind_checkpoints = derive_rewind_log(self._engine._history)
            self._engine._last_prompt_fingerprint = runner.last_prompt_fingerprint
            self._engine._compaction_count = runner.compaction_count
            self._engine._session_tokens += runner.total_usage.total_tokens

    async def build_and_run_runner(
        self,
        submission_id: str,
        turn_cancel: CancellationToken,
        resolved_for_turn: list[ResolvedInstruction],
        *,
        seed_pending_call_id: str | None = None,
        cache_break_expected_reason: str | None = None,
        auto_retry_count: int = 0,
    ) -> None:
        """构造并运行一轮，最后一次性回写 Engine 状态。"""
        runner = self._engine._new_turn_runner(
            submission_id,
            turn_cancel,
            resolved_for_turn,
            auto_retry_count=auto_retry_count,
        )
        # turn-rewind retry_tool：让 runner 采样前先补跑被保留的悬空 call
        runner._seed_pending_call_id = seed_pending_call_id  # noqa: SLF001
        # turn-rewind R2：rewind 蓄意回退 anchor → 首采样的 cache 失效记为 expected
        if cache_break_expected_reason is not None:
            runner._next_cache_break_expected = True  # noqa: SLF001
            runner._next_cache_break_reason = cache_break_expected_reason  # noqa: SLF001
        # post_turn 钩子需用「本 turn 的 index」(= +1 之前的值,与同 turn 的
        # pre_turn iteration 对齐),故在 finally 自增前先捕获。
        fired_iteration = runner.turn_index
        try:
            outcome = await runner.run()
            if self._engine._audit_state is not None:
                self._engine._audit_state.coordinator.record_target_outcome(
                    submission_id,
                    outcome.end_reason,
                )
        finally:
            # runner 因取消 / 异常退出时 pending 队列可能仍有未消费注入：
            # 在回写之前并入 runner buffer + store，不丢（R5），事件报 delivered:false。
            await self._engine._drain_residual_injections(runner, submission_id)
            self._engine._pending.pop(submission_id, None)
            self._engine._turn_index += 1

        await self._engine._writeback_turn_runner(runner)
        await self._engine._fire_post_turn_hook(
            submission_id, outcome, turn_cancel, fired_iteration,
        )

    async def fire_post_turn_hook(
        self,
        submission_id: str,
        outcome: TurnOutcome,
        turn_cancel: CancellationToken,
        iteration: int,
    ) -> None:
        """root turn 真终态时同步触发 post_turn 钩子(审计型,不可否决)。

        触发点在 turn 状态回写之后、下一 turn 启动之前 —— 给宿主「下一轮前必须
        完成」的顺序保证(self-review / 记忆固化等认知回路落脚点)。仅当注册了
        post_turn 钩子时才执行(常见路径零开销)。

        门控:
          - 挂起(suspended)= 暂停等 Resume —— 续跑到真终态才触发,此刻不触发;
          - 取消(cancelled)= teardown —— 不触发(与 R4 可取消语义一致)。
        R4:经 ``ctx.extras["cancel"]`` 把本 turn 的 CancellationToken 交给钩子;
        审计型经 ``run_audit_only`` 触发(deny / 异常都不改变已终结的 turn)。
        """
        if self._engine._hooks is None:
            return
        if outcome.end_reason in ("suspended", "cancelled"):
            return
        handlers = self._engine._hooks.registry.handlers("post_turn")
        if not handlers:
            return
        from taifeng.hooks.types import HookContext, PostTurnHook
        await self._engine._hooks.run_audit_only(
            "post_turn",
            PostTurnHook(
                end_reason=outcome.end_reason,
                success=outcome.success,
                final_text=outcome.final_text,
                iteration=iteration,
            ),
            HookContext(
                thread_id=self._engine._thread_id,
                submission_id=submission_id,
                entry_skill_id=self._engine._entry_skill.id,
                extras={"cancel": turn_cancel},
            ),
        )
        await self._engine._emit(EventMsg(
            submission_id=submission_id,
            msg=PostTurnHookFired(data={
                "end_reason": outcome.end_reason,
                "iteration": iteration,
                "hook_count": len(handlers),
            }),
        ))

    # -----------------------------------------------------------------
