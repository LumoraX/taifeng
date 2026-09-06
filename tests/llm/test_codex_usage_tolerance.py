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


# --- 实际读取的键：取不到就置空，不判死 turn（ADR 0034） --------------------


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"cached_tokens": "5"}, 5),     # 数字字符串：表示法差异，照常采用
        ({"cached_tokens": 1.5}, 1),     # 浮点：截断采用
        ({"cached_tokens": 0.0}, 0),
        ({"cached_tokens": -1}, 0),      # 负计数无意义 → 置空
        ({"cached_tokens": True}, 0),    # bool 不得被 coercion 蒙混 → 置空
        ({"cached_tokens": "abc"}, 0),   # 真坏值 → 置空
        ({"cached_tokens": {"n": 1}}, 0),
        ({"cached_tokens": None}, 0),
    ],
)
def test_read_input_detail_key_never_kills_turn(
    details: dict[str, Any], expected: int
) -> None:
    """cached_tokens 是纯记账量：能用就用，用不了置空，**绝不判死 turn**。

    ADR 0032 曾对它 fail closed，理由是「提取器 int() 会在更深处炸出非
    LLMError」。ADR 0034 让提取器自己容忍后该理由消失，而闸门的副作用是：
    中转网关把数字发成 "0" / 0.0 / null 这类表示法差异都会判死整条链路。
    """
    terminal = _run(_usage(input_tokens_details=details))
    assert terminal.usage.cache_read_input_tokens == expected
    assert terminal.usage.input_tokens == 20  # 主计数不受影响


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"reasoning_tokens": "3"}, 3),
        ({"reasoning_tokens": -1}, 0),
        ({"reasoning_tokens": True}, 0),
        ({"reasoning_tokens": "oops"}, 0),
    ],
)
def test_read_output_detail_key_never_kills_turn(
    details: dict[str, Any], expected: int
) -> None:
    """reasoning_tokens 同理。"""
    terminal = _run(_usage(output_tokens_details=details))
    assert terminal.usage.reasoning_tokens == expected


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
