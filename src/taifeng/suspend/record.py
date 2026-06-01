"""SuspensionRecord —— 落 store 的可序列化挂起断点标记。

一条 record 对应 turn 的一次挂起，可含多个 PendingRequest（多挂起点并存）。
通过 to_item/from_item 与 conversation.ResponseItem(kind="suspension") 互转，
复用既有 JSONL 追加写，实现跨进程 resume（R5）。

与 R1 的关系：SuspensionRecord 本身无业务概念，thread_id / submission_id 均为
基础调度 id，不含 tenant / 领域名词。
"""
from __future__ import annotations

from dataclasses import dataclass

from taifeng.conversation.models import ResponseItem, suspension_item
from taifeng.suspend.reason import PendingRequest, SuspendReason


@dataclass(frozen=True)
class SuspensionRecord:
    """turn 一次挂起的完整断点。

    一个 record 可对应多个并发挂起点（如同一 turn 内多路 call_skill 各需审批），
    所有 PendingRequest 共享同一 record_id；resume 时按 request_ids 逐一核销。

    Attributes:
        record_id: 幂等键（重复 resume 检测）。
        thread_id: 所属 thread。
        submission_id: 触发挂起的 submission。
        turn_index: 挂起时的 turn 迭代序号。
        pending: 本次挂起的全部待办请求（不可变 tuple）。
        created_at: 业务侧时间戳（R1：src 内不取系统时钟）。
    """

    record_id: str
    thread_id: str
    submission_id: str
    turn_index: int
    pending: tuple[PendingRequest, ...]
    created_at: int

    def request_ids(self) -> set[str]:
        """本 record 全部 pending 的 request_id 集合（resume 校验用）。

        Returns:
            所有 PendingRequest.request_id 构成的 set。
        """
        return {p.request_id for p in self.pending}

    def to_item(self) -> ResponseItem:
        """序列化为 suspension ResponseItem（落 JSONL）。

        PendingRequest 各字段逐一展开为 dict，SuspendReason 取 .value
        保证 JSON 序列化稳定（StrEnum 保障，见 test_suspend_reason_json_serializes_to_string）。

        Returns:
            kind="suspension" 的 ResponseItem，payload 含完整断点信息。
        """
        return suspension_item(
            record_id=self.record_id,
            submission_id=self.submission_id,
            turn_index=self.turn_index,
            pending=[
                {
                    "request_id": p.request_id,
                    "reason": p.reason.value,
                    "payload_schema": p.payload_schema,
                    "related_call_id": p.related_call_id,
                    "detail": p.detail,
                }
                for p in self.pending
            ],
            created_at=self.created_at,
            thread_id=self.thread_id,
        )

    @classmethod
    def from_item(cls, item: ResponseItem) -> SuspensionRecord:
        """从 suspension ResponseItem 还原 SuspensionRecord。

        SuspendReason 从字符串值还原为枚举；payload_schema / detail / related_call_id
        缺失时恢复默认值（与 PendingRequest dataclass 默认值对齐）。

        Args:
            item: kind 必须为 "suspension" 的 ResponseItem。

        Returns:
            等价的 SuspensionRecord 实例。

        Raises:
            ValueError: item.kind 不是 'suspension'。
        """
        if item.kind != "suspension":
            raise ValueError(f"期望 kind='suspension'，实际 kind='{item.kind}'")

        p = item.payload
        # 逐一还原 PendingRequest，缺省字段用 or {} / or None 对齐 dataclass 默认值
        pending = tuple(
            PendingRequest(
                request_id=d["request_id"],
                reason=SuspendReason(d["reason"]),
                payload_schema=d.get("payload_schema") or {},
                related_call_id=d.get("related_call_id"),
                detail=d.get("detail") or {},
            )
            for d in p["pending"]
        )
        return cls(
            record_id=p["record_id"],
            thread_id=item.thread_id,
            submission_id=p["submission_id"],
            turn_index=p["turn_index"],
            pending=pending,
            created_at=p["created_at"],
        )
