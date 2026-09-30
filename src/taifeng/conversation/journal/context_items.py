"""内核自己生成的对话项的稳定 payload 形状（session-journal，ADR 0094 / 0097）。

独立成模块：``records.py`` 校验对话项时要用到这些形状，而本包的上下文 record
（``context_records``）又依赖 ``records.py``，放在一起会成环。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from taifeng.conversation.journal.models import JournalModel


class CompactedItemPayload(JournalModel):
    """``compacted`` 对话项：一段被折叠的 history 的替代条目。"""

    summary: str
    replaced_range: list[int]
    cache_invalidated: bool

    @model_validator(mode="after")
    def _require_index_pair(self) -> CompactedItemPayload:
        """``replaced_range`` 必须是两个非负整数，且构成非空区间。"""
        values = self.replaced_range
        if len(values) != 2 or any(
            isinstance(value, bool) or value < 0 for value in values
        ):
            raise ValueError("replaced_range must hold two non-negative integers")
        if values[1] <= values[0]:
            raise ValueError("replaced_range must be a non-empty range")
        return self


class SystemInjectionItemPayload(JournalModel):
    """``system_injection`` 对话项。

    审计模式只落账内核自己生成、且不改写 history 的注入：预算提示与挂起的结清标记。
    截断类 marker（rewind / rollback）的来源不在允许之列。
    """

    text: str
    source: Literal["budget_hint", "suspend_resolved"]


class AwaitedRequestItem(JournalModel):
    """``suspension`` 对话项里的一个待答请求。"""

    request_id: str = Field(min_length=1)
    reason: Literal["permission", "form", "data"]
    payload_schema: dict[str, Any]
    related_call_id: str = Field(min_length=1)
    detail: dict[str, Any]
    ttl_seconds: None = None
    """审计模式不支持到期自动裁决：挂起只能由 ``Resume`` 结清。"""
    on_expire: Literal["abort"] = "abort"


class SuspensionItemPayload(JournalModel):
    """``suspension`` 对话项：turn 停下等人时的断点。"""

    record_id: str = Field(min_length=1)
    submission_id: str = Field(min_length=1)
    turn_index: int = Field(ge=0)
    pending: list[AwaitedRequestItem] = Field(min_length=1)
    created_at: int
    resolved: Literal[False] = False


__all__ = [
    "AwaitedRequestItem",
    "CompactedItemPayload",
    "SuspensionItemPayload",
    "SystemInjectionItemPayload",
]
