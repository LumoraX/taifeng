"""engine 事件投递：emit / 终态记账 / 投递与丢弃 / 高低水位告警

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `engine-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 engine 唯一持有。

**兄弟调用一律经 ``self._engine._x(...)`` 回弹**——engine 是唯一白盒寻址面，兄弟模块
与测试按原名调用/打桩，回弹才能让注入点继续生效。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from taifeng.loop.engine_types import _TERMINAL_KINDS, DeliveredEvent, _Subscriber
from taifeng.loop.event import EventMsg

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine

logger = logging.getLogger(__name__)


class EngineEvents:
    """engine 事件投递协作器（持 engine 引用，自身无状态）。"""

    def __init__(self, engine: AgentEngine) -> None:
        """
        Args:
            engine: 宿主 AgentEngine —— 提供运行态与共享依赖。
        """
        self._engine = engine

    async def emit(self, ev: EventMsg) -> None:
        # suspension-ttl:借唯一事件总线做定时器簿记——所有层级 turn(根/子/spawn)的
        # 挂起与核销事件都流经此处,单点覆盖,无需在各续跑路径埋点。
        kind = ev.msg.kind
        if kind == "turn_suspended":
            self._engine._arm_ttl_timer(ev.msg.data)
        elif kind == "suspension_resolved":
            # 人工(或上一轮自动)核销 → 撤销该 record 的定时器(先核销者胜)
            timer = self._engine._ttl_timers.pop(ev.msg.data.get("record_id", ""), None)
            if timer is not None:
                timer.cancel()
        # 审计可观测 层1：全局 seq 在入口同步分配（asyncio 单线程、本函数无 await
        # 让出点 → 并发多 turn/spawn 下原子、不重不漏）。同一 ev 广播给所有订阅，
        # 全局 seq 对各订阅一致；per-subscriber 的 delivery_seq 由 _deliver 各自记。
        ev.seq = self._engine._seq
        self._engine._seq += 1
        # 广播给 all subs（firehose）
        for sub in list(self._engine._all_subs):
            self._engine._deliver(sub, ev)
        # 投递给 per-submission sub（过滤订阅）
        per = self._engine._event_subs.get(ev.submission_id)
        if per is not None:
            self._engine._deliver(per, ev)
        # 终态记账（ADR 0031）：放在投递之后——先保证在线订阅者拿到，再留档给晚到者。
        if kind in _TERMINAL_KINDS:
            self._engine._record_terminal(ev)

    def record_terminal(self, ev: EventMsg) -> None:
        """登记一个 submission 的终结事件，供晚到订阅者补投（有界 FIFO）。

        同一 submission 重复终结（如 turn_suspended 后又被 Resume 跑出 turn_completed）
        以**最后一条**为准：晚到者关心的是「现在是什么状态」。重复登记会把该条目挪到
        队尾（视为最新），淘汰仍从队首取。
        """
        if self._engine._terminal_replay_size <= 0:
            return
        self._engine._terminal_replay.pop(ev.submission_id, None)
        self._engine._terminal_replay[ev.submission_id] = ev
        while len(self._engine._terminal_replay) > self._engine._terminal_replay_size:
            self._engine._terminal_replay.popitem(last=False)

    def deliver(self, sub: _Subscriber, ev: EventMsg) -> None:
        """把事件投递给单个订阅者：分配 per-subscriber delivery_seq（含丢弃烧号）→
        入队 → 失败计数 → 高/低水位告警。永不阻塞主 actor（put_nowait，R4）。

        delivery_seq 即使 QueueFull 丢弃也照常 +1（烧号），使订阅者凭自己收到的
        delivery_seq 跳号即可精确自检「我漏了几条」，与全局 seq 跳号互不混淆。
        """
        n = sub.next_delivery
        sub.next_delivery += 1
        try:
            sub.queue.put_nowait(DeliveredEvent(event=ev, delivery_seq=n))
        except asyncio.QueueFull:
            # K4：不再静默丢——累计计数 + WARNING（consumer 另可凭 delivery_seq 跳号
            # 精确自检）。不阻塞 emit（慢/缺席 consumer 不得拖死主 actor，R4）。
            self._engine._events_dropped += 1
            logger.warning("event queue full, drop event %s", ev.msg.kind)
        self._engine._maybe_warn_water(sub)

    def maybe_warn_water(self, sub: _Subscriber) -> None:
        """有界队列堆积告警：qsize 上穿高水位告一条 WARNING，回落到低水位以下才
        重新武装（迟滞）；告警另受 ``event_warn_cooldown_sec`` 限频。无界队列不告警。

        ⚠️ 告警走 logger 而非 emit 事件——告警事件本身也会进所有队列，堆积时会
        自我放大成告警风暴。
        """
        if sub.high_water is None:  # 无界队列：无容量百分比可言，不告警
            return
        qsize = sub.queue.qsize()
        if qsize >= sub.high_water and not sub.warned:
            now = self._engine._now_factory()
            if sub.last_warn is None or now - sub.last_warn >= self._engine._event_warn_cooldown_sec:
                logger.warning(
                    "event queue high-water: %d/%d (subscriber lagging)",
                    qsize,
                    self._engine._event_queue_size,
                )
                sub.last_warn = now
            sub.warned = True
        elif sub.low_water is not None and qsize <= sub.low_water and sub.warned:
            sub.warned = False  # 回落到低水位以下 → 重新武装下次告警
