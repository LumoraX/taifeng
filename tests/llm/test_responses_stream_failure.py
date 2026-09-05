"""Responses 流内失败事件的归一契约（ADR 0033）。

字段来源是 openai-openapi 官方 spec：

- ``ResponseErrorEvent``：``type`` / ``code: str|null`` / ``message: str`` /
  ``param: str|null`` / ``sequence_number: int``
- ``ResponseError``（``response.failed`` 的 ``response.error``）：``code`` + ``message``，
  非 null 时两者必填
- ``incomplete_details.reason``：**闭集** ``content_filter`` | ``max_output_tokens``

旧实现把这三种一律塌缩成 ``InvalidResponseError``（不可重试 / invalid_request），
既丢掉 provider 原文，又把瞬时故障伪装成确定性客户端错误。
"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.llm.errors import (
    AuthenticationError,
    ContentFilterError,
    ContextOverflowError,
    InvalidResponseError,
    LLMError,
    RateLimitError,
    ServerError,
)
from taifeng.llm.providers._shared import classify_responses_stream_failure
from taifeng.llm.providers.codex.accumulator import CodexResponsesAccumulator


def _error_event(**fields: Any) -> dict[str, Any]:
    """官方 ResponseErrorEvent 形状。"""
    return {"type": "error", "sequence_number": 7, **fields}


def _failed_event(error: Any) -> dict[str, Any]:
    """官方 response.failed 形状。"""
    return {
        "type": "response.failed",
        "response": {"id": "resp_1", "status": "failed", "error": error},
    }


def _incomplete_event(reason: Any) -> dict[str, Any]:
    """官方 response.incomplete 形状。"""
    return {
        "type": "response.incomplete",
        "response": {
            "id": "resp_1",
            "status": "incomplete",
            "incomplete_details": None if reason is None else {"reason": reason},
        },
    }


# --- error 事件：按 code 归类 ------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        ("rate_limit_exceeded", RateLimitError, True),
        ("too_many_requests", RateLimitError, True),
        ("server_error", ServerError, True),
        ("internal_server_error", ServerError, True),
        ("service_unavailable", ServerError, True),
        ("request_timeout", type(None), True),      # → TransientNetworkError，下方单测
        ("invalid_api_key", AuthenticationError, False),
        ("permission_denied", AuthenticationError, False),
        ("content_filter", ContentFilterError, False),
        ("context_length_exceeded", ContextOverflowError, False),
    ],
)
def test_error_event_code_is_classified(
    code: str, expected: type, retryable: bool
) -> None:
    """官方 code 决定 LLMError 子类与 retryable。"""
    exc = classify_responses_stream_failure(_error_event(code=code, message="boom"))
    assert exc.retryable is retryable
    if expected is not type(None):
        assert isinstance(exc, expected)


def test_timeout_code_maps_to_transient() -> None:
    """timeout 类 code 归瞬时网络错误。"""
    from taifeng.llm.errors import TransientNetworkError

    exc = classify_responses_stream_failure(_error_event(code="timeout", message="x"))
    assert isinstance(exc, TransientNetworkError)
    assert exc.failure_class == "provider_transport"


def test_unknown_code_defaults_to_server_error() -> None:
    """spec 对流内 error 的描述是「internal server error or a timeout」——
    无法识别的 code 默认按 provider 侧瞬时故障，而不是确定性客户端错误。"""
    exc = classify_responses_stream_failure(
        _error_event(code="relay_upstream_hiccup_2027", message="upstream said no")
    )
    assert isinstance(exc, ServerError)
    assert exc.retryable is True
    assert exc.failure_class == "provider_internal"


def test_missing_code_still_classified_not_swallowed() -> None:
    """code 是 nullable —— 缺失时不得退化成信息为零的 invalid_response。"""
    exc = classify_responses_stream_failure(_error_event(code=None, message="upstream 502"))
    assert isinstance(exc, ServerError)


# --- 原文不得丢 --------------------------------------------------------------


def test_official_fields_are_preserved_in_message() -> None:
    """code / param / message 三个官方字段必须都出现在异常文本里。"""
    exc = classify_responses_stream_failure(
        _error_event(code="rate_limit_exceeded", message="Rate limit reached", param="input")
    )
    text = str(exc)
    assert "rate_limit_exceeded" in text
    assert "param=input" in text
    assert "Rate limit reached" in text


def test_retry_after_hint_is_parsed_from_message() -> None:
    """限流时顺带取服务端 retry hint（与 HTTP 分类同一个解析器）。"""
    exc = classify_responses_stream_failure(
        _error_event(code="rate_limit_exceeded", message='{"retry_after": 12}')
    )
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after_seconds == 12


def test_message_keywords_used_when_code_unhelpful() -> None:
    """code 无法识别时回落到正文关键字（与 classify_http_error 共用同一张表）。"""
    exc = classify_responses_stream_failure(
        _error_event(code="bad_request", message="This request exceeds the maximum context")
    )
    assert isinstance(exc, ContextOverflowError)


# --- response.failed ---------------------------------------------------------


def test_response_failed_reads_nested_error_object() -> None:
    """response.error 的 code/message 与 error 事件同等对待。"""
    exc = classify_responses_stream_failure(
        _failed_event({"code": "rate_limit_exceeded", "message": "slow down"})
    )
    assert isinstance(exc, RateLimitError)
    assert "slow down" in str(exc)


def test_response_failed_with_null_error_is_still_typed() -> None:
    """error 可以是 null（spec 允许）—— 仍须给出可处置的分类，不得空转。"""
    exc = classify_responses_stream_failure(_failed_event(None))
    assert isinstance(exc, ServerError)
    assert "<no message>" in str(exc)


# --- response.incomplete：闭集 ------------------------------------------------


def test_incomplete_content_filter_maps_to_content_filter() -> None:
    """incomplete_details.reason=content_filter 就是内容拦截，不是响应畸形。"""
    exc = classify_responses_stream_failure(_incomplete_event("content_filter"))
    assert isinstance(exc, ContentFilterError)
    assert exc.failure_class == "content_filter"


def test_incomplete_max_output_tokens_maps_to_window_class() -> None:
    """输出被上限截断 → context_window 桶（其恢复配方=压缩后重试一次，正对症）。"""
    exc = classify_responses_stream_failure(_incomplete_event("max_output_tokens"))
    assert isinstance(exc, ContextOverflowError)
    assert exc.failure_class == "context_window"


@pytest.mark.parametrize("reason", ["something_new", None, 42])
def test_incomplete_reason_outside_closed_enum_is_protocol_violation(reason: Any) -> None:
    """reason 是闭集；集合外的值属协议违规，仍归 invalid_response。"""
    exc = classify_responses_stream_failure(_incomplete_event(reason))
    assert isinstance(exc, InvalidResponseError)


# --- 端到端：经 codex 累加器抛出的就是归一后的类型 ---------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "error", "code": "rate_limit_exceeded", "message": "x"}, RateLimitError),
        (
            {
                "type": "response.failed",
                "response": {"error": {"code": "server_error", "message": "y"}},
            },
            ServerError,
        ),
        (
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "content_filter"}},
            },
            ContentFilterError,
        ),
    ],
)
def test_accumulator_raises_normalized_error(event: dict[str, Any], expected: type) -> None:
    """累加器不再一律抛 InvalidResponseError。"""
    accumulator = CodexResponsesAccumulator()
    with pytest.raises(LLMError) as caught:
        accumulator.accept(event)
    assert isinstance(caught.value, expected)
