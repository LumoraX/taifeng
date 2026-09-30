"""SpawnDriver 的终态收敛（finalize / settle / persist）。

方法体从 ``spawn_driver.py`` 原样搬出（W7.1 拆文件，零行为变更）；``SpawnDriver`` 类里按原名赋值，
三个收敛点的单点约束与终态幂等不变（见 detached-spawn 契约）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from taifeng.loop.event import EventMsg, SpawnCancelled, SpawnCompleted, SpawnFailed, SpawnSuspended
from taifeng.loop.spawn_ledger import settle_spawn

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from taifeng.loop.spawn_driver import SpawnDriver
    from taifeng.loop.spawn_handle import SpawnHandle, SpawnStatus


async def _finalize_spawn(self: SpawnDriver, handle_id: str, child_thread_id: str, outcome: Any
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

    终态的顺序是「持久化 → 句柄状态 → 事件」(见 ``_settle``):读句柄表的人看到
    终态时,它已经持久化了。
    """
    eng = self._engine
    # 终态幂等：已收敛（或正在收敛）的句柄不再二次处理（防 running-kill 双发 spawn_cancelled）。
    if self._spawn_handles.is_terminal(handle_id) or handle_id in self._settling:
        return
    end = outcome.end_reason
    if end == "completed":
        await self._settle(
            handle_id, "done", outcome.final_text, end,
            SpawnCompleted(data={"handle_id": handle_id, "result": outcome.final_text}),
        )
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
        await self._settle(
            handle_id, "cancelled", outcome.error, end,
            SpawnCancelled(data={"handle_id": handle_id}),
        )
    else:
        # error / max_iterations / resource_limit 等非成功终态 → error
        err = outcome.error or end
        await self._settle(
            handle_id, "error", err, end,
            SpawnFailed(data={"handle_id": handle_id, "error": err}),
        )
    # join-barrier:本 spawn 进入终态(含 suspended——但 suspended 非终态,
    # all_terminal 不满足 → 不触发),检查是否凑齐某 barrier 的全终态条件。
    await self._check_barriers(handle_id)


async def _settle_failed(self: SpawnDriver,
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
    # 终态幂等:已收敛(或正在收敛)的句柄不二次处理(终态事件恰好一次)。
    if self._spawn_handles.is_terminal(handle_id) or handle_id in self._settling:
        return
    await self._settle(
        handle_id, "error", error, "error",
        SpawnFailed(data={"handle_id": handle_id, "error": error}),
    )
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


async def _settle(self: SpawnDriver, handle_id: str, status: SpawnStatus, result: str | None, end_reason: str,
    msg: Any,
) -> None:
    """终态三步:持久化 → 句柄状态 → 事件。

    句柄状态在持久化之后才变:查询与等待读的是句柄表,它们看到终态时这个终态已经
    写下了。持久化期间句柄记在 ``_settling`` 里,其他收敛路径据此让开(终态恰好一次)。
    持久化失败时状态照样回写(句柄不停在 running),异常继续上抛。
    """
    handle = self._spawn_handles.get(handle_id)
    assert handle is not None, handle_id
    self._settling.add(handle_id)
    try:
        await self._persist_settled(handle.child_thread_id, status, result, end_reason)
    finally:
        self._settling.discard(handle_id)
        self._spawn_handles.set_result(handle_id, status=status, result=result)
    await self._engine._emit(EventMsg(submission_id=handle_id, msg=msg))  # noqa: SLF001


async def _persist_settled(self: SpawnDriver, child_thread_id: str, status: str, result: str | None,
    end_reason: str | None = None,
) -> None:
    """终态持久化(三个收敛点共用):非审计落子 thread 的 ``spawn_settled`` 锚,
    审计落 ``spawn_settled`` 记录(见 spawn_ledger)。

    冷恢复 ``_infer_spawn_status_from_child`` 据此得到与热状态一致的终态,不再
    凭「有无 assistant 文本」猜 done(wave2b 复现 f)。durable 先于 emit。
    """
    handle_id = next(
        h.handle_id for h in self._spawn_handles.handles.values()
        if h.child_thread_id == child_thread_id
    )
    await settle_spawn(
        self._engine, handle_id=handle_id, child_thread_id=child_thread_id,
        status=status, result=result, end_reason=end_reason or status,
    )


async def _settle_cancelled_suspended(self: SpawnDriver, handle: SpawnHandle) -> None:
    """挂起句柄的 cancelled 收敛(kill / 续跑链取消共用):落盘 + 撤销 TTL + emit + barrier。

    前置:调用方已在**同步步**把句柄置 cancelled(与 token.cancel 同步,使并发
    resume 的 CAS 据此放弃)。无 live runner 驱动 _finalize_spawn,故在此内联。
    落盘两条(append-only,子 thread):
      - ``suspend_resolved:<record_id>`` marker:活跃挂起随取消一并核销——否则
        冷恢复推断回 suspended(僵尸复活)、TTL 重武装后对已 kill 句柄提交裁决、
        match_suspended_spawn 允许再次 Resume(wave2b 复现 e);
      - ``spawn_settled(cancelled)`` 终态锚。
    同时撤销该 record 的到期定时器(R4:被 kill 的句柄不再收到裁决)。
    """
    eng = self._engine
    child_tid = handle.child_thread_id
    record = eng._find_active_suspension_in(  # noqa: SLF001
        await eng._load_thread_items(child_tid))  # noqa: SLF001
    if record is not None:
        timer = eng._ttl_timers.pop(record.record_id, None)  # noqa: SLF001
        if timer is not None:
            timer.cancel()
        await eng._append_resolved_marker(child_tid, record.record_id)  # noqa: SLF001
    await self._persist_settled(child_tid, "cancelled", None)
    await eng._emit(EventMsg(  # noqa: SLF001
        submission_id=handle.handle_id,
        msg=SpawnCancelled(data={"handle_id": handle.handle_id}),
    ))
    # join-barrier:本句柄进入 cancelled 终态,可能凑齐某 barrier → 检查。
    await self._check_barriers(handle.handle_id)
