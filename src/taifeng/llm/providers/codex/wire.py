"""Codex ``codex-responses-v1`` 请求 wire 构造。"""

from __future__ import annotations

from typing import Any

from taifeng.llm.errors import InvalidHistoryError
from taifeng.llm.providers._mid_history import mid_history_system_text
from taifeng.llm.providers.openai._shared import (
    enforce_openai_wire_size,
    tool_output_content,
)
from taifeng.llm.types import (
    ApiFunctionCallItem,
    ApiFunctionCallOutputItem,
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    TextPart,
)


def _message_content(item: ApiMessageItem) -> list[dict[str, Any]]:
    """把 user/assistant content 投影为 Codex typed content parts。"""
    if item.role == "system":
        raise InvalidHistoryError("Codex system messages must use instructions")
    if isinstance(item.content, str):
        kind = "output_text" if item.role == "assistant" else "input_text"
        return [{"type": kind, "text": item.content}]
    content: list[dict[str, Any]] = []
    for part in item.content:
        if isinstance(part, TextPart):
            kind = "output_text" if item.role == "assistant" else "input_text"
            content.append({"type": kind, "text": part.text})
            continue
        if isinstance(part, ImagePart):
            if item.role != "user":
                raise InvalidHistoryError(
                    "Codex images are only valid in user messages"
                )
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{part.media_type};base64,{part.base64_data}",
                    "detail": part.detail,
                }
            )
    return content


def _reasoning_state(item: ApiProviderStateItem) -> dict[str, Any]:
    """exact-match Codex reasoning envelope，并白名单化 payload。"""
    state = item.state
    if (state.provider, state.protocol, state.item_type) != (
        "codex",
        "responses",
        "reasoning",
    ):
        raise InvalidHistoryError("foreign provider state cannot be replayed by Codex")
    allowed = {"id", "type", "encrypted_content", "summary", "status"}
    payload = state.payload
    if set(payload) - allowed or payload.get("type") != "reasoning":
        raise InvalidHistoryError("invalid Codex reasoning provider state")
    if not isinstance(payload.get("id"), str) or not payload["id"]:
        raise InvalidHistoryError("invalid Codex reasoning provider state id")
    encrypted = payload.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        raise InvalidHistoryError("invalid Codex reasoning encrypted state")
    if "summary" in payload and not isinstance(payload["summary"], list):
        raise InvalidHistoryError("invalid Codex reasoning summary")
    if "status" in payload and not isinstance(payload["status"], str):
        raise InvalidHistoryError("invalid Codex reasoning status")
    return dict(payload)


def _input_item(item: object) -> dict[str, Any]:
    """映射一个 provider-neutral ordered input item。"""
    if isinstance(item, ApiMessageItem):
        return {"type": "message", "role": item.role, "content": _message_content(item)}
    if isinstance(item, ApiFunctionCallItem):
        return {
            "type": "function_call",
            "call_id": item.call_id,
            "name": item.name,
            "arguments": item.arguments,
        }
    if isinstance(item, ApiFunctionCallOutputItem):
        return {
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": tool_output_content(item.output),
        }
    if isinstance(item, ApiProviderStateItem):
        return _reasoning_state(item)
    raise InvalidHistoryError(f"unsupported Codex input item: {type(item).__name__}")


def _partition_instructions(request: ApiRequest) -> tuple[list[str], list[dict[str, Any]]]:
    """顶层 instructions 只放 system prompt；历史中段 system item 原位改写为带标签 user 消息。

    Codex 代理拒收 role=system 的 input item（ADR 0026），但把压缩摘要 / pinned 重注 /
    预算提示 / 记忆预取这类中段注记折叠进 instructions 有两处代价：丢失位置（周期重注
    本在尾部，折叠后排到对话之前，模型反把对话里更早的旧状态当成最新）；且每次注入都
    改写 instructions 前缀，prompt cache 随之失效（R2）。与 Anthropic / Gemini 同一处置
    （ADR 0055 / 0072）：保持原位、改写为 ``<system-reminder>`` 包裹的 user 文本。
    """
    prompts = [prompt for prompt in request.system_prompt if prompt != ""]
    input_items: list[dict[str, Any]] = []
    for item in request.input_items:
        if isinstance(item, ApiMessageItem) and item.role == "system":
            input_items.append({
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": mid_history_system_text(item.content)}],
            })
            continue
        input_items.append(_input_item(item))
    return prompts, input_items


def _optional_fields(payload: dict[str, Any], request: ApiRequest) -> None:
    """加入 tools、structured output 和采样旋钮。"""
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in request.tools
        ]
        payload["parallel_tool_calls"] = request.parallel_tool_calls
    if request.response_format is not None:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": request.response_format.name,
                "schema": request.response_format.json_schema,
                "strict": request.response_format.strict,
            }
        }
    if request.reasoning_effort is not None:
        payload["reasoning"] = {"effort": request.reasoning_effort}
    if request.max_output_tokens is not None:
        payload["max_output_tokens"] = request.max_output_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature


def build_codex_payload(
    request: ApiRequest,
    *,
    default_model: str,
) -> dict[str, Any]:
    """构造独立 Codex request；不按模型名或域名猜 dialect。"""
    prompts, input_items = _partition_instructions(request)
    payload: dict[str, Any] = {
        "model": request.model or default_model,
        "input": input_items,
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
    }
    if prompts:
        payload["instructions"] = "\n\n".join(prompts)
    _optional_fields(payload, request)
    enforce_openai_wire_size(payload, request)
    return payload


__all__ = ["build_codex_payload"]
