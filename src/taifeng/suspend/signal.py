"""SuspendSignal —— 挂起的内部控制流异常。

不属于 LLMError 体系:挂起不是错误,是 turn 的正常中断信号。深处挂起点
(permission ask / 表单 tool / LLM 可恢复错误耗尽 retry 后)抛出,由
dispatch_batch / run_turn 捕获后聚合为 TurnSuspended 结局。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from taifeng.suspend.reason import PendingRequest


class SuspendSignal(Exception):  # noqa: N818
    """携带单个 PendingRequest 的挂起信号。

    用法：深处挂起点 ``raise SuspendSignal(pending)``；
    上层 run_turn / dispatch_batch 捕获后聚合为 TurnSuspended 结局。
    不继承 LLMError：挂起是 turn 的正常中断，不是错误分支。
    """

    def __init__(self, pending: PendingRequest) -> None:
        """初始化挂起信号。

        Args:
            pending: 触发本次挂起的待处理请求，包含原因与载荷。
        """
        self.pending = pending
        # 消息仅用于调试；格式稳定，便于日志过滤
        super().__init__(f"suspend: {pending.reason.value} request_id={pending.request_id}")
