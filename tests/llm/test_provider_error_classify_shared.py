"""`_shared.classify_http_error` 单元测试。

覆盖 spec ``llm-provider-native`` Requirement「共享 `_shared.py` 提供错误分类
与 SSE 解析」的 6 个错误分类 Scenario + 4xx 兜底。
"""

from __future__ import annotations

from taifeng.llm.errors import (
    AuthenticationError,
    ContentFilterError,
    ContextOverflowError,
    InvalidRequestError,
    RateLimitError,
    ServerError,
    TransientNetworkError,
)
from taifeng.llm.providers._shared import classify_http_error

# ============================================================
# 1. 401 / 403 → AuthenticationError
# ============================================================


def test_401_unauthorized_classified_as_auth() -> None:
    out = classify_http_error(401, '{"error":{"message":"invalid api key"}}')
    assert isinstance(out, AuthenticationError)
    assert out.kind == "authentication"
    assert out.retryable is False


def test_403_forbidden_classified_as_auth() -> None:
    out = classify_http_error(403, "forbidden")
    assert isinstance(out, AuthenticationError)


# ============================================================
# 2. 429 → RateLimitError（带 retry_after 解析）
# ============================================================


def test_429_with_retry_after_in_error_object() -> None:
    """OpenAI 风格：error.retry_after"""
    out = classify_http_error(
        429, '{"error":{"type":"rate_limit","retry_after":30}}',
    )
    assert isinstance(out, RateLimitError)
    assert out.retry_after_seconds == 30.0
    assert out.retryable is True


def test_429_with_top_level_retry_after() -> None:
    """通用：顶层 retry_after"""
    out = classify_http_error(429, '{"retry_after": 5.5}')
    assert isinstance(out, RateLimitError)
    assert out.retry_after_seconds == 5.5


def test_429_without_retry_after() -> None:
    """Anthropic 风格：只有 error.type，无 retry_after"""
    out = classify_http_error(429, '{"error":{"type":"rate_limit_error"}}')
    assert isinstance(out, RateLimitError)
    assert out.retry_after_seconds is None


def test_429_non_json_body() -> None:
    """body 不是 json 也不报错"""
    out = classify_http_error(429, "too many requests")
    assert isinstance(out, RateLimitError)
    assert out.retry_after_seconds is None


# ============================================================
# 3. 408 → TransientNetworkError
# ============================================================


def test_408_classified_as_transient() -> None:
    out = classify_http_error(408, "request timeout")
    assert isinstance(out, TransientNetworkError)
    assert out.retryable is True


# ============================================================
# 4. 5xx → ServerError
# ============================================================


def test_500_classified_as_server() -> None:
    out = classify_http_error(500, "upstream exploded")
    assert isinstance(out, ServerError)
    assert out.retryable is True


def test_502_bad_gateway_classified_as_server() -> None:
    out = classify_http_error(502, "bad gateway")
    assert isinstance(out, ServerError)


def test_503_classified_as_server() -> None:
    out = classify_http_error(503, "service unavailable")
    assert isinstance(out, ServerError)


# ============================================================
# 5. 400 + 关键字 → ContextOverflow / ContentFilter
# ============================================================


def test_400_context_overflow_keyword() -> None:
    out = classify_http_error(
        400,
        "prompt is too long: maximum tokens is 200000",
    )
    assert isinstance(out, ContextOverflowError)


def test_400_context_length_keyword() -> None:
    out = classify_http_error(
        400, "context_length_exceeded: max is 8192",
    )
    assert isinstance(out, ContextOverflowError)


def test_400_safety_filter_keyword() -> None:
    out = classify_http_error(400, "content blocked by safety filter")
    assert isinstance(out, ContentFilterError)


def test_400_content_filter_keyword() -> None:
    out = classify_http_error(400, "content_filter triggered")
    assert isinstance(out, ContentFilterError)


# ============================================================
# 6. 其他 4xx → InvalidRequestError（兜底）
# ============================================================


def test_400_unknown_4xx_classified_as_invalid_request() -> None:
    out = classify_http_error(400, '{"error":{"message":"bad parameter X"}}')
    assert isinstance(out, InvalidRequestError)
    assert out.kind == "invalid_request"
    assert out.retryable is False


def test_404_classified_as_invalid_request() -> None:
    out = classify_http_error(404, "model not found")
    assert isinstance(out, InvalidRequestError)
