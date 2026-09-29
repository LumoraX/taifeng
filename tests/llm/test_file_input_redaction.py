"""文件正文在 request capture 与 strict attempt 投影中的脱敏（llm-file-input 契约）。"""

from __future__ import annotations

import json

import pytest

from taifeng.conversation.journal.records import RedactionEntryV1
from taifeng.llm.audit_redaction import SensitiveRequestShapeError, project_attempt_request
from taifeng.llm.file_input import FileAttachmentV1
from taifeng.llm.image_input import redact_sensitive_request_data
from taifeng.llm.types import ApiMessageItem, ApiRequest, TextPart
from tests.pdf_fixtures import minimal_pdf

_ATTACHMENT = FileAttachmentV1.from_bytes(minimal_pdf("secret-body"), filename="note.pdf")


def _request() -> ApiRequest:
    """含一段文字 + 一个 PDF 的 user 请求。"""
    return ApiRequest(
        model="gpt-5.6-luna",
        input_items=[
            ApiMessageItem(
                role="user", content=[TextPart(text="read it"), _ATTACHMENT.to_part()]
            )
        ],
    )


def test_request_capture_keeps_descriptor_and_drops_body() -> None:
    captured = redact_sensitive_request_data(_request().model_dump(mode="json"))

    encoded = json.dumps(captured, ensure_ascii=False, sort_keys=True)
    assert _ATTACHMENT.content not in encoded
    assert "base64_data" not in encoded
    assert "data:application/pdf" not in encoded
    assert "read it" in encoded  # 文字 prompt 仍保留
    part = captured["input_items"][0]["content"][1]
    assert part == {
        "type": "file",
        "media_type": "application/pdf",
        "size": _ATTACHMENT.size,
        "sha256": _ATTACHMENT.sha256,
        "filename": "note.pdf",
        "content_redacted": True,
    }


def test_strict_projection_records_file_redaction_manifest() -> None:
    projection = project_attempt_request("codex", "gpt-5.6-luna", _request())

    encoded = json.dumps(projection.api_request_safe, sort_keys=True)
    assert _ATTACHMENT.content not in encoded
    assert _ATTACHMENT.sha256 in encoded
    # input_items 与派生的 messages 视图各有一份 part，两处正文都必须登记
    assert [(entry.path, entry.kind) for entry in projection.redactions] == [
        ("/input_items/0/content/1/base64_data", "file_base64"),
        ("/messages/0/content/1/base64_data", "file_base64"),
    ]
    # manifest 条目可原样落 strict Journal（RedactionEntryV1 接受 file_base64）
    entry = projection.redactions[0]
    assert RedactionEntryV1(path=entry.path, kind=entry.kind).kind == "file_base64"


def test_strict_projection_digest_binds_unredacted_file_body() -> None:
    """canonical digest 绑定脱敏前正文：正文不同 → digest 不同，安全投影相同形状。"""
    other = FileAttachmentV1.from_bytes(minimal_pdf("other-body"), filename="note.pdf")
    changed = ApiRequest(
        model="gpt-5.6-luna",
        input_items=[
            ApiMessageItem(role="user", content=[TextPart(text="read it"), other.to_part()])
        ],
    )

    first = project_attempt_request("codex", "gpt-5.6-luna", _request())
    second = project_attempt_request("codex", "gpt-5.6-luna", changed)

    assert first.canonical_attempt_sha256 != second.canonical_attempt_sha256


def test_file_marker_collision_fails_closed() -> None:
    request = _request().model_dump(mode="json")
    request["input_items"][0]["content"][1]["content_redacted"] = True
    with pytest.raises(SensitiveRequestShapeError, match="file redaction marker collision"):
        from taifeng.llm.audit_redaction import _redact_value

        _redact_value(request, (), [])
