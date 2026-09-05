"""Codex usage 明细的容忍契约（ADR 0032）。

`usage` 是纯记账元数据：它不影响输出正确性，而上游又最爱往
`*_tokens_details` 里加新字段（`cached_tokens` / `audio_tokens` /
`reasoning_tokens` 都是这么陆续冒出来的）。旧实现无差别要求明细里**每一个**值
都是非负整数——等于在校验它根本不读的字段，一个新的字符串字段就能把一个已经
成功产出内容、已经 `response.completed` 的 turn 判死。

本文件锁住分界：**实际会被读取的键**仍严格 fail closed；其余字段一律忽略。
"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.llm.errors import InvalidResponseError
from taifeng.llm.providers.codex.accumulator import CodexResponsesAccumulator

_TEXT = "库存 A-17"


def _stream(usage: dict[str, Any]) -> list[dict[str, Any]]:
    """一条最小合法流，usage 可注入。"""
    return [
        {"type": "response.created"},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_1", "type": "message", "role": "assistant"},
        },
        {"type": "response.output_text.delta", "output_index": 0, "delta": _TEXT},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": _TEXT}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": usage,
            },
        },
    ]


def _run(usage: dict[str, Any]):
    """吸收整条流并 finalize，返回 terminal。"""
    accumulator = CodexResponsesAccumulator()
    for event in _stream(usage):
        accumulator.accept(event)
    return accumulator.finalize()


def _usage(**extra: Any) -> dict[str, Any]:
    """基础合法 usage，可叠加明细。"""
    return {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28, **extra}


# --- 不读的字段：形状随便变，都不许中断 -------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        # 上游新增的非整数字段
        {"cached_tokens": 5, "note": "hit"},
        {"cached_tokens": 5, "hit_rate": 0.87},
        {"cached_tokens": 5, "tier": {"name": "ephemeral", "ttl": "1h"}},
        {"cached_tokens": 5, "flags": ["a", "b"]},
        {"cached_tokens": 5, "disabled": True},
        {"cached_tokens": 5, "unavailable": None},
        # 甚至负数——只要不是我们读的键
        {"cached_tokens": 5, "delta_tokens": -3},
    ],
)
def test_unknown_input_detail_fields_do_not_abort(details: dict[str, Any]) -> None:
    """input_tokens_details 里的未知字段一律忽略，turn 照常完成。"""
    terminal = _run(_usage(input_tokens_details=details))
    assert terminal.usage.cache_read_input_tokens == 5


@pytest.mark.parametrize(
    "details",
    [
        {"reasoning_tokens": 3, "verbosity": "high"},
        {"reasoning_tokens": 3, "breakdown": {"plan": 1, "check": 2}},
        {"reasoning_tokens": 3, "sampled": False},
    ],
)
def test_unknown_output_detail_fields_do_not_abort(details: dict[str, Any]) -> None:
    """output_tokens_details 里的未知字段一律忽略。"""
    terminal = _run(_usage(output_tokens_details=details))
    assert terminal.usage.reasoning_tokens == 3


@pytest.mark.parametrize(
    "details", ["n/a", 0, ["cached_tokens"], True]
)
def test_non_object_details_are_ignored_not_fatal(details: Any) -> None:
    """明细整体不是 object → 什么都读不到，按 0 处理，不得杀 turn。

    与提取器一致：``extract_usage_openai_family`` 对非 dict 明细本就直接跳过。
    """
    terminal = _run(_usage(input_tokens_details=details))
    assert terminal.usage.cache_read_input_tokens == 0


def test_unknown_top_level_usage_fields_still_ok() -> None:
    """回归护栏：usage 顶层未知字段本来就该放行。"""
    terminal = _run(_usage(brand_new_tokens=7, service_tier="flex"))
    assert terminal.usage.input_tokens == 20


# --- 实际读取的键：维持 fail closed -----------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        {"cached_tokens": "5"},      # 字符串会让提取器 int() 崩，必须挡在前面
        {"cached_tokens": -1},
        {"cached_tokens": True},     # bool 是 int 子类，不得被 coercion 蒙混
        {"cached_tokens": 1.5},
    ],
)
def test_read_input_detail_key_is_still_strict(details: dict[str, Any]) -> None:
    """cached_tokens 是我们真读的值 —— 非非负整数仍须 fail closed。"""
    with pytest.raises(InvalidResponseError, match="input_tokens_details"):
        _run(_usage(input_tokens_details=details))


@pytest.mark.parametrize(
    "details", [{"reasoning_tokens": "3"}, {"reasoning_tokens": -1}, {"reasoning_tokens": True}]
)
def test_read_output_detail_key_is_still_strict(details: dict[str, Any]) -> None:
    """reasoning_tokens 同理。"""
    with pytest.raises(InvalidResponseError, match="output_tokens_details"):
        _run(_usage(output_tokens_details=details))


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": True, "output_tokens": 8, "total_tokens": 9},
        {"input_tokens": -1, "output_tokens": 8, "total_tokens": 7},
        {"input_tokens": 20, "output_tokens": 8},                      # 缺 total
        {"input_tokens": 20, "output_tokens": 8, "total_tokens": 29},  # 对不上账
    ],
)
def test_top_level_counts_remain_strict(usage: dict[str, Any]) -> None:
    """顶层三个计数喂 K2 会话 token 天花板，本次放宽不涉及它们。"""
    with pytest.raises(InvalidResponseError):
        _run(usage)
