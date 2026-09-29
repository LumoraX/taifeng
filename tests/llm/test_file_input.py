"""文件输入 canonical 形态、admission 与 token 估算（llm-file-input 契约）。"""

from __future__ import annotations

import base64
import hashlib

import pytest
from pydantic import ValidationError

from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import (
    AttachmentTooLargeError,
    FileCountExceededError,
    InvalidFileError,
    UnsupportedModalityError,
)
from taifeng.llm.file_input import (
    DISABLED_FILE_POLICY,
    FileAttachmentV1,
    FileInputPolicy,
    InspectedFile,
    admit_file_attachments,
    estimate_file_tokens,
    inspect_pdf,
)
from taifeng.llm.types import ApiMessage, FilePart, TextPart
from tests.pdf_fixtures import minimal_pdf

_ENABLED = FileInputPolicy(enabled=True, max_files=2)


def _attachment(data: bytes, **overrides: object) -> FileAttachmentV1:
    """按原始字节构造 attachment，可覆盖任一字段制造不一致。"""
    fields: dict[str, object] = {
        "media_type": "application/pdf",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": base64.b64encode(data).decode("ascii"),
        "filename": "note.pdf",
    }
    fields.update(overrides)
    return FileAttachmentV1.model_validate(fields)


def test_from_bytes_produces_self_consistent_attachment() -> None:
    data = minimal_pdf("abc")
    attachment = FileAttachmentV1.from_bytes(data, filename="a.pdf")

    assert attachment.kind == "file"
    assert attachment.size == len(data)
    assert attachment.sha256 == hashlib.sha256(data).hexdigest()
    assert base64.b64decode(attachment.content) == data
    assert admit_file_attachments([attachment], _ENABLED)[0].page_count == 1


def test_attachment_rejects_data_url_and_foreign_media_type() -> None:
    with pytest.raises(ValidationError):
        _attachment(minimal_pdf(), content="data:application/pdf;base64,AAAA")
    with pytest.raises(ValidationError):
        _attachment(minimal_pdf(), media_type="text/plain")
    with pytest.raises(ValidationError):
        _attachment(minimal_pdf(), extra_field=1)


@pytest.mark.parametrize("name", ["../a.pdf", "dir\\a.pdf", "a\nb.pdf", "", "x" * 256])
def test_attachment_rejects_unsafe_filename(name: str) -> None:
    with pytest.raises(ValidationError):
        _attachment(minimal_pdf(), filename=name)


def test_attachment_filename_is_optional() -> None:
    attachment = _attachment(minimal_pdf(), filename=None)

    assert attachment.filename is None
    assert attachment.to_part().wire_filename() == f"attachment-{attachment.sha256[:12]}.pdf"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_files": 0},
        {"max_item_bytes": 0},
        {"max_item_bytes": 10, "max_total_bytes": 5},
        {"page_token_ceiling": 0},
        {"unknown_file_token_ceiling": 0},
        {"allowed_media_types": frozenset({"image/png"})},
    ],
)
def test_policy_rejects_unenforceable_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FileInputPolicy(**kwargs)  # type: ignore[arg-type]


def test_disabled_policy_rejects_any_file() -> None:
    assert DISABLED_FILE_POLICY.enabled is False
    with pytest.raises(UnsupportedModalityError, match="disabled"):
        admit_file_attachments([_attachment(minimal_pdf())], DISABLED_FILE_POLICY)


def test_admission_enforces_count_and_mime_allowlist() -> None:
    files = [_attachment(minimal_pdf()) for _ in range(3)]
    with pytest.raises(FileCountExceededError):
        admit_file_attachments(files, _ENABLED)
    with pytest.raises(UnsupportedModalityError, match="not allowed"):
        admit_file_attachments(
            files[:1], FileInputPolicy(enabled=True, allowed_media_types=frozenset())
        )


def test_admission_rejects_body_that_disagrees_with_declaration() -> None:
    data = minimal_pdf()
    with pytest.raises(InvalidFileError, match="size mismatch"):
        admit_file_attachments([_attachment(data, size=len(data) + 1)], _ENABLED)
    with pytest.raises(InvalidFileError, match="SHA-256"):
        admit_file_attachments([_attachment(data, sha256="0" * 64)], _ENABLED)


def test_admission_rejects_non_canonical_base64() -> None:
    data = minimal_pdf()
    encoded = base64.b64encode(data).decode("ascii")
    with pytest.raises(InvalidFileError, match="strict base64"):
        admit_file_attachments([_attachment(data, content=encoded + "!")], _ENABLED)
    # 去掉 padding 仍可解码但不是 canonical 形态
    unpadded = encoded.rstrip("=")
    if unpadded != encoded:
        with pytest.raises(InvalidFileError):
            admit_file_attachments([_attachment(data, content=unpadded)], _ENABLED)


def test_admission_enforces_item_and_total_byte_limits() -> None:
    data = minimal_pdf()
    with pytest.raises(AttachmentTooLargeError, match="file encoded"):
        admit_file_attachments(
            [_attachment(data)],
            FileInputPolicy(enabled=True, max_item_bytes=16, max_total_bytes=16),
        )
    policy = FileInputPolicy(
        enabled=True, max_files=2, max_item_bytes=len(data), max_total_bytes=len(data) + 1
    )
    with pytest.raises(AttachmentTooLargeError, match="total"):
        admit_file_attachments([_attachment(data), _attachment(data)], policy)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"hello world, not a pdf", "header"),
        (b"  %PDF-1.4\n%%EOF\n", "header"),
        (b"%PDF-1.4\n1 0 obj << >> endobj\n", "end-of-file"),
    ],
)
def test_inspect_pdf_rejects_non_pdf_and_truncated(body: bytes, message: str) -> None:
    with pytest.raises(InvalidFileError, match=message):
        inspect_pdf(body)
    with pytest.raises(InvalidFileError):
        admit_file_attachments([_attachment(body)], _ENABLED)


def test_inspect_pdf_counts_page_objects_not_page_tree() -> None:
    assert inspect_pdf(minimal_pdf(pages=3)) == 3
    # 页对象全在压缩 object stream 里时数不到 → 页数未知
    assert inspect_pdf(b"%PDF-1.7\n1 0 obj << /Type /Pages /Count 9 >> endobj\n%%EOF") is None


def test_estimate_tokens_uses_pages_or_conservative_ceiling() -> None:
    policy = FileInputPolicy(enabled=True, page_token_ceiling=100, unknown_file_token_ceiling=7)
    attachment = _attachment(minimal_pdf(pages=3))

    assert estimate_file_tokens(InspectedFile(attachment, page_count=3), policy) == 300
    assert estimate_file_tokens(InspectedFile(attachment, page_count=None), policy) == 7


def test_file_part_rejects_data_url_and_builds_wire_values() -> None:
    part = _attachment(minimal_pdf()).to_part()

    assert part.type == "file"
    assert part.data_url().startswith("data:application/pdf;base64,")
    assert part.wire_filename() == "note.pdf"
    with pytest.raises(ValidationError):
        FilePart(
            media_type="application/pdf",
            base64_data="data:application/pdf;base64,AAAA",
            size=3,
            sha256="0" * 64,
        )


def test_api_message_with_file_part_round_trips_through_json() -> None:
    message = ApiMessage(
        role="user",
        content=[TextPart(text="read"), _attachment(minimal_pdf()).to_part()],
    )

    restored = ApiMessage.model_validate_json(message.model_dump_json())

    assert restored == message
    assert isinstance(restored.content, list)
    assert isinstance(restored.content[1], FilePart)


def test_file_modality_is_opt_in_capability() -> None:
    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text", "file"}), provider="x", protocol="y"
    )

    assert "file" in capabilities.input_modalities
