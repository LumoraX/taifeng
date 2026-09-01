"""ToolResult 图片附件字段的契约测试。"""

from __future__ import annotations

from taifeng.llm.image_input import ImageAttachmentV1
from taifeng.tool.spec import ToolResult


def _png() -> bytes:
    """构造只供 header inspector 使用的最小 PNG（与 tests/llm 同形）。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _attachment() -> ImageAttachmentV1:
    return ImageAttachmentV1.from_bytes(_png(), media_type="image/png")


def test_default_attachments_is_empty_tuple() -> None:
    """不带附件的工具结果与既有行为逐位一致。"""
    result = ToolResult.ok("done")

    assert result.attachments == ()


def test_error_results_carry_no_attachments() -> None:
    """error() 不开放附件通道——失败结果不该往模型面前塞图。"""
    assert ToolResult.error("boom").attachments == ()


def test_ok_accepts_attachments_without_disturbing_data() -> None:
    """ok() 以关键字传附件，既有 **data 语义不受影响。"""
    attachment = _attachment()

    result = ToolResult.ok("2 frames", attachments=(attachment,), frames=2)

    assert result.attachments == (attachment,)
    assert result.data == {"frames": 2}
    assert result.output == "2 frames"
    assert result.is_error is False


def test_attachments_field_is_immutable_tuple() -> None:
    """frozen dataclass + tuple —— 附件序列不可在结算路径上被就地篡改。"""
    result = ToolResult.ok("one", attachments=(_attachment(),))

    assert isinstance(result.attachments, tuple)
