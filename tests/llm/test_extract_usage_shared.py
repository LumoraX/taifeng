"""`_shared.extract_usage_*` 单元测试。

覆盖 spec ``llm-provider-native`` Requirement「DeepSeekClient 作为
OpenAICompatClient 薄子类」的 ``extract_usage_openai_family`` 字段优先级
2 个 Scenario + Anthropic / Gemini 字段映射。
"""

from __future__ import annotations

import pytest

from taifeng.llm.errors import InvalidResponseError
from taifeng.llm.providers._shared import (
    extract_usage_anthropic,
    extract_usage_gemini,
    extract_usage_openai_family,
)

# ============================================================
# extract_usage_openai_family —— cache_read 字段三优先级
# ============================================================


def test_openai_standard_prompt_tokens_details_cached() -> None:
    """OpenAI 标准：`prompt_tokens_details.cached_tokens`。"""
    u = extract_usage_openai_family({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 50},
    })
    assert u.input_tokens == 100
    assert u.output_tokens == 20
    assert u.total_tokens == 120
    assert u.cache_read_input_tokens == 50


def test_deepseek_prompt_cache_hit_tokens() -> None:
    """DeepSeek 特有字段：`prompt_cache_hit_tokens`。"""
    u = extract_usage_openai_family({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "prompt_cache_hit_tokens": 800,
        "prompt_cache_miss_tokens": 200,
    })
    assert u.input_tokens == 1000
    assert u.output_tokens == 200
    assert u.cache_read_input_tokens == 800
    # miss_tokens 不映射，但走 raw
    assert u.raw["prompt_cache_miss_tokens"] == 200


def test_anthropic_style_cache_read_input_tokens_takes_priority() -> None:
    """`cache_read_input_tokens` 顶层字段优先于其他两路。"""
    u = extract_usage_openai_family({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_read_input_tokens": 99,
        "prompt_tokens_details": {"cached_tokens": 50},  # 应被忽略
        "prompt_cache_hit_tokens": 80,  # 应被忽略
    })
    assert u.cache_read_input_tokens == 99


def test_priority_falls_through_to_deepseek_when_others_missing() -> None:
    """优先级 1 / 2 都缺时落到 DeepSeek 字段。"""
    u = extract_usage_openai_family({
        "prompt_tokens": 500,
        "completion_tokens": 100,
        "prompt_cache_hit_tokens": 300,
    })
    assert u.cache_read_input_tokens == 300


def test_empty_usage_returns_zeros() -> None:
    u = extract_usage_openai_family({})
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.cache_read_input_tokens == 0


def test_reasoning_tokens_from_completion_details() -> None:
    """OpenAI o1 / DeepSeek R1 风格的 reasoning_tokens。"""
    u = extract_usage_openai_family({
        "prompt_tokens": 100,
        "completion_tokens": 500,
        "completion_tokens_details": {"reasoning_tokens": 400},
    })
    assert u.reasoning_tokens == 400


# ============================================================
# extract_usage_anthropic
# ============================================================


def test_anthropic_usage_full() -> None:
    u = extract_usage_anthropic({
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 80,
    })
    assert u.input_tokens == 200
    assert u.output_tokens == 50
    assert u.total_tokens == 250
    assert u.cache_creation_input_tokens == 100
    assert u.cache_read_input_tokens == 80


def test_anthropic_usage_no_cache_fields() -> None:
    u = extract_usage_anthropic({"input_tokens": 100, "output_tokens": 20})
    assert u.cache_creation_input_tokens == 0
    assert u.cache_read_input_tokens == 0


# ============================================================
# extract_usage_gemini
# ============================================================


def test_gemini_usage_full() -> None:
    u = extract_usage_gemini({
        "promptTokenCount": 500,
        "candidatesTokenCount": 100,
        "totalTokenCount": 600,
        "cachedContentTokenCount": 200,
    })
    assert u.input_tokens == 500
    assert u.output_tokens == 100
    assert u.total_tokens == 600
    assert u.cache_read_input_tokens == 200


def test_gemini_usage_no_cache() -> None:
    u = extract_usage_gemini({
        "promptTokenCount": 100,
        "candidatesTokenCount": 50,
        "totalTokenCount": 150,
    })
    assert u.cache_read_input_tokens == 0


# ============================================================
# 记账字段的容忍度 —— 坏值 / 未知字段不得判死已成功的 turn
# ============================================================

_OK_COUNTS = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("cache_read_input_tokens", "abc"),
        ("cache_read_input_tokens", {"nested": 1}),
        ("prompt_cache_hit_tokens", ["not", "a", "number"]),
        ("cache_creation_input_tokens", "N/A"),
    ],
)
def test_accounting_field_bad_value_is_ignored_not_fatal(
    field: str, bad_value: object
) -> None:
    """记账字段坏值按缺失处理，绝不判死 turn。

    这些字段不是 OpenAI 正式顶层字段（Anthropic 风格 / DeepSeek 特有），只用于
    观测 cache 命中率。中转网关塞进怪值时若硬失败，等于让一个已经成功产出内容
    的 turn 因为一条记账数据而崩——与 ADR 0030 对 SSE 未知帧的口径一致。
    """
    u = extract_usage_openai_family({**_OK_COUNTS, field: bad_value})
    assert u.input_tokens == 10
    assert u.output_tokens == 5
    assert u.cache_read_input_tokens == 0
    assert u.cache_creation_input_tokens == 0


def test_reasoning_tokens_bad_value_is_ignored_not_fatal() -> None:
    """明细里的 reasoning_tokens 坏值同样只按缺失处理。"""
    u = extract_usage_openai_family({
        **_OK_COUNTS,
        "output_tokens_details": {"reasoning_tokens": "oops"},
    })
    assert u.reasoning_tokens == 0
    assert u.total_tokens == 15


def test_unknown_upstream_fields_do_not_break_extraction() -> None:
    """上游新增的未知字段一律放行——新功能上线不得让内核当场报错。"""
    u = extract_usage_openai_family({
        **_OK_COUNTS,
        "brand_new_2027_field": {"nested": "whatever"},
        "input_tokens_details": {"cached_tokens": 3, "audio_tokens": 9},
    })
    assert u.cache_read_input_tokens == 3  # 认识的照常提取
    assert u.input_tokens == 10


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "total_tokens"])
def test_spec_counts_bad_value_raises_classified_error(field: str) -> None:
    """主计数坏值 fail closed，但必须是**分类过的** LLMError。

    input / output / total 是规范必填整数，且直接喂会话 token 天花板等资源决策，
    错值会让调度判断出错，所以不能容忍。但抛的必须是 InvalidResponseError——
    裸 ValueError / TypeError 不是 LLMError，失败策略分不了类，拿不到
    SUSPEND / TERMINAL 处置。
    """
    with pytest.raises(InvalidResponseError):
        extract_usage_openai_family({**_OK_COUNTS, field: "not-a-number"})


def test_spec_counts_absent_or_zero_still_default() -> None:
    """缺省 / 零值走历史默认路径（total 缺失时回落 prompt+completion）。"""
    u = extract_usage_openai_family({"input_tokens": 7, "output_tokens": 3})
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (7, 3, 10)
    empty = extract_usage_openai_family({})
    assert (empty.input_tokens, empty.output_tokens, empty.total_tokens) == (0, 0, 0)
