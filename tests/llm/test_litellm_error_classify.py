"""LiteLLM provider 的 ``_classify_litellm_error`` 分类正确性。

回归 bug：实际生产链路上 LiteLLM 把上游 OpenAI 兼容 endpoint 的网络故障
包成 ``InternalServerError`` 类（类名不含 connection / timeout），message 才
含 ``"Connection error."`` —— 旧分类器只看类名 → 误判成 ``InvalidRequestError``
（kind=invalid_request 不在 retryable_kinds）→ 整个 turn 不重试直接失败。

修复后：classifier 同时看 class name + message lower，连接类错误归到
``TransientNetworkError``，5xx / InternalServerError 归到 ``ServerError``，
两者都在 retry_async 的可重试集合里。
"""

from __future__ import annotations

from taifeng.llm.errors import (
    AuthenticationError,
    ContextOverflowError,
    InvalidRequestError,
    RateLimitError,
    ServerError,
    TransientNetworkError,
)
from taifeng.llm.providers.litellm_provider import _classify_litellm_error

# ============================================================
# 1. 网络类错误 —— TransientNetworkError（可重试）
# ============================================================


def test_connection_error_in_message_classified_as_transient() -> None:
    """实际生产 bug 现场：InternalServerError + 'Connection error.' message。"""

    class InternalServerError(Exception):
        pass

    exc = InternalServerError(
        "litellm.InternalServerError: InternalServerError: "
        "OpenAIException - Connection error."
    )
    out = _classify_litellm_error(exc)
    assert isinstance(out, TransientNetworkError)
    assert out.kind == "transient_network"


def test_timeout_in_class_name_classified_as_transient() -> None:
    class APITimeoutError(Exception):
        pass

    out = _classify_litellm_error(APITimeoutError("request timed out after 30s"))
    assert isinstance(out, TransientNetworkError)


def test_connection_reset_classified_as_transient() -> None:
    out = _classify_litellm_error(
        Exception("upstream peer: Connection reset by peer"),
    )
    assert isinstance(out, TransientNetworkError)


def test_remote_disconnected_classified_as_transient() -> None:
    out = _classify_litellm_error(
        Exception("Remote disconnected without response"),
    )
    assert isinstance(out, TransientNetworkError)


# ============================================================
# 2. 5xx / InternalServerError（不含 connection）→ ServerError（可重试）
# ============================================================


def test_pure_internal_server_error_classified_as_server() -> None:
    """类名是 InternalServerError 但 message 不含 connection → ServerError。"""

    class InternalServerError(Exception):
        pass

    out = _classify_litellm_error(InternalServerError("backend exploded"))
    assert isinstance(out, ServerError)
    assert out.kind == "server_error"


def test_502_status_classified_as_server() -> None:
    out = _classify_litellm_error(Exception("upstream returned 502 Bad Gateway"))
    assert isinstance(out, ServerError)


def test_bad_gateway_class_name() -> None:
    class BadGatewayError(Exception):
        pass

    out = _classify_litellm_error(BadGatewayError("upstream unreachable"))
    assert isinstance(out, ServerError)


# ============================================================
# 3. Rate limit
# ============================================================


def test_rate_limit_class_name() -> None:
    class _FakeRateLimitError(Exception):
        pass

    out = _classify_litellm_error(_FakeRateLimitError("too many requests"))
    assert isinstance(out, RateLimitError)


def test_rate_limit_message() -> None:
    out = _classify_litellm_error(
        Exception("upstream returned: rate limit exceeded"),
    )
    assert isinstance(out, RateLimitError)


# ============================================================
# 4. Auth
# ============================================================


def test_401_classified_as_auth() -> None:
    out = _classify_litellm_error(Exception("401 Unauthorized"))
    assert isinstance(out, AuthenticationError)


def test_403_classified_as_auth() -> None:
    out = _classify_litellm_error(Exception("403 Forbidden"))
    assert isinstance(out, AuthenticationError)


# ============================================================
# 5. Context overflow
# ============================================================


def test_context_length_classified_as_overflow() -> None:
    out = _classify_litellm_error(
        Exception("This model's maximum context length is 8192 tokens"),
    )
    assert isinstance(out, ContextOverflowError)


# ============================================================
# 6. 兜底：未知错误 → InvalidRequestError（不重试）
# ============================================================


def test_unknown_error_classified_as_invalid_request() -> None:
    out = _classify_litellm_error(Exception("some weird business error"))
    assert isinstance(out, InvalidRequestError)
    assert out.kind == "invalid_request"


# ============================================================
# 7. 关键回归断言：connection / server / rate_limit 三类是 retryable_kinds
# ============================================================


def test_retryable_kinds_contains_all_three_transient_categories() -> None:
    """与 RetryConfig.retryable_kinds 默认值一致。"""
    from taifeng.llm.retry import RetryConfig
    cfg = RetryConfig()
    assert "transient_network" in cfg.retryable_kinds
    assert "server_error" in cfg.retryable_kinds
    assert "rate_limit" in cfg.retryable_kinds
    # invalid_request 不在重试集合 —— 防止真请求错误被无意义重试
    assert "invalid_request" not in cfg.retryable_kinds
