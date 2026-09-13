"""suspension-TTL 裁决协作器：武装 / 到期触发 / 路由裁决 / 冷重武装。

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。这组行为围绕同一入口
序列彼此紧密调用（武装 → 到期 → 路由 → 收敛），故按 `engine-module-structure`
契约落为**协作者类**（同 ``SpawnResumeChain`` / ``JoinBarrierCoordinator``）。

与既有协作者一致：**本类无自有状态**。定时器表 ``_ttl_timers`` 仍由 engine 唯一
持有（``spawn_driver`` 与多个测试按 ``engine._ttl_timers`` 白盒寻址），本类经 engine
引用读写。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from taifeng.loop.event import EventMsg, SuspensionExpired
from taifeng.loop.submission import Resume

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine
    from taifeng.suspend.record import SuspensionRecord

logger = logging.getLogger(__name__)


class SuspensionTtlScheduler:
    """挂起到期自动裁决协作器（持 engine 引用，自身无状态）。

    **兄弟调用一律经 ``self._engine._x(...)`` 回弹**，不直接调本类方法：engine 是
    唯一白盒寻址面，测试按 ``monkeypatch.setattr(engine, "_ttl_expire_after", ...)``
    / ``engine._resolve_expiry_route`` 注入，回弹才能让注入点继续生效。
    """

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供定时器表、store、emit 与挂起记录访问。
        """
        self._engine = engine

    def arm(self, data: dict[str, Any]) -> None:
        """按 turn_suspended 事件武装到期定时器(expires_at 为 None 则不武装)。

        delay ≤ 0(装载时已过期)同样入队,任务体内立即触发裁决。重复武装同一
        record(冷重武装 + 热事件竞态)以先到者为准,后到 no-op。
        """
        record_id = data.get("record_id")
        expires_at = data.get("expires_at")
        thread_id = data.get("thread_id")
        if not record_id or expires_at is None or not thread_id:
            return
        if record_id in self._engine._ttl_timers:
            return
        delay = max(0, int(expires_at) - int(self._engine._now_factory()))
        self._engine._ttl_timers[record_id] = self._engine._start_operation(
            self._engine._ttl_expire_after(delay, str(thread_id), str(record_id)),
            name=f"ttl:{record_id}",
        )

    async def record_active(
        self, thread_id: str, record_id: str,
    ) -> SuspensionRecord | None:
        """到期任务的活跃性验证:record 仍活跃且不在飞 → 返回 record,否则 None。

        「先核销者胜」的单点判定:人工 Resume 已核销(marker 落盘)或正在处理
        (在飞占位)时,到期裁决让位。根 thread 读内存 history,其余 load store。
        """
        items = (
            list(self._engine._history) if thread_id == self._engine._thread_id
            else await self._engine._load_thread_items(thread_id)
        )
        record = self._engine._find_active_suspension_in(items)
        if record is None or record.record_id != record_id:
            return None
        if record_id in self._engine._resolving_records:
            return None
        return record

    async def expire_after(
        self, delay: float, thread_id: str, record_id: str
    ) -> None:
        """到期任务体:睡到 deadline → 验证 record 仍活跃 → emit + 内核签发 auto-Resume。

        先核销者胜:触发时重读该 thread 的活跃挂起,record 已被人工 Resume 核销
        (或不是同一条)→ no-op。auto-Resume 用 EXPIRE_SENTINEL payload 经公共
        Resume Op 提交 —— root / call_skill 嵌套 / spawn 三条续跑链零改动全复用。
        """
        from taifeng.suspend.resolver import EXPIRE_EXHAUSTED, EXPIRE_SENTINEL

        try:
            if delay > 0:
                await asyncio.sleep(delay)
            # 验证活跃性 + 在飞让位(先核销者胜)
            record = await self._engine._ttl_record_active(thread_id, record_id)
            if record is None:
                return
            # 路由解析(fire 时):spawn 嵌套 leaf 的 thread_id 不可直接路由
            # (match_suspended_spawn 只认 spawn 子 tid)→ 解析可路由入口。
            # 挂起上浮(leaf → 上层 CHILD_SKILL / spawn 句柄置 suspended)有毫秒级
            # 窗口,fire 紧贴挂起时(短 ttl / 冷装载即过期)可能撞上 → 有界重试消化
            route_tid: str | None = None
            for _ in range(20):
                route_tid = await self._engine._resolve_expiry_route(thread_id, record_id)
                if route_tid is not None:
                    break
                await asyncio.sleep(0.05)
            if route_tid is None:
                # 退避重武装(suspend-review-fixes D3):到期是事实,路由不可达只是
                # 时序(挂起上浮未完成 / 批内兄弟在跑)。放弃版的「冷装载补」对长
                # 生命进程是永不发生的承诺;2s 后重试,人工核销/Shutdown 经既有
                # 取消路径终止,纯内存检查无 token 成本。
                logger.warning(
                    "suspension ttl expiry unroutable, rearming in 2s: "
                    "record=%s thread=%s", record_id, thread_id)
                self._engine._ttl_timers.pop(record_id, None)
                self._engine._arm_ttl_timer({
                    "record_id": record_id,
                    "thread_id": thread_id,
                    "expires_at": int(self._engine._now_factory()) + 2,
                })
                return
            # 重试等待期间可能已被人工 Resume 核销/占位 → 让位(先核销者胜)
            record = await self._engine._ttl_record_active(thread_id, record_id)
            if record is None:
                return
            items = (
                list(self._engine._history) if thread_id == self._engine._thread_id
                else await self._engine._load_thread_items(thread_id)
            )
            # 内核签发到期裁决:只对**未核销** pending 配哨兵(request 级核销下,
            # 已部分结算的 pending 再发哨兵会产生重复 fco)
            pending_left = self._engine._unsettled_pendings(record, items)
            if not pending_left:
                return
            # 自动 retry 谱系熔断(resource-limit-retry-semantics):任一 retry 位
            # pending 的谱系计数已达上限 → 本次裁决强制 abort,终止无界自动循环
            max_auto = self._engine._failure_suspend_max_auto_retries
            exhausted = max_auto is not None and any(
                p.on_expire == "retry"
                and int(p.detail.get("auto_retry_count", 0) or 0) >= max_auto
                for p in pending_left)
            expired_data: dict[str, Any] = {
                "record_id": record_id,
                "thread_id": thread_id,
                "on_expire": ",".join(sorted(
                    {p.on_expire for p in record.pending})),
                "reasons": sorted({p.reason.value for p in record.pending}),
            }
            if exhausted:
                expired_data["auto_retry_exhausted"] = True
            await self._engine._emit(EventMsg(
                submission_id=record_id,
                msg=SuspensionExpired(data=expired_data),
            ))
            sentinel: dict[str, Any] = {EXPIRE_SENTINEL: True}
            if exhausted:
                sentinel[EXPIRE_EXHAUSTED] = True
            await self._engine.submit(Resume(
                thread_id=route_tid,
                resolutions={p.request_id: dict(sentinel) for p in pending_left},
            ))
        except asyncio.CancelledError:
            # 人工 Resume 先到 / shutdown:正常撤销,非异常
            raise
        except Exception:
            logger.exception("suspension ttl expiry crashed: %s", record_id)
        finally:
            # 只弹自身:路由失败分支已重武装新 task,不可被旧 task 的收尾误弹
            if self._engine._ttl_timers.get(record_id) is asyncio.current_task():
                self._engine._ttl_timers.pop(record_id, None)

    async def resolve_expiry_route(
        self, thread_id: str, record_id: str,
    ) -> str | None:
        """到期裁决的路由解析(fire 时):返回可路由的 Resume.thread_id。

        武装时只记原 thread_id;leaf 挂起事件先于上层 CHILD_SKILL/spawn 挂起,
        提前解析有时序窗口,故推迟到 fire 时全量判定:
        1. 根 thread → 直接可路由;
        2. call_skill 链(挂根):根链可下探到该 thread → 原 thread_id 可路由
           (_handle_child_resume 自根寻址);
        3. spawn 拓扑:该 thread 是 spawn 子自身,或埋在某挂起态 spawn 句柄的
           子链中 → 以 **spawn 子 tid** 提交(与人工 Resume 约定一致,
           resume_spawn_nested 自动下探);
        4. 均未命中 → None(调用方 log + no-op,冷装载重武装再试)。
        """
        if thread_id == self._engine._thread_id:
            return thread_id
        if await self._engine._build_resume_chain(thread_id) is not None:
            return thread_id
        for h in self._engine._spawn.suspended_handles():
            if h.status != "suspended":
                continue
            if h.child_thread_id == thread_id:
                return thread_id  # spawn 直接挂起(既有可路由形态)
            if await self._engine._chain_contains_thread(
                    h.child_thread_id, thread_id,
                    self._engine._max_total_spawns_guard()):
                return h.child_thread_id
        return None

    async def chain_contains_thread(
        self, root_tid: str, target_tid: str, depth: int,
    ) -> bool:
        """自 root_tid 沿活跃挂起的 CHILD_SKILL pending DFS,判定子链是否含 target。"""
        from taifeng.suspend.reason import SuspendReason

        if root_tid == target_tid:
            return True
        if depth <= 0:
            return False
        items = await self._engine._load_thread_items(root_tid)
        record = self._engine._find_active_suspension_in(items)
        if record is None:
            return False
        for pend in record.pending:
            if pend.reason is not SuspendReason.CHILD_SKILL:
                continue
            child_tid = pend.detail.get("sub_thread_id")
            if isinstance(child_tid, str) and await self._engine._chain_contains_thread(
                    child_tid, target_tid, depth - 1):
                return True
        return False

    async def rearm_cold(self) -> None:
        """冷启动重武装(R5,根段):根 history 的活跃挂起。

        已过期 → delay=0 立即裁决;未过期 → 按剩余壁钟时长重武装。旧 JSONL 无
        ttl 字段 → expires_at 为 None,不武装(永不过期,前向兼容)。挂起态 spawn
        子 thread 由 ``_rearm_spawn_ttl_timers_cold`` 在句柄表重建完成后武装
        (run() 起跑时句柄表尚空,此处枚举不到);深层 call_skill leaf 的 ttl 由其
        自身 turn_suspended 热路径覆盖(v1 边界,见能力契约)。
        """
        record = self._engine._find_active_suspension()
        if record is not None and record.expires_at is not None:
            self._engine._arm_ttl_timer({
                "record_id": record.record_id,
                "thread_id": self._engine._thread_id,
                "expires_at": record.expires_at,
            })

    async def rearm_spawn_cold(self) -> None:
        """冷启动重武装(R5,spawn 段):挂起态 spawn 句柄子 thread 的活跃挂起。

        必须在 ``rebuild_from_history`` 填好句柄表之后调用——否则
        ``suspended_handles`` 为空,spawn 子 thread 的 TTL 永不武装(wave2b 复现 b:
        过期的挂起也永不裁决)。``_arm_ttl_timer`` 按 record_id 去重,与热路径 /
        根段重复调用无副作用。
        """
        for h in self._engine._spawn.suspended_handles():
            try:
                items = await self._engine._load_thread_items(h.child_thread_id)
            except Exception:
                logger.exception(
                    "ttl cold rearm: load spawn thread failed: %s",
                    h.child_thread_id)
                continue
            rec = self._engine._find_active_suspension_in(items)
            if rec is not None and rec.expires_at is not None:
                self._engine._arm_ttl_timer({
                    "record_id": rec.record_id,
                    "thread_id": h.child_thread_id,
                    "expires_at": rec.expires_at,
                })

    def cancel_all(self) -> None:
        """shutdown:取消全部到期定时器(R4,不阻塞主 actor)。"""
        for timer in self._engine._ttl_timers.values():
            timer.cancel()
        self._engine._ttl_timers.clear()
