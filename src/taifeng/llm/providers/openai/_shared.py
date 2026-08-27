"""OpenAI 官方协议共享的鉴权与输入边界辅助函数。"""

from __future__ import annotations

from typing import Any

from taifeng.llm.errors import InvalidHistoryError
from taifeng.llm.types import ApiProviderStateItem, ImagePart, PartContent, TextPart

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


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
