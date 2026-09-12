"""native provider 共享边界 —— 错误分类 / 可取消 SSE / usage 字段提取。

除可取消 SSE 迭代器外，本模块只放纯函数，让三家 native session
（OpenAICompat / Anthropic / Gemini）+ DeepSeek 薄子类共享统一的：

- HTTP 错误 → ``LLMError`` 分类（基于 status code + body 关键字）
- SSE 行解析（OpenAI / Gemini 单行 `data: {...}` 与 Anthropic 双行 `event:\\ndata:`）
- usage 字段映射（OpenAI 标准 / Anthropic / DeepSeek 三种 cache 字段）

参照：codex codex-rs/core/src/error.rs + claw-code crates/api/src/client.rs
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from taifeng.loop.cancellation import CancellationToken

from taifeng.llm.errors import (
    AuthenticationError,
    ContentFilterError,
    ContextOverflowError,
    InvalidHistoryError,
    InvalidRequestError,
    InvalidResponseError,
    LLMError,
    RateLimitError,
    ServerError,
    TransientNetworkError,
    TransportPhase,
    UnsupportedModalityError,
)
from taifeng.llm.types import (
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    RateLimitSnapshot,
    TokenUsage,
)


def assert_text_only_request(request: ApiRequest) -> None:
    """在序列化前拒绝图片与不透明 provider state。

    旧 provider adapter 只能消费兼容 messages view；显式检查可避免 Pydantic
    part 泄漏到 JSON encoder，或在切换协议时静默丢失 Responses 状态。
    """
    if any(isinstance(item, ApiProviderStateItem) for item in request.input_items):
        raise InvalidHistoryError("provider state is not supported by this protocol")
    for message in request.messages:
        if isinstance(message.content, list) and any(
            isinstance(part, ImagePart) for part in message.content
        ):
            raise UnsupportedModalityError("image input is not supported by this client")

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


# ---------------------------------------------------------------------------
# Responses 流内失败事件归一（ADR 0033）
# ---------------------------------------------------------------------------

# 官方 error code → 归类桶。按**子串**匹配（小写）：``ResponseErrorCode`` 是持续
# 演进的开放集合，中转网关还会自造码，硬编码闭集必然漏。元组顺序即优先级。
_STREAM_ERROR_CODE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("rate_limit", "too_many_requests"), "rate_limit"),
    (("timeout", "timed_out", "deadline_exceeded"), "transient"),
    (("invalid_api_key", "authentication", "unauthorized", "permission_denied"), "auth"),
    (("content_filter", "content_policy", "moderation"), "content_filter"),
    (("context_length", "context_window", "max_tokens"), "context_overflow"),
    (("server_error", "internal_error", "internal_server", "service_unavailable"), "server"),
)

# ``incomplete_details.reason`` 在 openai-openapi 里是**闭集**，只有这两个值。
# 二者都不是「响应畸形」，各有确定语义，必须分开归类而不是一律 invalid_response。
_INCOMPLETE_REASONS = ("content_filter", "max_output_tokens")


def _error_object_fields(container: object) -> tuple[object, object, object, object]:
    """从一个 error 对象里取 ``(code, message, param, type)``。

    ``type`` 也带出来参与归类：中转网关常把真实语义只写在它上面
    （实测 ``{"type": "service_unavailable_error", "code": "server_is_overloaded"}``）。
    """
    if not isinstance(container, dict):
        return None, None, None, None
    return (
        container.get("code"),
        container.get("message"),
        container.get("param"),
        container.get("type"),
    )


def _stream_failure_detail(
    kind: str, code: object, message: object, param: object
) -> str:
    """把官方字段拼成一条不丢信息的可读描述（code / param / message 全带出）。"""
    parts = [kind]
    if isinstance(code, str) and code:
        parts.append(f"code={code}")
    if isinstance(param, str) and param:
        parts.append(f"param={param}")
    text = message if isinstance(message, str) and message else "<no message>"
    parts.append(text)
    return " | ".join(parts)


def _retry_after_hint(*containers: object) -> float | None:
    """从结构化 error 对象里取 ``retry_after`` 数值提示（秒）。

    流内失败事件给的是**字段**而非 JSON body，所以不能复用 ``_parse_retry_after``
    （它 ``json.loads`` 一段 body 文本；拿散文 message 喂它必然解析失败，导致流内
    ``RateLimitError.retry_after_seconds`` 恒为 None，服务端提示白给）。
    """
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("retry_after_seconds", "retry_after"):
            value = container.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value >= 0:
                return float(value)
    return None


def _stream_error_from_bucket(
    bucket: str, detail: str, retry_after: float | None, message: str
) -> LLMError:
    """按归类桶构造对应 ``LLMError``（rate_limit 带上服务端 retry hint）。

    hint 取值优先级：**结构化字段** > message 里的 JSON body。后者是历史兼容——
    个别上游会把整段 JSON 塞进 message，``_parse_retry_after`` 能从中捞出来。
    """
    if bucket == "rate_limit":
        hint = retry_after if retry_after is not None else _parse_retry_after(message)
        return RateLimitError(detail, retry_after_seconds=hint)
    if bucket == "transient":
        return TransientNetworkError(detail)
    if bucket == "auth":
        return AuthenticationError(detail)
    if bucket == "content_filter":
        return ContentFilterError(detail)
    if bucket == "context_overflow":
        return ContextOverflowError(detail)
    return ServerError(detail)


def classify_responses_stream_failure(event: dict[str, Any]) -> LLMError:
    """把 Responses **流内**失败事件归一为 typed ``LLMError``（ADR 0033）。

    覆盖 openai-openapi 定义的三种流内失败终态：

    - ``error``（``ResponseErrorEvent``）：``code: str|null`` / ``message: str`` /
      ``param: str|null`` / ``sequence_number``；
    - ``response.failed``：``response.error`` = ``{code, message}``（非 null 时两者必填）；
    - ``response.incomplete``：``response.incomplete_details.reason``，闭集
      ``content_filter`` | ``max_output_tokens``。

    为什么归一而不是吞掉：这些事件带着 provider 的**真实失败语义**。旧实现一律塌缩成
    ``InvalidResponseError``（``retryable=False`` / ``failure_class=invalid_request``），
    把限流、5xx、超时这类**瞬时**故障伪装成确定性客户端错误——上层 policy 于是既不
    重试也不挂起，直接判死一个本可恢复的 turn。

    默认取值的依据：官方对流内 error 事件的描述是「This can happen due to an internal
    server error or a timeout」，即**默认属 provider 侧瞬时故障**，故无法识别 code 的
    ``error`` / ``response.failed`` 归 :class:`ServerError`（可重试）。``response.incomplete``
    的 reason 是闭集，出现集合外的值属协议违规，仍归 :class:`InvalidResponseError`。

    Args:
        event: 已解析的 SSE data object（``type`` 必须是上述三者之一）。

    Returns:
        带正确 ``failure_class`` / ``retryable`` 的 ``LLMError``；其 message 携带
        官方 code / param / message 原文，不丢诊断信息。
    """
    kind = str(event.get("type", "unknown"))

    if kind == "response.incomplete":
        response = event.get("response")
        details = response.get("incomplete_details") if isinstance(response, dict) else None
        reason = details.get("reason") if isinstance(details, dict) else None
        detail = _stream_failure_detail(kind, reason, "response incomplete", None)
        if reason == "content_filter":
            return ContentFilterError(detail)
        if reason == "max_output_tokens":
            # 输出被上限截断：非畸形、非瞬时。归 context_window 桶 —— 其恢复配方
            # （压缩后重试一次）正是这里有意义的动作，且 turn 侧有界自愈只跑一次。
            return ContextOverflowError(detail)
        return InvalidResponseError(detail)

    if kind == "response.failed":
        response = event.get("response")
        raw_error = response.get("error") if isinstance(response, dict) else None
        code, message, param, err_type = _error_object_fields(raw_error)
        retry_after = _retry_after_hint(raw_error, response)
    else:
        # 官方 ResponseErrorEvent 是**扁平**的（code/message/param 在顶层）；
        # 但实测中转网关会发嵌套变体：
        #   {"type":"error","error":{"type":"service_unavailable_error",
        #    "code":"server_is_overloaded","message":"..."},"sequence_number":3}
        # 顶层读不到就回落到嵌套 error 对象——否则整条诊断被吃掉（报成
        # "<no message>"），且下面的 code 归类规则拿不到 code，只能靠兜底
        # 归成通用 ServerError：嵌套的 rate_limit 会因此丢掉 retry_after。
        code, message, param = event.get("code"), event.get("message"), event.get("param")
        err_type = None
        nested = event.get("error")
        if code is None and message is None:
            code, message, param, err_type = _error_object_fields(nested)
        retry_after = _retry_after_hint(nested, event)

    detail = _stream_failure_detail(kind, code, message, param)
    # code + type + message 一起参与匹配：真实原因可能只写在其中任意一个上
    code_text = code if isinstance(code, str) else ""
    message_text = message if isinstance(message, str) else ""
    type_text = err_type if isinstance(err_type, str) else ""
    text = f"{code_text} {type_text} {message_text}".lower()
    for needles, bucket in _STREAM_ERROR_CODE_RULES:
        if any(needle in text for needle in needles):
            return _stream_error_from_bucket(
                bucket, detail, retry_after, message_text
            )
    # code 无法识别时再看正文关键字（与 HTTP 分类共用同一张表）
    if any(kw in text for kw in _CONTEXT_OVERFLOW_KEYWORDS):
        return ContextOverflowError(detail)
    if any(kw in text for kw in _CONTENT_FILTER_KEYWORDS):
        return ContentFilterError(detail)
    return ServerError(detail)


def transport_phase_of(exc: BaseException) -> TransportPhase:
    """把 httpx 传输异常判为 ``connect`` / ``stream`` 相位。

    按异常**类型**判定（参照 codex ``is_connect()``），不看消息文本——消息文本随
    httpx 版本与底层 OS error 变化，拿来判相位不稳定。

    Args:
        exc: httpx 抛出的传输层异常（``TransportError`` 家族，含 ``TimeoutException``）。

    Returns:
        ``connect``：连接建立阶段失败（连不上 / 连接超时 / 连接池取连接超时），
        此时必然尚无内容产出；``stream``：其余（读写超时、读写错误、协议错误，
        尤其 mid-stream 的 ``RemoteProtocolError``）。未知类型保守归 ``stream``。
    """
    import httpx  # 惰性 import：与各 provider ``stream()`` 内的用法保持一致

    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return "connect"
    return "stream"


def transport_error(exc: BaseException, *, provider: str = "") -> TransientNetworkError:
    """把 httpx 传输异常统一归为带相位的 ``TransientNetworkError``。

    五家 provider 的 ``stream()`` 共用本函数，保证「相位判定 + 消息形状」一致。

    **消息只取异常类型名，绝不插值原始异常**：httpx 异常的 ``str()`` 可能带上请求
    URL（含代理地址 / query 参数），进日志即泄漏（参照 codex ``without_url``）。
    诊断所需的区分度由「相位 + 类型名」提供，如 ``(stream): RemoteProtocolError``。

    Args:
        exc: 捕获到的 httpx 传输层异常。
        provider: provider 名前缀，用于在多 provider 日志里辨识来源；空则不加前缀。

    Returns:
        已带 ``transport_phase`` 的 ``TransientNetworkError``（``retryable=True``，
        ``failure_class=provider_transport``，不新增 FailureClass 桶）。
    """
    phase = transport_phase_of(exc)
    prefix = f"{provider} " if provider else ""
    return TransientNetworkError(
        f"{prefix}transport error ({phase}): {type(exc).__name__}",
        transport_phase=phase,
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


async def iter_lines_with_cancel(
    response: Any,
    cancel: CancellationToken,
) -> AsyncIterator[str]:
    """竞争下一行与 turn token，让 stalled HTTP read 也能被立即取消。"""
    iterator = response.aiter_lines().__aiter__()
    while True:
        line_task = asyncio.create_task(anext(iterator))
        cancel_task = asyncio.create_task(cancel.wait_cancelled())
        try:
            done, _ = await asyncio.wait(
                {line_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                line_task.cancel()
                await asyncio.gather(line_task, return_exceptions=True)
                cancel.raise_if_cancelled()
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            try:
                line = line_task.result()
            except StopAsyncIteration:
                return
            yield line
        except BaseException:
            line_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(line_task, cancel_task, return_exceptions=True)
            raise

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

logger = logging.getLogger(__name__)

_ACCOUNTING_NOISE_SEEN: set[str] = set()
"""已告警过的记账字段标签——同一坏字段只吼一次，避免每 turn 刷屏。"""


def _coerce_count(value: object) -> int | None:
    """把 usage 计数强制成**非负**整数；无法解析或语义无效返回 ``None``。

    只负责「这个值能不能当计数用」，不替调用方决定「用不了算不算致命」——
    主计数与记账字段的处置完全不同（见下面两个调用点）。

    判为用不了的三类：
      - 解析不了（``"abc"`` / dict / list）；
      - ``bool`` —— 它是 ``int`` 子类，``int(True) == 1`` 会把一个布尔标志
        蒙混成计数；
      - 负数 —— token 计数不可能为负，放行会污染 ``cache_hit_ratio`` 之类的
        下游计算。

    宽容的部分同样明确：数字字符串（``"128"``）与浮点（``0.0`` / ``1.5``）照常
    接受——中转网关改一下序列化就会产出这些，它们是**表示法差异**而非坏数据。
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _spec_count(raw: dict[str, Any], *keys: str, default: int = 0) -> int:
    """读取**规范要求**的主计数（input / output / total）。

    这三个是 OpenAI Chat / Responses 规范里的必填整数，而且直接喂会话 token
    天花板等资源决策——坏值会让调度判断出错，所以 fail closed。但必须抛
    **分类过的** ``InvalidResponseError``，而不是让 ``int()`` 裸崩出
    ``ValueError`` / ``TypeError``：后者不是 ``LLMError``，失败策略分不了类，
    拿不到 SUSPEND / TERMINAL 处置。
    """
    for key in keys:
        if key not in raw:
            continue
        value = raw[key]
        if not value:  # 0 / None / "" —— 与历史 `or 0` 语义一致
            return default
        parsed = _coerce_count(value)
        if parsed is None:
            raise InvalidResponseError(
                f"usage {key} must be an integer, got {type(value).__name__}"
            )
        return parsed
    return default


def _accounting_count(value: object, label: str) -> int:
    """读取**纯记账**的可选计数（cache 命中 / reasoning tokens）。

    这些字段要么不是 OpenAI 正式顶层字段（``cache_*_input_tokens`` 是 Anthropic
    风格、``prompt_cache_hit_tokens`` 是 DeepSeek 特有），要么是可选明细。它们
    只用于观测 cache 命中率，**不影响输出正确性、也不驱动任何决策**。

    所以坏值一律按「缺失」处理并记一条日志，绝不判死一个已经成功产出内容的
    turn——上游随时会往 usage 里加新字段 / 改表示法，为一个我们只拿来记账的
    值把 turn 判死是不可辩护的（同 ADR 0030 对 SSE 未知帧的处置口径）。
    """
    if not value:  # 0 / None / "" —— 与历史 `or 0` 语义一致
        return 0
    parsed = _coerce_count(value)
    if parsed is not None:
        return parsed
    if label not in _ACCOUNTING_NOISE_SEEN:
        _ACCOUNTING_NOISE_SEEN.add(label)
        logger.warning(
            "usage 记账字段 %s 无法解析为整数（%r），按缺失处理；"
            "turn 不受影响，后续同类值不再重复告警",
            label, value,
        )
    return 0



def extract_usage_openai_family(raw: dict[str, Any]) -> TokenUsage:
    """把 OpenAI chat/completions 风格的 ``usage`` 对象解析为 ``TokenUsage``。

    cache_read_input_tokens 字段查找优先级：
        1. ``raw["cache_read_input_tokens"]``（Anthropic 风格 / 部分 OpenAI 版本）
        2. ``raw["prompt_tokens_details"]["cached_tokens"]``（OpenAI 标准）
        3. ``raw["prompt_cache_hit_tokens"]``（DeepSeek 特有）

    DeepSeek 还有 ``prompt_cache_miss_tokens`` —— 当前不映射，业务侧通过
    ``TokenUsage.raw`` 读原始值。
    """
    # 主计数：规范必填 + 驱动资源决策 → 坏值 fail closed（但抛分类错误）
    pt = _spec_count(raw, "prompt_tokens", "input_tokens")
    ct = _spec_count(raw, "completion_tokens", "output_tokens")
    tt = _spec_count(raw, "total_tokens", default=pt + ct)

    # cache_read 三优先级查找（纯记账 → 坏值按缺失处理，不判死 turn）
    cache_read = raw.get("cache_read_input_tokens")
    label = "cache_read_input_tokens"
    if not cache_read:
        pt_details = raw.get("prompt_tokens_details")
        if not isinstance(pt_details, dict):
            pt_details = raw.get("input_tokens_details")
        if isinstance(pt_details, dict):
            cache_read = pt_details.get("cached_tokens")
            label = "prompt_tokens_details.cached_tokens"
    if not cache_read:
        cache_read = raw.get("prompt_cache_hit_tokens")
        label = "prompt_cache_hit_tokens"
    cache_read_int = _accounting_count(cache_read, label)

    cache_creation = _accounting_count(
        raw.get("cache_creation_input_tokens"), "cache_creation_input_tokens"
    )

    # reasoning_tokens（OpenAI o1 / DeepSeek R1）—— 同为纯记账
    ct_details = raw.get("completion_tokens_details")
    if not isinstance(ct_details, dict):
        ct_details = raw.get("output_tokens_details")
    reasoning = 0
    if isinstance(ct_details, dict):
        reasoning = _accounting_count(
            ct_details.get("reasoning_tokens"), "completion_tokens_details.reasoning_tokens"
        )

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
    "classify_responses_stream_failure",
    "extract_usage_anthropic",
    "extract_usage_gemini",
    "extract_usage_openai_family",
    "parse_sse_data",
    "parse_sse_event",
]


# ── 异常终止原因分类（llm-provider-native 契约「流终止真相」）─────────────────
# 各家原生取值不同，规则同一份：安全拦截类 → ContentFilterError；函数调用畸形类
# → InvalidResponseError；其余（length / MAX_TOKENS / tool_calls …）不是失败。
# 调用方仅在**本次调用零内容产出**时询问本函数——已有产出说明模型确实干了活，
# 不作废（与 openai_compat 的既有判据一致）。
_CONTENT_FILTER_FINISHES = frozenset({
    "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
    "content_filter", "refusal",
})
_MALFORMED_FINISHES = frozenset({
    "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL",
})


def classify_abnormal_finish(
    finish_reason: str, *, provider: str,
) -> LLMError | None:
    """把异常终止原因映射到既有 LLMError 分类；正常终止返回 None。

    Args:
        finish_reason: provider 原生终止原因字符串。
        provider: provider 名（仅用于错误消息可读性）。

    Returns:
        ``ContentFilterError`` / ``InvalidResponseError``；不是异常终止则 None。
    """
    if finish_reason in _CONTENT_FILTER_FINISHES:
        return ContentFilterError(
            f"{provider}: response blocked by content policy "
            f"(finish_reason={finish_reason})"
        )
    if finish_reason in _MALFORMED_FINISHES:
        return InvalidResponseError(
            f"{provider}: response stopped by provider function call filter "
            f"(finish_reason={finish_reason})"
        )
    return None
