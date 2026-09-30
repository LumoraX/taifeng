"""BackgroundCompaction 策略 —— 后台延迟压缩（ADR 0087）。

LLM 摘要类压缩要调一次模型，耗时数秒到数十秒。按既有做法，它发生在某一轮开始之前，用户
提交消息后要先等压缩跑完。本策略把这次计算挪到后台：

```
pre_turn 检查，估算 ≥ 软阈值
  ├─ 已有算好的结果、且 history 前缀没变 → 立即应用（不再调模型）
  ├─ 估算 ≥ urgent_ratio × 窗口           → 等不起，同步压缩（与既有行为相同）
  └─ 其余                                 → 在后台开始算，本轮不压缩、照常进行
```

后台任务在 history 的一份快照上计算。history 是只追加的：计算期间新增的条目都在快照之后，
应用时把它们原样接在压缩结果后面即可。前缀若被改写过（rewind / rollback / 另一次压缩），
快照与现状对不上，结果作废并重新开始。

本策略包装一组内层策略（内部自带 orchestrator），应作为 ``compressors`` 里**唯一**的策略：
orchestrator 在一个策略不触发时会继续尝试下一个，若把兜底策略并列配置，后台计算期间兜底
策略会立刻同步压缩，延迟就失去了意义。

约束：

- **只在 pre_turn 起后台任务、只在 pre_turn 应用**。后台结果按 pre_turn 语义算出（可以动
  head）；mid_turn 不得动已缓存的前缀（R2），此时只在逼近硬阈值时交给内层同步处理。
- **每个 thread 至多一个后台任务**；状态按 thread 隔离（同一策略实例可被多个 engine 共用）。
- **后台失败过一次就退回同步**：不在后台反复重试一个会失败的摘要。
- ``aclose()`` 取消全部后台任务；关闭后一律同步。``EnginePool.close`` 会调用它。

参照：openclaw 的后台压缩。差异：不引入独立的调度器，延迟逻辑完全落在压缩策略协议之内，
内核主循环不感知。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.context.compressor import CompressionStrategy
    from taifeng.conversation.models import ResponseItem

logger = logging.getLogger(__name__)


@dataclass
class _Job:
    """一个 thread 上的后台压缩。"""

    snapshot_ids: tuple[str, ...]
    """计算所基于的 history 快照（条目 id，按序）。"""
    task: asyncio.Task[CompressionResult | None]
    result: CompressionResult | None = None
    """已算好、待应用的结果；尚未算完为 None。"""


class BackgroundCompactionStrategy:
    """把内层策略的压缩计算挪到后台，下一轮开始时应用。

    Args:
        strategies: 内层策略（按各自 priority 排序，语义同 ``compressors``）。
        priority: 本策略在外层 orchestrator 里的优先级（应是唯一策略，取值无关紧要）。
        urgent_ratio: token 估算占 context_window 的比例 ≥ 此值时不再等后台，同步压缩。
            应高于预算的软阈值比例、低于硬阈值比例。

    Raises:
        ValueError: 没有内层策略，或 ``urgent_ratio`` 不在 (0, 1] 内。
    """

    name = "background"

    def __init__(
        self,
        strategies: Sequence[CompressionStrategy],
        *,
        priority: int = 100,
        urgent_ratio: float = 0.85,
    ) -> None:
        if not strategies:
            raise ValueError("BackgroundCompactionStrategy requires at least one strategy")
        if not 0.0 < urgent_ratio <= 1.0:
            raise ValueError(f"urgent_ratio must be within (0, 1], got {urgent_ratio!r}")
        self.priority = priority
        self._inner = CompressionOrchestrator(list(strategies))
        self._urgent_ratio = urgent_ratio
        self._jobs: dict[str, _Job] = {}
        # 后台失败过的 thread：不再后台重试，退回同步
        self._failed: set[str] = set()
        self._closed = False

    # ---- 观测 ----

    @property
    def pending_threads(self) -> frozenset[str]:
        """有后台任务在算、或有结果待应用的 thread。"""
        return frozenset(self._jobs)

    async def wait_idle(self) -> None:
        """等全部在算的后台任务收尾（测试与优雅停机用）；不取消它们。"""
        tasks = [job.task for job in self._jobs.values() if not job.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 让 done 回调（把结果记到 job 上）先跑完
        await asyncio.sleep(0)

    async def aclose(self) -> None:
        """取消全部后台任务并清空状态；此后一律同步压缩。"""
        self._closed = True
        jobs = list(self._jobs.values())
        self._jobs.clear()
        for job in jobs:
            job.task.cancel()
        if jobs:
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)

    # ---- 触发 ----

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        """见模块 docstring 的三分支；返回 None 表示「本次不压缩」。"""
        thread_id = _thread_of(ctx.history)
        if thread_id is None:
            return None
        inner_trigger = self._inner_trigger(ctx)
        if inner_trigger is None:
            return None
        if ctx.phase != "pre_turn":
            return inner_trigger if self._urgent(ctx) else None
        if self._ready(thread_id, ctx.history) is not None:
            return inner_trigger
        if self._urgent(ctx) or self._closed or thread_id in self._failed:
            self._discard(thread_id)
            return inner_trigger
        self._ensure_job(thread_id, ctx)
        return None

    def _inner_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        """内层是否有策略愿意压缩（取第一个触发的）。"""
        for strategy in self._inner.strategies:
            trigger = strategy.should_trigger(ctx)
            if trigger is not None:
                return trigger
        return None

    def _urgent(self, ctx: CompressionContext) -> bool:
        """是否已逼近硬阈值、等不起后台。"""
        ratio = ctx.token_estimate / max(ctx.budget.context_window, 1)
        return ratio >= self._urgent_ratio

    # ---- 后台任务 ----

    def _ready(
        self, thread_id: str, history: list[ResponseItem],
    ) -> CompressionResult | None:
        """该 thread 上可应用的后台结果；没有、没算完、或快照已对不上返回 None。"""
        job = self._jobs.get(thread_id)
        if job is None or job.result is None:
            return None
        if not _is_prefix(job.snapshot_ids, history):
            return None
        return job.result

    def _ensure_job(self, thread_id: str, ctx: CompressionContext) -> None:
        """保证该 thread 上有一个基于当前前缀的后台任务；已有且仍有效则不动。"""
        job = self._jobs.get(thread_id)
        if job is not None and _is_prefix(job.snapshot_ids, ctx.history):
            return
        self._discard(thread_id)
        snapshot = replace(ctx, history=list(ctx.history))
        task = asyncio.get_running_loop().create_task(
            self._inner.maybe_compress(
                snapshot, InitialContextInjection.BEFORE_LAST_USER_MESSAGE
            ),
            name=f"background-compaction:{thread_id}",
        )
        new_job = _Job(snapshot_ids=tuple(item.id for item in ctx.history), task=task)
        self._jobs[thread_id] = new_job
        task.add_done_callback(lambda done: self._on_done(thread_id, new_job, done))

    def _on_done(
        self,
        thread_id: str,
        job: _Job,
        task: asyncio.Task[CompressionResult | None],
    ) -> None:
        """后台任务收尾：成功则记下结果待应用；失败则作废并标记退回同步。"""
        if self._jobs.get(thread_id) is not job:
            # 任务已被作废（前缀变了 / 关闭）；结果不要
            if not task.cancelled():
                task.exception()  # 取走异常，避免「never retrieved」告警
            return
        if task.cancelled():
            self._jobs.pop(thread_id, None)
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "background compaction failed for thread %s; falling back to synchronous",
                thread_id,
                exc_info=error,
            )
        result = None if error is not None else task.result()
        if result is None or not result.success:
            self._jobs.pop(thread_id, None)
            self._failed.add(thread_id)
            return
        job.result = result

    def _discard(self, thread_id: str) -> None:
        """作废该 thread 上的后台任务 / 结果。"""
        job = self._jobs.pop(thread_id, None)
        if job is not None:
            job.task.cancel()

    # ---- 主入口 ----

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult:
        """应用后台算好的结果；没有可用结果时交给内层同步压缩。"""
        thread_id = _thread_of(ctx.history)
        ready = None
        if thread_id is not None and ctx.phase == "pre_turn":
            ready = self._ready(thread_id, ctx.history)
        if thread_id is not None:
            job = self._jobs.pop(thread_id, None)
            if job is not None and ready is None:
                job.task.cancel()
            # 无论这次走哪条路，都给后台重试一次机会
            self._failed.discard(thread_id)
        if ready is not None and job is not None:
            return _rebase(ready, ctx.history, len(job.snapshot_ids))
        result = await self._inner.force_compress(ctx, injection)
        if result is None:
            return CompressionResult(
                success=False,
                cache_invalidated=False,
                anchor_preserved_until=ctx.cache_anchor_index,
                reason="no_strategy",
            )
        return result


def _thread_of(history: list[ResponseItem]) -> str | None:
    """history 所属的 thread；空 history 返回 None。"""
    return history[0].thread_id if history else None


def _is_prefix(snapshot_ids: tuple[str, ...], history: list[ResponseItem]) -> bool:
    """快照是否仍是当前 history 的前缀（逐条目 id 比对）。"""
    if len(history) < len(snapshot_ids):
        return False
    return all(
        history[index].id == item_id for index, item_id in enumerate(snapshot_ids)
    )


def _rebase(
    result: CompressionResult, history: list[ResponseItem], snapshot_len: int,
) -> CompressionResult:
    """把在快照上算出的结果接到当前 history 上：快照之后追加的条目原样跟在后面。"""
    return replace(
        result,
        new_history=[*result.new_history, *history[snapshot_len:]],
        detail={**result.detail, "background": 1},
    )


__all__ = ["BackgroundCompactionStrategy"]
