"""`_shared.extract_usage_*` 单元测试。

覆盖 spec ``llm-provider-native`` Requirement「DeepSeekClient 作为
OpenAICompatClient 薄子类」的 ``extract_usage_openai_family`` 字段优先级
2 个 Scenario + Anthropic / Gemini 字段映射。
"""

from __future__ import annotations

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
