"""native provider 共享纯函数 —— 错误分类 / SSE 解析 / usage 字段提取。

本模块刻意只放**纯函数**（无 IO、无可变状态），让三家 native session
（OpenAICompat / Anthropic / Gemini）+ DeepSeek 薄子类共享统一的：

- HTTP 错误 → ``LLMError`` 分类（基于 status code + body 关键字）
- SSE 行解析（OpenAI / Gemini 单行 `data: {...}` 与 Anthropic 双行 `event:\\ndata:`）
- usage 字段映射（OpenAI 标准 / Anthropic / DeepSeek 三种 cache 字段）

参照：codex codex-rs/core/src/error.rs + claw-code crates/api/src/client.rs
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from taifeng.llm.errors import (
    AuthenticationError,
    ContentFilterError,
    ContextOverflowError,
    InvalidRequestError,
    LLMError,
    RateLimitError,
    ServerError,
    TransientNetworkError,
)
from taifeng.llm.types import RateLimitSnapshot, TokenUsage

# ---------------------------------------------------------------------------
# G3：响应头解析（request-id 回流 + 结构化 rate-limit 窗口）
# ---------------------------------------------------------------------------

# 服务端 request-id 头兜底链（覆盖 OpenAI / Anthropic / Azure / AWS / Cloudflare）
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "anthropic-request-id",
    "x-amzn-requestid",
    "x-ms-request-id",
    "cf-ray",
)

# 时长 token：数字 + 单位（ms/s/m/h）。OpenAI reset 形如 "1s" / "6m0s" / "100ms"。
_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_DURATION_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def extract_request_id(headers: Mapping[str, str]) -> str | None:
    """从响应头按兜底链提取服务端 request-id（用于支持工单关联）。"""
    for key in _REQUEST_ID_HEADERS:
        value = headers.get(key)
        if value:
            return value
    return None


def _parse_reset_duration(value: str | None) -> float | None:
    """把 reset 头解析为秒数。支持纯数字（秒）与 "6m0s"/"100ms" 形式；无法解析→None。"""
    if value is None:
        return None
    value = value.strip()
    try:
        return float(value)  # 纯数字 → 秒
    except ValueError:
        pass
    total = 0.0
    matched = False
    for num, unit in _DURATION_TOKEN.findall(value):
        matched = True
        total += float(num) * _DURATION_UNIT_SECONDS[unit]
    return total if matched else None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def extract_rate_limit_snapshot(
    headers: Mapping[str, str],
) -> RateLimitSnapshot | None:
    """解析 OpenAI / Anthropic 家族的 ``*ratelimit*`` 头 → RateLimitSnapshot。

    无任何 ratelimit 头时返回 None（provider 据此决定是否 emit rate_limits 事件）。
    credits / 余额属业务范畴（R1 排除），此处只取「窗口剩余 + 重置」。
    """
    raw = {k: v for k, v in headers.items() if "ratelimit" in k.lower()}
    if not raw:
        return None
    req_rem = headers.get("x-ratelimit-remaining-requests") or headers.get(
        "anthropic-ratelimit-requests-remaining"
    )
    tok_rem = headers.get("x-ratelimit-remaining-tokens") or headers.get(
        "anthropic-ratelimit-tokens-remaining"
    )
    return RateLimitSnapshot(
        requests_remaining=_to_int(req_rem),
        requests_reset_seconds=_parse_reset_duration(
            headers.get("x-ratelimit-reset-requests")
        ),
        tokens_remaining=_to_int(tok_rem),
        tokens_reset_seconds=_parse_reset_duration(
            headers.get("x-ratelimit-reset-tokens")
        ),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# HTTP 错误分类
# ---------------------------------------------------------------------------

# body lower 含以下关键字 → ContextOverflowError（status 必须 400 类）
_CONTEXT_OVERFLOW_KEYWORDS = (
    "context_length",
    "context length",
    "maximum context",
    "maximum tokens",
    "too long",
    "exceed",
)

# body lower 含以下关键字 → ContentFilterError
_CONTENT_FILTER_KEYWORDS = (
    "content_filter",
    "safety",
    "blocked",
    "violat",
)


def classify_http_error(status: int, body: str, *, provider: str = "openai") -> LLMError:
    """根据 HTTP status code + body 关键字把上游错误分类到 ``LLMError`` 子类。

    优先级（高 → 低）：
        429 → RateLimitError（带 retry_after_seconds）
        408 → TransientNetworkError（请求超时，可重试）
        401/403 → AuthenticationError
        >=500 → ServerError（可重试）
        4xx + body 关键字 → ContextOverflowError / ContentFilterError
        其他 → InvalidRequestError（不可重试）

    ``provider`` 参数当前用于在 body json 中解析 provider-specific 字段（如
    Anthropic 的 ``error.type``）；通用路径不依赖它。
    """
    lower = body.lower()

    # 429 优先：含 retry_after 的 rate limit
    if status == 429:
        retry_after = _parse_retry_after(body)
        return RateLimitError(body, retry_after_seconds=retry_after)

    if status == 408:
        return TransientNetworkError(body)

    if status in (401, 403):
        return AuthenticationError(body)

    if status >= 500:
        return ServerError(body)

    # 4xx 区段先看关键字
    if any(kw in lower for kw in _CONTEXT_OVERFLOW_KEYWORDS):
        return ContextOverflowError(body)
    if any(kw in lower for kw in _CONTENT_FILTER_KEYWORDS):
        return ContentFilterError(body)

    return InvalidRequestError(body)


def _parse_retry_after(body: str) -> float | None:
    """从 body JSON 提取 retry_after_seconds —— 兼容 OpenAI / Anthropic / DeepSeek。

    OpenAI: ``{"error": {"retry_after": 30}}``
    Anthropic: ``{"error": {"type": "rate_limit_error"}}``（无 retry_after，返回 None）
    通用：顶层 ``retry_after`` 字段
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # 嵌套 error.retry_after
    err = data.get("error")
    if isinstance(err, dict):
        ra = err.get("retry_after")
        if isinstance(ra, (int, float)):
            return float(ra)
    # 顶层 retry_after
    ra = data.get("retry_after")
    if isinstance(ra, (int, float)):
        return float(ra)
    return None


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------

# `data: [DONE]` 标记 → None（流结束）；解析失败 → None 静默跳过
def parse_sse_data(line: str) -> dict[str, Any] | None:
    """解析 OpenAI / Gemini 风格的 SSE 单行 ``data: {json}``。

    返回 dict 表示有效 payload；返回 None 表示该行应跳过（空行 / 注释 /
    ``[DONE]`` 标记 / json 解析失败）。
    """
    if not line:
        return None
    if line.startswith(":"):  # SSE comment
        return None
    if not line.startswith("data:"):
        return None
    payload = line[5:].lstrip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_sse_event(
    lines: list[str],
) -> tuple[str | None, dict[str, Any] | None]:
    """解析 Anthropic 风格的 SSE 事件（``event: <name>\\ndata: <json>`` 双行）。

    返回 ``(event_name, payload)`` 元组。任一为 None 表示该事件不完整或解析
    失败 —— 调用方应跳过。
    """
    event_name: str | None = None
    payload: dict[str, Any] | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or None
        elif line.startswith("data:"):
            payload = parse_sse_data(line)
    return event_name, payload


# ---------------------------------------------------------------------------
# Usage 提取（OpenAI 家族 —— 含 DeepSeek）
# ---------------------------------------------------------------------------


def extract_usage_openai_family(raw: dict[str, Any]) -> TokenUsage:
    """把 OpenAI chat/completions 风格的 ``usage`` 对象解析为 ``TokenUsage``。

    cache_read_input_tokens 字段查找优先级：
        1. ``raw["cache_read_input_tokens"]``（Anthropic 风格 / 部分 OpenAI 版本）
        2. ``raw["prompt_tokens_details"]["cached_tokens"]``（OpenAI 标准）
        3. ``raw["prompt_cache_hit_tokens"]``（DeepSeek 特有）

    DeepSeek 还有 ``prompt_cache_miss_tokens`` —— 当前不映射，业务侧通过
    ``TokenUsage.raw`` 读原始值。
    """
    pt = int(raw.get("prompt_tokens", 0) or 0)
    ct = int(raw.get("completion_tokens", 0) or 0)
    tt = int(raw.get("total_tokens", pt + ct) or (pt + ct))

    # cache_read 三优先级查找
    cache_read = raw.get("cache_read_input_tokens")
    if not cache_read:
        pt_details = raw.get("prompt_tokens_details")
        if isinstance(pt_details, dict):
            cache_read = pt_details.get("cached_tokens")
    if not cache_read:
        cache_read = raw.get("prompt_cache_hit_tokens")
    cache_read_int = int(cache_read or 0)

    cache_creation = int(raw.get("cache_creation_input_tokens", 0) or 0)

    # reasoning_tokens（OpenAI o1 / DeepSeek R1）
    ct_details = raw.get("completion_tokens_details")
    reasoning = 0
    if isinstance(ct_details, dict):
        reasoning = int(ct_details.get("reasoning_tokens", 0) or 0)

    return TokenUsage(
        input_tokens=pt,
        output_tokens=ct,
        total_tokens=tt,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read_int,
        reasoning_tokens=reasoning,
        raw=raw,
    )


def extract_usage_anthropic(raw: dict[str, Any]) -> TokenUsage:
    """把 Anthropic messages API 的 ``usage`` 对象解析为 ``TokenUsage``。

    Anthropic 字段：``input_tokens`` / ``output_tokens`` /
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens``。
    """
    it = int(raw.get("input_tokens", 0) or 0)
    ot = int(raw.get("output_tokens", 0) or 0)
    cc = int(raw.get("cache_creation_input_tokens", 0) or 0)
    cr = int(raw.get("cache_read_input_tokens", 0) or 0)
    return TokenUsage(
        input_tokens=it,
        output_tokens=ot,
        total_tokens=it + ot,
        cache_creation_input_tokens=cc,
        cache_read_input_tokens=cr,
        reasoning_tokens=0,
        raw=raw,
    )


def extract_usage_gemini(raw: dict[str, Any]) -> TokenUsage:
    """把 Gemini ``usageMetadata`` 解析为 ``TokenUsage``。

    Gemini 字段：``promptTokenCount`` / ``candidatesTokenCount`` /
    ``totalTokenCount`` / ``cachedContentTokenCount``。
    """
    pt = int(raw.get("promptTokenCount", 0) or 0)
    ct = int(raw.get("candidatesTokenCount", 0) or 0)
    tt = int(raw.get("totalTokenCount", pt + ct) or (pt + ct))
    cr = int(raw.get("cachedContentTokenCount", 0) or 0)
    return TokenUsage(
        input_tokens=pt,
        output_tokens=ct,
        total_tokens=tt,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cr,
        reasoning_tokens=0,
        raw=raw,
    )


__all__ = [
    "classify_http_error",
    "extract_usage_anthropic",
    "extract_usage_gemini",
    "extract_usage_openai_family",
    "parse_sse_data",
    "parse_sse_event",
]
