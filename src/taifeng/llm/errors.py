"""LLM 调用相关异常分类。

参照：
    - codex codex-rs/core/src/error.rs
    - claw-code crates/api/src/error.rs（safe_failure_class + suggested_action）
"""

from __future__ import annotations

from typing import Literal

# === G3：稳定的失败分类桶 ===
# 这是供 telemetry 聚合的**稳定字符串**（与 ``kind`` 区分：kind 偏内部语义，
# failure_class 是对外稳定契约，命名对齐 claw-code，跨版本不轻易改）。
FailureClass = Literal[
    "context_window",  # 上下文超窗
    "provider_auth",  # 鉴权失败
    "provider_rate_limit",  # 限流
    "provider_transport",  # 网络/传输层瞬时错误
    "provider_internal",  # provider 5xx 内部错误
    "invalid_request",  # 请求参数非法（模型名 / schema / 消息结构）
    "content_filter",  # 内容安全拦截
    "provider_unreliable_finish",  # provider/网关未如实上报终止原因（finish_reason 不可信）
    "cancelled",  # 调用被取消
    "request_size",  # 请求体过大（发送前预检，G2b body-size 用）
    "runtime_io",  # 本地 IO 错误（非 LLM）
    "unknown",  # 未分类
]

# 每个 failure_class 的人类可读处置建议（telemetry / HITL UI 展示用）。
_SUGGESTED_ACTION: dict[FailureClass, str] = {
    "context_window": "压缩或精简上下文后重试，或开新 thread",
    "provider_auth": "检查 API key / 凭据配置",
    "provider_rate_limit": "退避后重试；降低并发或申请更高配额",
    "provider_transport": "网络抖动，退避后自动重试通常可恢复",
    "provider_internal": "provider 端故障，退避后重试",
    "invalid_request": "请求参数非法，检查模型名 / 工具 schema / 消息结构",
    "content_filter": "内容被安全策略拦截，调整输入后重试",
    "provider_unreliable_finish": (
        "网关未如实上报终止原因（常见于兼容网关把上游未知 finishReason 塌缩成 content_filter），"
        "退避后重试通常可恢复"
    ),
    "cancelled": "调用被取消，无需处理",
    "request_size": "请求体过大，精简消息 / 附件后重试",
    "runtime_io": "本地 IO 错误，检查磁盘 / 文件路径 / 权限",
    "unknown": "未分类错误，查看日志与 telemetry 详情",
}


def suggested_action_for(failure_class: FailureClass) -> str:
    """返回某个 failure_class 的稳定处置建议（缺失回退到 unknown）。"""
    return _SUGGESTED_ACTION.get(failure_class, _SUGGESTED_ACTION["unknown"])


class LLMError(Exception):
    """LLM 调用错误基类。"""

    retryable: bool = False
    kind: str = "llm_error"
    failure_class: FailureClass = "unknown"
    request_id: str | None = None
    """G3：provider 响应的服务端 request-id（provider 在抛出前回填，供工单关联）。"""

    @property
    def suggested_action(self) -> str:
        """本错误对应的稳定处置建议（供 telemetry / HITL 展示）。"""
        return suggested_action_for(self.failure_class)


class RateLimitError(LLMError):
    retryable = True
    kind = "rate_limit"
    failure_class: FailureClass = "provider_rate_limit"

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TransientNetworkError(LLMError):
    retryable = True
    kind = "transient_network"
    failure_class: FailureClass = "provider_transport"


class ServerError(LLMError):
    retryable = True
    kind = "server_error"
    failure_class: FailureClass = "provider_internal"


class ContentFilterError(LLMError):
    retryable = False
    kind = "content_filter"
    failure_class: FailureClass = "content_filter"


class UnreliableFinishError(LLMError):
    """流以**不可信的** finish_reason 终止，且本次调用零产出。

    仅在接入方显式声明端点 ``trust_finish_reason=False`` 时抛出。动因：某些
    OpenAI 兼容网关会把上游一切未枚举的终止原因塌缩成 ``content_filter``
    （实测 new-api v1.0.0-rc.25 的 Gemini→OpenAI 响应转换 ``default:`` 分支），
    使得「模型输出了畸形 tool call」这类**瞬时可重试**故障被伪装成「内容被安全
    策略拦截」这类**终态**故障。此时该标签不具判别力，按可重试处置。

    与 ``ContentFilterError`` 的分工：端点可信时真安全拦截仍走前者（终态）。
    """

    retryable = True
    kind = "unreliable_finish"
    failure_class: FailureClass = "provider_unreliable_finish"


class ContextOverflowError(LLMError):
    """上下文 token 超出 provider 限制。压缩处理，不重试。"""

    retryable = False
    kind = "context_overflow"
    failure_class: FailureClass = "context_window"


class AuthenticationError(LLMError):
    retryable = False
    kind = "authentication"
    failure_class: FailureClass = "provider_auth"


class InvalidRequestError(LLMError):
    retryable = False
    kind = "invalid_request"
    failure_class: FailureClass = "invalid_request"


class UnsupportedModalityError(InvalidRequestError):
    """业务策略或客户端能力未允许指定输入模态。"""

    kind = "unsupported_modality"


class UnsupportedCombinationError(InvalidRequestError):
    """模型、协议与请求选项的组合不受支持。"""

    kind = "unsupported_combination"


class InvalidHistoryError(InvalidRequestError):
    """规范历史无法被目标 provider/protocol 无损重放。"""

    kind = "invalid_history"


class InvalidResponseError(InvalidRequestError):
    """provider terminal 响应缺失、重复或与 preview 不一致。"""

    kind = "invalid_response"


class UnsupportedPersistenceCapabilityError(InvalidRequestError):
    """目标协议要求的 durable store 能力未提供。"""

    kind = "unsupported_persistence_capability"


class InvalidImageError(InvalidRequestError):
    """图片 canonical 内容、签名或维度不符合输入契约。"""

    kind = "invalid_image"


class ImageCountExceededError(InvalidRequestError):
    """图片数量超过业务策略允许的上限。"""

    kind = "image_count_exceeded"


class CancelledError(LLMError):
    retryable = False
    kind = "cancelled"
    failure_class: FailureClass = "cancelled"


class RequestTooLargeError(LLMError):
    """发送前预检：请求体字节数超出 ``ContextBudget.max_request_bytes``。

    在发请求前主动失败，比等 provider 返回 4xx 更快更清晰（G2b 硬护栏）。
    """

    retryable = False
    kind = "request_too_large"
    failure_class: FailureClass = "request_size"

    def __init__(self, message: str, *, estimated_bytes: int, max_bytes: int) -> None:
        super().__init__(message)
        self.estimated_bytes = estimated_bytes
        self.max_bytes = max_bytes


class AttachmentTooLargeError(RequestTooLargeError):
    """单张图片或图片累计 decoded bytes 超过策略上限。"""

    kind = "attachment_too_large"


def classify_failure(exc: BaseException) -> tuple[FailureClass, str]:
    """把任意异常归类到稳定 failure_class，并给出处置建议。

    - LLMError 子类：用其声明的 ``failure_class``
    - OSError（本地 IO）：``runtime_io``
    - 其他：``unknown``

    Returns:
        (failure_class, suggested_action) 二元组。
    """
    if isinstance(exc, LLMError):
        return exc.failure_class, exc.suggested_action
    if isinstance(exc, OSError):
        return "runtime_io", suggested_action_for("runtime_io")
    return "unknown", suggested_action_for("unknown")
