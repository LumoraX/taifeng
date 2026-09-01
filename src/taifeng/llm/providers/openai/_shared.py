"""OpenAI 官方协议共享的鉴权与输入边界辅助函数。"""

from __future__ import annotations

import json
from typing import Any

from taifeng.llm.errors import InvalidHistoryError, InvalidRequestError, RequestTooLargeError
from taifeng.llm.types import ApiProviderStateItem, ApiRequest, ImagePart, PartContent, TextPart

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_REQUEST_BYTES_METADATA_KEY = "taifeng.max_request_bytes"


def enforce_openai_wire_size(payload: dict[str, Any], request: ApiRequest) -> None:
    """按 httpx 紧凑 JSON 规则检查最终 OpenAI wire 的精确 UTF-8 字节数。"""
    max_bytes = request.metadata.get(MAX_REQUEST_BYTES_METADATA_KEY)
    if max_bytes is None:
        return
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidRequestError("max request bytes metadata must be a positive integer")
    wire = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(wire) > max_bytes:
        raise RequestTooLargeError(
            f"final OpenAI request body {len(wire)}B exceeds limit {max_bytes}B",
            estimated_bytes=len(wire),
            max_bytes=max_bytes,
        )


def build_openai_headers(
    api_key: str, extra_headers: dict[str, str] | None = None
) -> dict[str, str]:
    """构造官方 OpenAI JSON 请求头，并允许调用方追加组织级 header。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def reject_provider_state(items: list[Any], *, protocol: str) -> None:
    """Chat 等无状态协议必须在网络前拒绝不透明 Responses 状态。"""
    if any(isinstance(item, ApiProviderStateItem) for item in items):
        raise InvalidHistoryError(f"provider state cannot be replayed with OpenAI {protocol}")


def tool_output_content(content: PartContent) -> str | list[dict[str, Any]]:
    """把 ``function_call_output`` 的内容投影为 Responses content items。

    纯文本保持**裸字符串**（wire 与既有逐位一致，非图片工具零影响）；带图时投影
    为 ``input_text`` / ``input_image`` 数组 —— Responses 协议原生接受这两种形态
    （参照 codex ``FunctionCallOutputBody`` 的 untagged Text | ContentItems）。

    空文本项直接丢弃：它不承载信息，却白占 API 数组槽位（对齐 codex #40737）。
    Data URL 只在此处临时构造，canonical base64 不带前缀地留在 conversation 层。
    """
    if isinstance(content, str):
        return content
    mapped: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            if not part.text:
                continue
            mapped.append({"type": "input_text", "text": part.text})
        elif isinstance(part, ImagePart):
            mapped.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{part.media_type};base64,{part.base64_data}",
                    "detail": part.detail,
                }
            )
    return mapped


def chat_content(content: PartContent) -> str | list[dict[str, Any]]:
    """把 provider-neutral parts 映射为 OpenAI Chat content parts。"""
    if isinstance(content, str):
        return content
    mapped: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            mapped.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            mapped.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.media_type};base64,{part.base64_data}",
                        "detail": part.detail,
                    },
                }
            )
    return mapped
