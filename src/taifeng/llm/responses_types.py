"""Responses 风格 provider 共用的 terminal normalized item。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from taifeng.llm.types import ProviderStateEnvelope


class _NormalizedItem(BaseModel):
    """不可变、禁止额外字段的 terminal output 基类。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    output_index: int = Field(ge=0)


class NormalizedReasoningItem(_NormalizedItem):
    """可见 reasoning 摘要及可选加密续传状态。"""

    type: Literal["reasoning"] = "reasoning"
    visible_text: str = ""
    state: ProviderStateEnvelope | None = None


class NormalizedMessageItem(_NormalizedItem):
    """terminal assistant text。"""

    type: Literal["message"] = "message"
    text: str


class NormalizedFunctionCallItem(_NormalizedItem):
    """terminal function call。"""

    type: Literal["function_call"] = "function_call"
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str


class NormalizedRefusalItem(_NormalizedItem):
    """terminal refusal；只参与错误分类，不进入 durable history。"""

    type: Literal["refusal"] = "refusal"
    text: str


type NormalizedOutputItem = (
    NormalizedReasoningItem
    | NormalizedMessageItem
    | NormalizedFunctionCallItem
    | NormalizedRefusalItem
)


__all__ = [
    "NormalizedFunctionCallItem",
    "NormalizedMessageItem",
    "NormalizedOutputItem",
    "NormalizedReasoningItem",
    "NormalizedRefusalItem",
]
