"""Audited 并发 turn 的 hot history 全身份合并边界。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.journal.records import StableErrorV1

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.models import ResponseItem


class AuditedHistoryConflictError(RuntimeError):
    """同一 ResponseItem id 对应不同完整内容。"""

    def __init__(self) -> None:
        """构造不携带 item id、payload 或异常原文的稳定边界异常。"""
        super().__init__("audited history item identity conflict")


def merge_audited_history(
    current: Sequence[ResponseItem],
    completed_runner: Sequence[ResponseItem],
) -> list[ResponseItem]:
    """按完整 ResponseItem 身份幂等合并，冲突时不修改输入列表。"""
    merged: list[ResponseItem] = []
    by_id: dict[str, ResponseItem] = {}
    for item in (*current, *completed_runner):
        existing = by_id.get(item.id)
        if existing is None:
            by_id[item.id] = item
            merged.append(item)
        elif existing != item:
            raise AuditedHistoryConflictError
    return merged


def audited_history_conflict_failure() -> StableErrorV1:
    """构造不暴露 history 内容的稳定 fail-closed 首因。"""
    return StableErrorV1(
        code="audit_history_item_conflict",
        class_name="AuditedHistoryConflictError",
        failure_class="history_invariant",
        retryable=False,
    )


__all__ = [
    "AuditedHistoryConflictError",
    "audited_history_conflict_failure",
    "merge_audited_history",
]
