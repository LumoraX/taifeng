"""Audited 并发 turn 的 hot history 全身份合并边界。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.journal.records import StableErrorV1

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from taifeng.conversation.models import ResponseItem


class AuditedHistoryConflictError(RuntimeError):
    """同一 ResponseItem id 对应不同完整内容。"""

    def __init__(self) -> None:
        """构造不携带 item id、payload 或异常原文的稳定边界异常。"""
        super().__init__("audited history item identity conflict")


def merge_audited_history(
    current: Sequence[ResponseItem],
    completed_runner: Sequence[ResponseItem],
    *,
    superseded: Collection[str] = (),
) -> list[ResponseItem]:
    """按完整 ResponseItem 身份幂等合并，冲突时不修改输入列表。

    ``superseded``：runner 在本轮压缩里折叠掉的条目 id（ADR 0094）。它们已不属于 hot history，
    ``current`` 里的这些条目不参与合并；runner 给出的 history 是它们所在位置的权威结果。
    发生过压缩时结果以 runner 的顺序为准，``current`` 里 runner 没见过的条目接在后面。
    """
    if superseded:
        seen = {item.id for item in completed_runner}
        late = [
            item for item in current
            if item.id not in superseded and item.id not in seen
        ]
        _require_consistent(current, completed_runner, superseded)
        return [*completed_runner, *late]
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


def _require_consistent(
    current: Sequence[ResponseItem],
    completed_runner: Sequence[ResponseItem],
    superseded: Collection[str],
) -> None:
    """未被折叠、两边都有的条目必须逐字一致。"""
    ours = {item.id: item for item in completed_runner}
    for item in current:
        if item.id in superseded:
            continue
        theirs = ours.get(item.id)
        if theirs is not None and theirs != item:
            raise AuditedHistoryConflictError


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
