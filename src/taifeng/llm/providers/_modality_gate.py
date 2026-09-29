"""provider 序列化前的输入模态门控（图片 / 文件 / provider state）。

所有只消费兼容 messages view 的 native adapter（OpenAICompat / Anthropic / Gemini /
LiteLLM）在组 payload 的第一步调用这里：client 未声明的模态一律显式拒绝，不得静默
丢弃、降级为文件名文本，或让 Pydantic part 泄漏进 JSON encoder。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.llm.errors import InvalidHistoryError, UnsupportedModalityError
from taifeng.llm.types import ApiProviderStateItem, FilePart, ImagePart

if TYPE_CHECKING:
    from taifeng.llm.types import ApiRequest


def assert_request_modalities(
    request: ApiRequest, input_modalities: frozenset[str]
) -> None:
    """在序列化前拒绝 client 未声明的输入模态与不透明 provider state。

    ``ImagePart`` 要求声明 ``"image"``，``FilePart`` 要求声明 ``"file"``
    （llm-image-input / llm-file-input 契约的同一道门）。

    Args:
        request: 待序列化的 provider-neutral 请求。
        input_modalities: 该 client 在 ``ModelCapabilities`` 里声明的输入模态。

    Raises:
        InvalidHistoryError: 含 provider state（本族协议不可重放）。
        UnsupportedModalityError: 含未声明模态的 part。
    """
    if any(isinstance(item, ApiProviderStateItem) for item in request.input_items):
        raise InvalidHistoryError("provider state is not supported by this protocol")
    for message in request.messages:
        if isinstance(message.content, str):
            continue
        for part in message.content:
            # 逐 part 核对声明：未声明即拒，绝不静默跳过
            if isinstance(part, ImagePart) and "image" not in input_modalities:
                raise UnsupportedModalityError("image input is not supported by this client")
            if isinstance(part, FilePart) and "file" not in input_modalities:
                raise UnsupportedModalityError("file input is not supported by this client")


def assert_text_only_request(request: ApiRequest) -> None:
    """text-only client 的门控：图片、文件与 provider state 一律在序列化前拒绝。"""
    assert_request_modalities(request, frozenset({"text"}))


__all__ = ["assert_request_modalities", "assert_text_only_request"]
