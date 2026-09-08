"""engine 的进程内类型：事件信封、订阅者簿记、在飞 turn 登记。

从 ``engine.py`` 原样下沉（Wave 4 模块切分，行为零变化）。单独成模块是为了让
``engine_ops`` 等兄弟模块能引用 ``_PendingTurn`` 而不反向依赖 ``engine``（否则循环
import）。``DeliveredEvent`` 是公共 API，``engine`` 仍原样再导出，
``from taifeng.loop.engine import DeliveredEvent`` 的既有写法不受影响。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from taifeng.conversation.models import ResponseItem
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EventMsg


@dataclass(frozen=True)
class DeliveredEvent:
    """投递给某个订阅者的事件信封（审计可观测 层1）。

    携带 per-subscriber 的投递序号 ``delivery_seq``：每个订阅各自从 0 起连续；
    队列满被丢弃（QueueFull）时该序号仍被「烧掉」（计入但不投递），故订阅者收到的
    ``delivery_seq`` 一旦跳号 = **它自己**漏了事件——与全局 ``event.seq`` 跳号
    （那是过滤订阅天然只收子集所致，非丢弃）互不混淆。

    属性：
        event: 原始事件（其 ``seq`` 是该 engine 总线的全局序号）。
        delivery_seq: 本订阅者的连续投递序号（从 0 起，含丢弃烧号）。
    """

    event: EventMsg
    delivery_seq: int


# submission 级终结 kind 单一真相：过滤订阅据此收尾，终态记账据此登记。
# 二者必须同集合，否则会出现「订阅认为没结束 / 记账认为已结束」的语义分叉。
_TERMINAL_KINDS = frozenset({"turn_completed", "turn_failed", "turn_suspended"})


class _Subscriber:
    """单个订阅的队列 + 投递簿记（审计可观测 层1）。

    封装三件 per-subscriber 状态：
    1. ``queue``：进程内 asyncio 队列（maxsize<=0 表示无界）。
    2. ``next_delivery``：下一个投递序号（每次投递尝试 +1，含丢弃烧号）。
    3. 高/低水位告警迟滞：``warned`` 标记 + ``last_warn`` 时戳，避免阈值附近刷屏。

    参数：
        maxsize: 队列容量（<=0 无界）。
        high_ratio / low_ratio: 高/低水位占容量的比例（仅有界队列有意义）。
    """

    def __init__(self, *, maxsize: int, high_ratio: float, low_ratio: float) -> None:
        self.queue: asyncio.Queue[DeliveredEvent] = asyncio.Queue(
            maxsize=maxsize if maxsize > 0 else 0
        )
        self.next_delivery: int = 0
        # 高/低水位绝对阈值：仅有界（maxsize>0）时可换算；无界时为 None → 不告警
        self.high_water: int | None = int(maxsize * high_ratio) if maxsize > 0 else None
        self.low_water: int | None = int(maxsize * low_ratio) if maxsize > 0 else None
        self.warned: bool = False
        self.last_warn: int | None = None


@dataclass
class _PendingTurn:
    submission_id: str
    cancel: CancellationToken
    turn_index: int | None = None
    # B1 midturn-input-steering：注入队列。engine 处理 InjectUserInput Op 时 append，
    # 与对应活跃 TurnRunner.pending_input 共享同一 list 引用，runner 迭代边界 drain。
    pending_input: list[ResponseItem] = field(default_factory=list)
    # 是否根 thread 的 turn：InjectSystemMessage 只投给根 turn；子 thread 续跑登记
    # `_pending`（供 CancelTurn 触达）时置 False。
    is_root: bool = True
