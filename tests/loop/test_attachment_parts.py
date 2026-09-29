"""附件 payload → parts 投影与 user 消息入队前准入（loop/attachment_parts）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import UnsupportedModalityError
from taifeng.llm.file_input import DISABLED_FILE_POLICY, FileInputPolicy
from taifeng.llm.image_input import DISABLED_IMAGE_POLICY, ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.types import FilePart, ImagePart
from taifeng.loop.attachment_parts import (
    admit_user_attachments,
    extract_attachments,
    to_file_parts,
    user_attachment_parts,
)
from tests.pdf_fixtures import pdf_attachment

_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image", "file"}), provider="x", protocol="y"
)
_FILES = FileInputPolicy(enabled=True, max_files=3, max_total_bytes=10 * 1024 * 1024)
_IMAGES = ImageInputPolicy(enabled=True, max_images=2, allowed_media_types=frozenset({"image/png"}))
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
)


def test_extract_attachments_filters_kind_and_ignores_bad_shapes() -> None:
    file = pdf_attachment()
    payload = {"attachments": [file, {"kind": "image"}, "junk", 3]}

    assert extract_attachments(payload, "file") == [file]
    assert extract_attachments(payload, "image") == [{"kind": "image"}]
    assert extract_attachments({"attachments": "nope"}, "file") == []
    assert extract_attachments({}, "file") == []


def test_no_attachments_never_touches_disabled_policies() -> None:
    """无该类附件时不调 admission：关闭的策略面对空列表不得报错。"""
    assert user_attachment_parts(
        {"attachments": []},
        image_input_policy=DISABLED_IMAGE_POLICY,
        file_input_policy=DISABLED_FILE_POLICY,
        model_capabilities=_CAPS,
    ) == []
    parts = user_attachment_parts(
        {"attachments": [pdf_attachment()]},
        image_input_policy=DISABLED_IMAGE_POLICY,
        file_input_policy=_FILES,
        model_capabilities=_CAPS,
    )
    assert [type(part) for part in parts] == [FilePart]


def test_parts_interleave_in_attachment_order() -> None:
    image = ImageAttachmentV1.from_bytes(_PNG, media_type="image/png").model_dump()
    payload = {
        "attachments": [image, pdf_attachment("a"), image, pdf_attachment("b", filename="b.pdf")]
    }

    parts = user_attachment_parts(
        payload,
        image_input_policy=_IMAGES,
        file_input_policy=_FILES,
        model_capabilities=_CAPS,
    )

    assert [type(part) for part in parts] == [ImagePart, FilePart, ImagePart, FilePart]
    assert [part.filename for part in parts if isinstance(part, FilePart)] == ["note.pdf", "b.pdf"]


def test_to_file_parts_projects_canonical_fields() -> None:
    attachment = pdf_attachment()

    (part,) = to_file_parts([attachment], _FILES)

    assert (part.base64_data, part.size, part.sha256, part.filename) == (
        attachment["content"], attachment["size"], attachment["sha256"], "note.pdf",
    )


def test_admit_reads_declared_capabilities_of_client() -> None:
    """旧 custom client（无 capabilities 属性）按 text-only 处理，文件被拒。"""
    with pytest.raises(UnsupportedModalityError, match="file input"):
        admit_user_attachments(
            [pdf_attachment()],
            image_input_policy=DISABLED_IMAGE_POLICY,
            file_input_policy=_FILES,
            model_client=SimpleNamespace(),
        )
    admit_user_attachments(
        [pdf_attachment()],
        image_input_policy=DISABLED_IMAGE_POLICY,
        file_input_policy=_FILES,
        model_client=SimpleNamespace(capabilities=_CAPS),
    )
