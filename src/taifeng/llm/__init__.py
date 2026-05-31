"""LLM 客户端 —— ModelClient、ResponseEvent、多 provider 适配。

设计文档：docs/architecture/llm-client.md
"""

from taifeng.llm.client import ModelClient, ModelClientSession
from taifeng.llm.errors import (
    AuthenticationError,
    CancelledError,
    ContentFilterError,
    ContextOverflowError,
    FailureClass,
    InvalidRequestError,
    LLMError,
    RateLimitError,
    RequestTooLargeError,
    ServerError,
    TransientNetworkError,
    classify_failure,
    suggested_action_for,
)
from taifeng.llm.events import EventKind, ResponseEvent
from taifeng.llm.recovery import RecoveryPlan, RecoveryStep, recommend_recovery
from taifeng.llm.retry import RetryConfig, retry_async
from taifeng.llm.types import (
    ApiMessage,
    ApiRequest,
    CacheBreakpoint,
    RateLimitSnapshot,
    ResponseFormatSpec,
    TokenUsage,
    ToolSpecRef,
)

__all__ = [
    "ApiMessage",
    "ApiRequest",
    "AuthenticationError",
    "CacheBreakpoint",
    "CancelledError",
    "ContentFilterError",
    "ContextOverflowError",
    "EventKind",
    "FailureClass",
    "InvalidRequestError",
    "LLMError",
    "ModelClient",
    "ModelClientSession",
    "RateLimitError",
    "RateLimitSnapshot",
    "RecoveryPlan",
    "RecoveryStep",
    "RequestTooLargeError",
    "ResponseEvent",
    "ResponseFormatSpec",
    "RetryConfig",
    "ServerError",
    "TokenUsage",
    "ToolSpecRef",
    "TransientNetworkError",
    "classify_failure",
    "recommend_recovery",
    "retry_async",
    "suggested_action_for",
]
