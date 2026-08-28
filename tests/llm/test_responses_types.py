"""provider-neutral Responses terminal item DTO 测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from taifeng.llm.responses_types import (
    NormalizedFunctionCallItem,
    NormalizedMessageItem,
)


def test_normalized_function_call_rejects_empty_identity() -> None:
    """函数调用在进入 durable history 前必须有稳定身份。"""
    with pytest.raises(ValidationError):
        NormalizedFunctionCallItem(
            output_index=0,
            call_id="",
            name="inspect",
            arguments="{}",
        )


def test_normalized_message_is_frozen_and_forbids_extra_fields() -> None:
    """中性 terminal DTO 不得被 provider 扩展字段污染。"""
    item = NormalizedMessageItem(output_index=0, text="ok")

    with pytest.raises(ValidationError):
        NormalizedMessageItem.model_validate(
            {**item.model_dump(), "provider_extension": True}
        )
    with pytest.raises(ValidationError):
        item.text = "changed"
