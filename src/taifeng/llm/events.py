"""LLM 流式响应事件枚举 —— 标准化跨 provider 事件流。

参照：
    - codex codex-rs/codex-api/src/common.rs::ResponseEvent (12 variants)
    - claw-code crates/runtime/src/conversation.rs::AssistantEvent (6 variants)

设计选择：采用 codex 路线，事件粒度更细，方便业务侧按需聚合到 ``EventMsg``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from taifeng.llm.types import RateLimitSnapshot, TokenUsage

EventKind = Literal[
    "created",
    "text_delta",
    "tool_call_delta",
    "tool_call_done",
    "reasoning_delta",
    "rate_limits",
    "server_model",
    "prompt_cache",
    "structured_output",
    "normalized_output",
    "completed",
    "error",
]


class ResponseEvent(BaseModel):
    """LLM 流式响应事件统一格式。

    10 类事件覆盖 LLM 生命周期。
    """

    kind: EventKind
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 强类型 helper：构造器（避免 dict 拼写错误）
# ---------------------------------------------------------------------------


def created() -> ResponseEvent:
    return ResponseEvent(kind="created")


def text_delta(text: str) -> ResponseEvent:
    return ResponseEvent(kind="text_delta", data={"text": text})


def tool_call_delta(call_id: str, name: str | None, delta: str) -> ResponseEvent:
    return ResponseEvent(
        kind="tool_call_delta",
        data={"call_id": call_id, "name": name, "delta": delta},
    )


def tool_call_done(call_id: str, name: str, arguments: str) -> ResponseEvent:
    return ResponseEvent(
        kind="tool_call_done",
        data={"call_id": call_id, "name": name, "arguments": arguments},
    )


def reasoning_delta(delta: str) -> ResponseEvent:
    return ResponseEvent(kind="reasoning_delta", data={"delta": delta})


def rate_limits(snapshot: RateLimitSnapshot) -> ResponseEvent:
    return ResponseEvent(kind="rate_limits", data=snapshot.model_dump())


def server_model(model: str) -> ResponseEvent:
    return ResponseEvent(kind="server_model", data={"model": model})


def prompt_cache(
    *,
    cache_read: int,
    cache_creation: int,
    previous_cache_read: int = 0,
) -> ResponseEvent:
    """Cache 读/写统计事件。``previous_cache_read`` 用于 break 检测。"""
    return ResponseEvent(
        kind="prompt_cache",
        data={
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "previous_cache_read_input_tokens": previous_cache_read,
            "token_drop": max(0, previous_cache_read - cache_read),
        },
    )


def structured_output(
    *,
    parsed: dict[str, Any] | list[Any],
    raw_text: str,
) -> ResponseEvent:
    """LLM 强类型输出事件 —— provider 在 completed 之前 emit。

    仅当 ApiRequest.response_format 非 None 且响应文本能成功 ``json.loads`` 时 emit。
    解析失败 provider 应改 emit ``error(kind="parse_error", retryable=False)``，不 emit 本事件。
    """
    return ResponseEvent(
        kind="structured_output",
        data={"parsed": parsed, "raw_text": raw_text},
    )


def normalized_output(items: list[dict[str, Any]]) -> ResponseEvent:
    """仅供 TurnRunner 消费的 terminal 有序输出；不得桥接到业务事件。"""
    return ResponseEvent(kind="normalized_output", data={"items": items})


def completed(
    *,
    response_id: str | None,
    usage: TokenUsage,
    end_turn: bool | None = True,
    request_id: str | None = None,
) -> ResponseEvent:
    return ResponseEvent(
        kind="completed",
        data={
            "response_id": response_id,
            "usage": usage.model_dump(),
            "end_turn": end_turn,
            # G3：服务端 request-id（成功路径回流，供工单关联）
            "request_id": request_id,
        },
    )


def error(*, message: str, kind: str = "provider_error", retryable: bool = False) -> ResponseEvent:
    return ResponseEvent(
        kind="error",
        data={"message": message, "kind": kind, "retryable": retryable},
    )
