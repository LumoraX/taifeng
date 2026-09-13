"""turn 上下文装载：记忆预取回写 / pre-evict 抢救 / pinned state 重注 / 预算提示

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__ctxload_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from taifeng.context.budget import estimate_history_tokens
from taifeng.context.budget_hint import evaluate_budget_hint, render_budget_hint
from taifeng.conversation.models import ResponseItem
from taifeng.loop.event import BudgetHintInjected, EngineLog, PinnedStateReinjected
from taifeng.loop.turn_helpers import _latest_user_text

if TYPE_CHECKING:
    from taifeng.loop.turn import TurnRunner

logger = logging.getLogger(__name__)


class TurnContextLoad:
    """turn 上下文装载协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__ctxload_owner = owner

    async def prefetch_memory(self) -> None:
        """K3 page-in：按最近用户消息 prefetch 长期记忆 → ``_prefetched_memory``。

        best-effort：``memory_store`` 抛异常被吞掉（内存层不得打断 turn）。
        """
        if self.__ctxload_owner.memory_store is None:
            return
        # 默认检索 query = 最近一条用户消息文本（与 current_task 注入同源）
        query = _latest_user_text(self.__ctxload_owner.history_buffer)
        # 业务侧 query 构造器：拿 history 拷贝自由组装检索语境（如近 N 轮拼接）。
        # 崩溃回退默认构造——prefetch 全链 best-effort，builder 故障不应使
        # 长期记忆整体失效（有日志，非静默）。
        if self.__ctxload_owner.memory_query_builder is not None:
            try:
                query = str(self.__ctxload_owner.memory_query_builder(list(self.__ctxload_owner.history_buffer)))
            except Exception:
                logger.exception(
                    "memory_query_builder failed (fallback to default query)")
        try:
            text = await self.__ctxload_owner.memory_store.prefetch(query, thread_id=self.__ctxload_owner.thread_id)
        except Exception:
            logger.exception("memory prefetch failed (ignored)")
            return
        self.__ctxload_owner._prefetched_memory = text or ""

    async def writeback_memory(self, new_items: list[ResponseItem]) -> None:
        """K3 dirty-page 写回：本 turn 新增 items 异步写回长期存储。best-effort。"""
        if self.__ctxload_owner.memory_store is None or not new_items:
            return
        try:
            await self.__ctxload_owner.memory_store.writeback(
                thread_id=self.__ctxload_owner.thread_id, items=list(new_items)
            )
        except Exception:
            logger.exception("memory writeback failed (ignored)")

    async def apply_pre_evict_salvage(
        self,
        before: list[ResponseItem],
        after: list[ResponseItem],
        summary_item_id: str | None,
    ) -> list[ResponseItem]:
        """K3 swap-out：把 before 中被换出（不在 after）的 items 交给 memory
        持久化；返回 digest 作为 system_injection 插在 summary 之后。

        无 memory_store / 无换出 / digest 为空 → 原样返回 after。best-effort。
        """
        if self.__ctxload_owner.memory_store is None:
            return after
        after_ids = {it.id for it in after}
        evicted = [it for it in before if it.id not in after_ids]
        if not evicted:
            return after
        try:
            digest = (await self.__ctxload_owner.memory_store.on_pre_evict(evicted)) or ""
        except Exception:
            logger.exception("memory on_pre_evict failed (ignored)")
            return after
        if not digest:
            return after
        from taifeng.conversation.models import system_injection

        note = system_injection(
            digest, thread_id=self.__ctxload_owner.thread_id, source="memory_pre_evict"
        )
        new_history = list(after)
        insert_at = len(new_history)
        if summary_item_id:
            for i, it in enumerate(new_history):
                if it.id == summary_item_id:
                    insert_at = i + 1
                    break
        new_history.insert(insert_at, note)
        await self.__ctxload_owner.store.append(note)
        return new_history

    async def reinject_pinned_state(
        self, history: list[ResponseItem], phase: str
    ) -> list[ResponseItem]:
        """postcompact re-injection：压缩成功后把 pinned 状态钉回 history 尾。

        紧随 K3 salvage 之后调用（两个「压缩瞬间钩子」相邻）。按注册序渲染
        全部 source（双层护栏在 registry 内完成），每条以 ``system_injection``
        （source="pinned:<name>"）追加尾部并经 store 持久化（R5）。

        渲染异常 → EngineLog 告警后跳过该 source（壳层隔离业务渲染崩溃，
        有事件、非 silent fallback）。无注入且无丢弃 → 不 emit（零噪声）。
        """
        if self.__ctxload_owner.pinned_states is None or len(self.__ctxload_owner.pinned_states) == 0:
            return history
        rendered = self.__ctxload_owner.pinned_states.render_all()
        for name, err in rendered.errors:
            await self.__ctxload_owner._emit(EngineLog(data={
                "level": "warning",
                "message": f"pinned state source {name!r} 渲染失败，已跳过: {err}",
                "extra": {"source": name},
            }))
        if not rendered.entries and not rendered.dropped:
            return history
        from taifeng.conversation.models import system_injection

        new_history = list(history)
        for entry in rendered.entries:
            note = system_injection(
                entry.text, thread_id=self.__ctxload_owner.thread_id,
                source=f"pinned:{entry.name}",
            )
            new_history.append(note)
            await self.__ctxload_owner.store.append(note)
        await self.__ctxload_owner._emit(PinnedStateReinjected(data={
            "sources": [
                {"name": e.name, "chars": len(e.text)} for e in rendered.entries
            ],
            "total_chars": rendered.total_chars,
            "dropped": rendered.dropped,
            "phase": phase,
        }))
        return new_history

    def history_token_estimate(self) -> int:
        """按本 turn 的图片策略与业务估算器计算完整历史成本。"""
        return estimate_history_tokens(
            self.__ctxload_owner.history_buffer,
            image_input_policy=self.__ctxload_owner.image_input_policy,
            input_cost_estimator=self.__ctxload_owner.input_cost_estimator,
            model=self.__ctxload_owner.entry_skill.model or "",
        )

    async def maybe_inject_budget_hint(self) -> None:
        """预算自知（budget-awareness，ADR 0017 规则②）：pre-turn 估算用量，穿越
        ``soft_limit`` 时往 history 尾追一条**中性预算事实**（用了百分之几 / 距 hard
        还剩多少 token）+ emit ``BudgetHintInjected``；**穿越一次注一次**，用量回落
        到 soft 以下时复位（见 ``evaluate_budget_hint``）。

        R2：仅尾追加（不动已缓存前缀，不破 anchor），且在 pre-turn 边界；一次性
        语义把每个超限 episode 的额外 system 消息限到 1 条，避免反复刷新打断 cache。
        R1：只陈述客观事实，不含「该不该收敛」的产品意见——怎么做交给模型/业务侧。
        """
        tokens = self.__ctxload_owner._history_token_estimate()
        inject, self.__ctxload_owner._budget_notified = evaluate_budget_hint(
            tokens, self.__ctxload_owner.budget, was_notified=self.__ctxload_owner._budget_notified)
        if not inject:
            return
        from taifeng.conversation.models import system_injection

        note = system_injection(
            render_budget_hint(tokens, self.__ctxload_owner.budget),
            thread_id=self.__ctxload_owner.thread_id, source="budget_hint")
        self.__ctxload_owner.history_buffer.append(note)
        await self.__ctxload_owner.store.append(note)
        window = self.__ctxload_owner.budget.context_window
        await self.__ctxload_owner._emit(BudgetHintInjected(data={
            "used": tokens,
            "context_window": window,
            "ratio": round(tokens / window, 2) if window > 0 else 0.0,
            "remaining_to_hard": max(0, self.__ctxload_owner.budget.hard_limit - tokens),
        }))
