"""Codex ``codex-responses-v1`` 请求 wire 构造。"""

from __future__ import annotations

from typing import Any

from taifeng.llm.errors import InvalidHistoryError
from taifeng.llm.providers.openai._shared import enforce_openai_wire_size
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
            "output": item.output,
        }
    if isinstance(item, ApiProviderStateItem):
        return _reasoning_state(item)
    raise InvalidHistoryError(f"unsupported Codex input item: {type(item).__name__}")


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
    prompts = [prompt for prompt in request.system_prompt if prompt != ""]
    payload: dict[str, Any] = {
        "model": request.model or default_model,
        "input": [_input_item(item) for item in request.input_items],
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
