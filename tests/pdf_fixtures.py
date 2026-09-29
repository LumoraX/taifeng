"""测试共用的最小 PDF 构造器（纯标准库，带正确 xref 偏移）。"""

from __future__ import annotations

from typing import Any

from taifeng.llm.file_input import FileAttachmentV1


def minimal_pdf(text: str = "hello", *, pages: int = 1) -> bytes:
    """拼一个每页只含一行文字的最小合法 PDF。

    Args:
        text: 每页写入的 ASCII 文本（括号与反斜杠会被转义）。
        pages: 页数（≥1），用于验证按页 token 估算。

    Returns:
        可被真实 PDF 阅读器打开的字节串。
    """
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 24 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    # 对象编号：1 catalog / 2 pages / 3 font / 4 content / 5.. 各页
    page_ids = [5 + index for index in range(pages)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    objects.extend(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        for _ in page_ids
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_offset = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_offset,
    )
    return bytes(out)


def pdf_attachment(
    text: str = "hello", *, pages: int = 1, filename: str | None = "note.pdf"
) -> dict[str, Any]:
    """最小 PDF 的 canonical attachment payload（UserMessage.attachments 元素形态）。"""
    return FileAttachmentV1.from_bytes(
        minimal_pdf(text, pages=pages), filename=filename
    ).model_dump()
