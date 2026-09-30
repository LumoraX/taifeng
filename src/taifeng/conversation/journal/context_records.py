"""上下文维护的 durable 记录：压缩结论与预算提示（session-journal，ADR 0094）。

审计模式下，压缩与预算提示都会改变模型看到的上下文，因此必须是 Journal 里的事实：

```text
context_compacted + conversation_item(compacted)        一次成功的折叠式压缩
budget_hint_injected + conversation_item(system_injection)  一条预算提示
```

两者各自与它产生的对话项同批提交。压缩为摘要发起的 LLM 调用按普通 LLM effect 落账
（request / checkpoint / response），``context_compacted`` 只引用它们的 record id。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from taifeng.conversation.journal.models import NonEmptyStr  # noqa: TC001  # Pydantic 运行期需要
from taifeng.conversation.journal.records import CanonicalMapping, PayloadModel

CONTEXT_COMPACTED_RECORD_TYPE = "context_compacted"
BUDGET_HINT_RECORD_TYPE = "budget_hint_injected"

CompactionPhaseV1 = Literal["pre_turn", "mid_turn"]
"""审计模式下允许的压缩时机。手动压缩与溢出自愈不在其中。"""

COMPACTION_LLM_ITERATION_BASE = 1_000_000
"""压缩发起的 LLM 调用在所属 turn 里使用的 iteration 起点。

采样的 iteration 从 1 起、受 ``max_iterations`` 约束，远小于此值；压缩调用从这里起编号，
二者的 operation identity 因此不会相撞，也一眼可辨。
"""


class ContextCompactedV1(PayloadModel):
    """一次成功应用的折叠式压缩。

    Attributes:
        phase: 压缩时机。
        strategy: 执行压缩的策略名。
        ordinal: 本 turn 内第几次压缩（从 0 起）。
        tokens_before / tokens_after: 压缩前后的上下文占用估算。
        replaced_range: 被折叠的区间 ``[start, end)``，坐标系是压缩前的逻辑 history。
        removed_item_count: 被折叠的条目数。
        summary_item_id: 摘要条目的 id（同批提交的那条 ``compacted`` 对话项）。
        cache_invalidated: 是否破坏了已缓存的前缀。
        anchor_preserved_until: 压缩后保留的缓存锚点下标；-1 = 没有。
        quality_warnings: 摘要质量审计给出的非致命警告。
        detail: 策略自报的结构化计数。
        llm_request_record_ids: 为这次压缩发起的 LLM 调用的 request record id，按发起顺序。
    """

    phase: CompactionPhaseV1
    strategy: NonEmptyStr
    ordinal: int = Field(ge=0)
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    replaced_range: tuple[int, int]
    removed_item_count: int = Field(ge=1)
    summary_item_id: NonEmptyStr
    cache_invalidated: bool
    anchor_preserved_until: int = Field(ge=-1)
    quality_warnings: tuple[str, ...] = ()
    detail: CanonicalMapping = Field(default_factory=dict)
    llm_request_record_ids: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _require_consistent_range(self) -> ContextCompactedV1:
        """区间必须非空、非负，且与被折叠的条目数一致。"""
        start, end = self.replaced_range
        if start < 0 or end <= start:
            raise ValueError("replaced_range must be a non-empty range of non-negative indexes")
        if end - start != self.removed_item_count:
            raise ValueError("removed_item_count must equal the size of replaced_range")
        return self


class BudgetHintInjectedV1(PayloadModel):
    """一条预算提示：上下文占用越过软阈值时告知模型的中性事实。

    Attributes:
        used_tokens: 当时的上下文占用估算。
        context_window: 上下文窗口。
        soft_limit / hard_limit: 生效预算的两个阈值。
        item_id: 同批提交的那条 ``system_injection`` 对话项的 id。
    """

    used_tokens: int = Field(ge=0)
    context_window: int = Field(ge=1)
    soft_limit: int = Field(ge=0)
    hard_limit: int = Field(ge=0)
    item_id: NonEmptyStr


__all__ = [
    "BUDGET_HINT_RECORD_TYPE",
    "COMPACTION_LLM_ITERATION_BASE",
    "CONTEXT_COMPACTED_RECORD_TYPE",
    "BudgetHintInjectedV1",
    "CompactionPhaseV1",
    "ContextCompactedV1",
]
