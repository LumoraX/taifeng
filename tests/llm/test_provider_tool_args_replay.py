"""历史 tool call 参数回放测试（ADR 0074）。

Anthropic / Gemini 要求历史里的 tool call 参数是 JSON 对象。模型当初产出的参数若不是
合法 JSON 对象，回放时 MUST NOT 静默改成 ``{}``——那会让模型看到「自己发过一次无参调用」，
与随后的 ``invalid_arguments`` 错误结果对不上。
"""

from __future__ import annotations

import logging

import pytest

from taifeng.llm.providers._tool_args import (
    INVALID_ARGUMENTS_KEY,
    RAW_ARGUMENTS_KEY,
    replay_tool_arguments,
)
from taifeng.llm.providers.anthropic_provider import _to_anthropic_messages
from taifeng.llm.providers.gemini_provider import _to_gemini_contents
from taifeng.llm.types import ApiMessage, ApiRequest

_PROVIDER_LOGGER = "taifeng.llm.providers._tool_args"


def _request_with_arguments(arguments: object) -> ApiRequest:
    """构造一条历史里带 tool call 的请求。"""
    return ApiRequest(
        model="m",
        messages=[
            ApiMessage(role="user", content="go"),
            ApiMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": arguments},
                }],
            ),
            ApiMessage(
                role="tool",
                content="invalid_arguments: invalid_json",
                tool_call_id="call_1",
                name="search",
            ),
        ],
    )


# ------------------------------------------------------------------
# replay_tool_arguments
# ------------------------------------------------------------------


def test_valid_object_is_parsed() -> None:
    assert replay_tool_arguments('{"q": "cats"}', tool_name="search") == {"q": "cats"}


def test_dict_passes_through_as_copy() -> None:
    raw = {"q": "cats"}
    parsed = replay_tool_arguments(raw, tool_name="search")
    assert parsed == raw
    assert parsed is not raw


@pytest.mark.parametrize("raw", ["", "   ", "\n"])
def test_blank_is_legit_empty_object(raw: str) -> None:
    """无参工具的空串参数是合法空对象，不是错误。"""
    assert replay_tool_arguments(raw, tool_name="ping") == {}


def test_invalid_json_keeps_raw_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_PROVIDER_LOGGER):
        parsed = replay_tool_arguments('{"q": "cats"', tool_name="search")

    assert parsed[RAW_ARGUMENTS_KEY] == '{"q": "cats"'
    assert parsed[INVALID_ARGUMENTS_KEY].startswith("invalid_json:")
    assert set(parsed) == {INVALID_ARGUMENTS_KEY, RAW_ARGUMENTS_KEY}
    assert any("search" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize(
    ("raw", "type_name"),
    [("[1, 2]", "list"), ('"text"', "str"), ("42", "int"), ("null", "NoneType")],
)
def test_non_object_json_keeps_raw_text(raw: str, type_name: str) -> None:
    parsed = replay_tool_arguments(raw, tool_name="search")
    assert parsed == {
        INVALID_ARGUMENTS_KEY: f"not_an_object: got {type_name}",
        RAW_ARGUMENTS_KEY: raw,
    }


def test_non_string_non_object_value_is_serialized() -> None:
    """历史里直接存了非对象值（如 list）时同样不得穿透成非对象参数。"""
    parsed = replay_tool_arguments([1, 2], tool_name="search")
    assert parsed == {
        INVALID_ARGUMENTS_KEY: "not_an_object: got list",
        RAW_ARGUMENTS_KEY: "[1, 2]",
    }


def test_oversized_raw_text_is_truncated() -> None:
    raw = "{" + "x" * 50_000
    parsed = replay_tool_arguments(raw, tool_name="search", max_raw_chars=100)
    kept = parsed[RAW_ARGUMENTS_KEY]
    assert kept.startswith("{xxx")
    assert len(kept) < 200
    assert "truncated" in kept
    assert str(len(raw)) in kept


# ------------------------------------------------------------------
# provider 回放
# ------------------------------------------------------------------


def test_anthropic_replay_does_not_blank_invalid_arguments() -> None:
    req = _request_with_arguments('{"q": "cats"')
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())

    tool_use = msgs[1]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["input"] != {}
    assert tool_use["input"][RAW_ARGUMENTS_KEY] == '{"q": "cats"'


def test_anthropic_replay_never_sends_non_object_input() -> None:
    req = _request_with_arguments("[1, 2]")
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())

    tool_input = msgs[1]["content"][0]["input"]
    assert isinstance(tool_input, dict)
    assert tool_input[RAW_ARGUMENTS_KEY] == "[1, 2]"


def test_anthropic_replay_valid_arguments_unchanged() -> None:
    req = _request_with_arguments('{"q": "cats"}')
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert msgs[1]["content"][0]["input"] == {"q": "cats"}


def test_gemini_replay_does_not_blank_invalid_arguments() -> None:
    req = _request_with_arguments('{"q": "cats"')
    _, contents = _to_gemini_contents(req)

    call = contents[1]["parts"][0]["functionCall"]
    assert call["args"] != {}
    assert call["args"][RAW_ARGUMENTS_KEY] == '{"q": "cats"'


def test_gemini_replay_never_sends_non_object_args() -> None:
    req = _request_with_arguments('"text"')
    _, contents = _to_gemini_contents(req)

    args = contents[1]["parts"][0]["functionCall"]["args"]
    assert isinstance(args, dict)
    assert args[RAW_ARGUMENTS_KEY] == '"text"'


def test_gemini_replay_valid_arguments_unchanged() -> None:
    req = _request_with_arguments('{"q": "cats"}')
    _, contents = _to_gemini_contents(req)
    assert contents[1]["parts"][0]["functionCall"]["args"] == {"q": "cats"}


def test_replay_is_deterministic_for_cache_prefix() -> None:
    """同一段历史两次回放逐字节一致，不破坏 provider 侧缓存前缀（R2）。"""
    req = _request_with_arguments('{"q": ')
    first = _to_anthropic_messages(req, cache_indexes=set())
    second = _to_anthropic_messages(req, cache_indexes=set())
    assert first == second
