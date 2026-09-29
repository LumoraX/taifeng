"""上下文维护产生的对话项的稳定 payload 形状（session-journal，ADR 0094）。

独立成模块：``records.py`` 校验对话项时要用到这些形状，而本包的上下文 record
（``context_records``）又依赖 ``records.py``，放在一起会成环。
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

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

    审计模式只落账内核自己生成、且不改写 history 的注入；截断类 marker（rewind / rollback）
    的来源不在允许之列。
    """

    text: str
    source: Literal["budget_hint"]


__all__ = ["CompactedItemPayload", "SystemInjectionItemPayload"]
