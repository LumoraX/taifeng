"""strict LLM request intent V2 的安全投影与 digest 测试。"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from taifeng.llm.audit_redaction import (
    SensitiveRequestShapeError,
    project_attempt_request,
)
from taifeng.llm.types import (
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    ProviderStateEnvelope,
    TextPart,
)


def _ping_request() -> ApiRequest:
    """构造契约 canonical digest vector 的请求。"""
    return ApiRequest(
        model="gpt-5.6-luna",
        input_items=[ApiMessageItem(role="user", content="ping")],
    )


def _sensitive_request() -> tuple[ApiRequest, str, str]:
    """构造同时含图片正文和 provider ciphertext 的请求。"""
    image_bytes = b"image-body"
    image_body = base64.b64encode(image_bytes).decode("ascii")
    ciphertext = "ciphertext-sentinel"
    request = ApiRequest(
        model="gpt-5.6-luna",
        input_items=[
            ApiMessageItem(
                role="user",
                content=[
                    TextPart(text="inspect"),
                    ImagePart(
                        media_type="image/png",
                        base64_data=image_body,
                        size=len(image_bytes),
                        sha256=hashlib.sha256(image_bytes).hexdigest(),
                        detail="high",
                    ),
                ],
            ),
            ApiProviderStateItem(
                sample_id="sample-1",
                output_index=0,
                state=ProviderStateEnvelope(
                    provider="codex",
                    protocol="responses",
                    item_type="reasoning",
                    payload={
                        "id": "rs_1",
                        "type": "reasoning",
                        "encrypted_content": ciphertext,
                        "summary": [],
                    },
                ),
            ),
        ],
    )
    return request, image_body, ciphertext


def test_attempt_digest_matches_contract_vector() -> None:
    """RFC 8785 preimage 必须跨实现得到契约固定 digest。"""
    projection = project_attempt_request(
        "codex",
        "gpt-5.6-luna",
        _ping_request(),
    )

    assert projection.canonical_attempt_sha256 == (
        "ca2f8ff5fcb8a45b8725d71e1943da15346e5ae2006adc6232e4b1cbd8fc13eb"
    )
    assert projection.redactions == ()


def test_sensitive_projection_has_sorted_unique_json_pointer_manifest() -> None:
    """图片与 state 正文必须替换为 descriptor，并留下稳定 manifest。"""
    request, image_body, ciphertext = _sensitive_request()

    projection = project_attempt_request("codex", "gpt-5.6-luna", request)

    encoded = json.dumps(projection.api_request_safe, sort_keys=True)
    assert image_body not in encoded
    assert ciphertext not in encoded
    assert '"encrypted_content":' not in encoded
    assert "sha256" in encoded
    paths = [entry.path for entry in projection.redactions]
    assert paths == sorted(set(paths), key=lambda value: value.encode("utf-8"))
    assert {entry.kind for entry in projection.redactions} == {
        "image_base64",
        "provider_encrypted_content",
    }


def test_sensitive_key_outside_approved_shape_fails_closed() -> None:
    """任意 metadata 不得用敏感键绕过 provider-state redactor。"""
    request = _ping_request().model_copy(
        update={"metadata": {"encrypted_content": "must-not-pass"}}
    )

    with pytest.raises(SensitiveRequestShapeError):
        project_attempt_request("codex", "gpt-5.6-luna", request)


def test_image_nested_in_function_call_output_is_redacted() -> None:
    """嵌在 function_call_output 里的图片同样必须脱敏。

    脱敏判据是「形状」（``type == "image"`` 且带 base64_data）而非「位置」，
    工具附件因此复用同一条通路、零改动即被覆盖。本用例锁死该性质，防止未来
    有人把判据改成按 item 位置枚举而漏掉工具侧。
    """
    from taifeng.llm.types import ApiFunctionCallOutputItem

    image_bytes = b"tool-frame-body"
    image_body = base64.b64encode(image_bytes).decode("ascii")
    request = ApiRequest(
        model="gpt-5.6-luna",
        input_items=[
            ApiFunctionCallOutputItem(
                call_id="c1",
                origin_sample_id="sample-1",
                output=[
                    TextPart(text="frame 1023"),
                    ImagePart(
                        media_type="image/png",
                        base64_data=image_body,
                        size=len(image_bytes),
                        sha256=hashlib.sha256(image_bytes).hexdigest(),
                        detail="high",
                    ),
                ],
            )
        ],
    )

    projection = project_attempt_request("codex", "gpt-5.6-luna", request)

    encoded = json.dumps(projection.api_request_safe, sort_keys=True)
    assert image_body not in encoded
    assert "frame 1023" in encoded  # 文本仍保留，只脱敏图片正文
    assert {entry.kind for entry in projection.redactions} == {"image_base64"}
