"""图片 / 文件附件共享的 canonical base64 解码。"""

from __future__ import annotations

import base64

import pytest

from taifeng.llm.attachment_codec import decode_canonical_base64, encoded_limit
from taifeng.llm.errors import AttachmentTooLargeError, InvalidFileError, InvalidImageError


def test_encoded_limit_matches_base64_expansion() -> None:
    assert encoded_limit(0) == 0
    assert encoded_limit(1) == 4
    assert encoded_limit(3) == 4
    assert encoded_limit(4) == 8


def test_decode_round_trips_canonical_content() -> None:
    raw = b"abcdef"
    encoded = base64.b64encode(raw).decode("ascii")

    assert decode_canonical_base64(
        encoded, max_bytes=16, label="file", invalid_error=InvalidFileError
    ) == raw


@pytest.mark.parametrize(
    ("label", "error"), [("image", InvalidImageError), ("file", InvalidFileError)]
)
def test_decode_raises_labelled_error_type(label: str, error: type[Exception]) -> None:
    with pytest.raises(error, match=f"{label} content is not strict base64"):
        decode_canonical_base64("a*b=", max_bytes=16, label=label, invalid_error=error)  # type: ignore[arg-type]
    # 非 canonical：尾部 bit 非零（"QR==" 与 "QQ==" 解码同为 b"A"）
    with pytest.raises(error, match=f"{label} content is not canonical base64"):
        decode_canonical_base64("QR==", max_bytes=16, label=label, invalid_error=error)  # type: ignore[arg-type]


def test_decode_gates_encoded_and_decoded_size() -> None:
    with pytest.raises(AttachmentTooLargeError, match="file encoded"):
        decode_canonical_base64(
            base64.b64encode(b"x" * 10).decode("ascii"),
            max_bytes=3,
            label="file",
            invalid_error=InvalidFileError,
        )
    # 编码长度过闸（4 字符上限对应 3 字节）但 decoded 超 2 字节
    with pytest.raises(AttachmentTooLargeError, match="file decoded"):
        decode_canonical_base64(
            base64.b64encode(b"xyz").decode("ascii"),
            max_bytes=2,
            label="file",
            invalid_error=InvalidFileError,
        )
