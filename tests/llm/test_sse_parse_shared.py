"""`_shared.parse_sse_data` + `parse_sse_event` 单元测试。

覆盖 spec ``llm-provider-native`` Requirement「共享 `_shared.py` 提供错误分类
与 SSE 解析」的 SSE 解析 3 个 Scenario + 边界。
"""

from __future__ import annotations

from taifeng.llm.providers._shared import parse_sse_data, parse_sse_event

# ============================================================
# parse_sse_data —— OpenAI / Gemini 单行 `data: {...}`
# ============================================================


def test_parse_sse_data_valid_json() -> None:
    assert parse_sse_data('data: {"x":1}') == {"x": 1}


def test_parse_sse_data_with_extra_whitespace() -> None:
    """`data:   {json}` 多空格也接受。"""
    assert parse_sse_data('data:    {"y":2}') == {"y": 2}


def test_parse_sse_data_done_marker() -> None:
    """`[DONE]` 表示流结束 → None。"""
    assert parse_sse_data("data: [DONE]") is None


def test_parse_sse_data_empty_line() -> None:
    assert parse_sse_data("") is None


def test_parse_sse_data_comment_line() -> None:
    """SSE 规范：`:` 开头是注释。"""
    assert parse_sse_data(": keepalive") is None


def test_parse_sse_data_non_data_prefix() -> None:
    """非 `data:` 前缀（如 Anthropic 的 `event:`） → None。"""
    assert parse_sse_data("event: message_start") is None


def test_parse_sse_data_malformed_json() -> None:
    """坏 json 静默跳过 → None（不抛）。"""
    assert parse_sse_data("data: {malformed") is None


def test_parse_sse_data_json_array_returns_none() -> None:
    """payload 必须是 dict；array 也返回 None（防 type pollution）。"""
    assert parse_sse_data("data: [1,2,3]") is None


# ============================================================
# parse_sse_event —— Anthropic 双行 `event:\ndata:`
# ============================================================


def test_parse_sse_event_anthropic_double_line() -> None:
    lines = ["event: message_start", 'data: {"type":"message_start"}']
    name, payload = parse_sse_event(lines)
    assert name == "message_start"
    assert payload == {"type": "message_start"}


def test_parse_sse_event_with_blank_and_comment() -> None:
    """事件块中可能夹杂空行 / 注释，应跳过。"""
    lines = [
        "",
        ": ping",
        "event: content_block_delta",
        'data: {"index":0,"delta":{"type":"text_delta","text":"hi"}}',
    ]
    name, payload = parse_sse_event(lines)
    assert name == "content_block_delta"
    assert payload is not None
    assert payload["delta"]["text"] == "hi"


def test_parse_sse_event_data_only_no_event_name() -> None:
    """仅有 data 行（无 event 头）→ event_name=None, payload 有。"""
    name, payload = parse_sse_event(['data: {"x":1}'])
    assert name is None
    assert payload == {"x": 1}


def test_parse_sse_event_empty_lines() -> None:
    assert parse_sse_event([]) == (None, None)
