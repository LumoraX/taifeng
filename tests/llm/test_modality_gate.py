"""provider 序列化前的输入模态门控（llm/providers/_modality_gate）。"""

from __future__ import annotations

import pytest

from taifeng.llm.errors import InvalidHistoryError, UnsupportedModalityError
from taifeng.llm.file_input import FileAttachmentV1
from taifeng.llm.providers._modality_gate import (
    assert_request_modalities,
    assert_text_only_request,
)
from taifeng.llm.types import (
    ApiMessage,
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    ProviderStateEnvelope,
    TextPart,
)
from tests.pdf_fixtures import minimal_pdf

_FILE = FileAttachmentV1.from_bytes(minimal_pdf()).to_part()
_IMAGE = ImagePart(media_type="image/png", base64_data="iVBORw0KGgo=", size=8, sha256="0" * 64)


def _request(*parts: TextPart | ImagePart) -> ApiRequest:
    """单条 user 多模态消息请求（文件 part 经 list 传入）。"""
    return ApiRequest(model="m", messages=[ApiMessage(role="user", content=list(parts))])


def test_text_only_gate_rejects_image_and_file() -> None:
    with pytest.raises(UnsupportedModalityError, match="image input"):
        assert_text_only_request(_request(_IMAGE))
    file_request = ApiRequest(model="m", messages=[ApiMessage(role="user", content=[_FILE])])
    with pytest.raises(UnsupportedModalityError, match="file input"):
        assert_text_only_request(file_request)
    assert_text_only_request(_request(TextPart(text="ok")))


def test_gate_follows_declared_modalities() -> None:
    request = ApiRequest(
        model="m", messages=[ApiMessage(role="user", content=[TextPart(text="x"), _FILE])]
    )

    assert_request_modalities(request, frozenset({"text", "file"}))
    with pytest.raises(UnsupportedModalityError, match="image input"):
        assert_request_modalities(_request(_IMAGE), frozenset({"text", "file"}))


def test_gate_rejects_provider_state() -> None:
    request = ApiRequest(
        model="m",
        input_items=[
            ApiMessageItem(role="user", content="x"),
            ApiProviderStateItem(
                sample_id="s",
                output_index=0,
                state=ProviderStateEnvelope(
                    provider="p", protocol="responses", item_type="reasoning", payload={}
                ),
            ),
        ],
    )
    with pytest.raises(InvalidHistoryError):
        assert_request_modalities(request, frozenset({"text", "image", "file"}))
