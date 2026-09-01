"""function_call_output 图片内容的 wire 投影（Responses / Codex 共用）。"""

from __future__ import annotations

from taifeng.llm.providers.openai._shared import tool_output_content
from taifeng.llm.types import ImagePart, TextPart


def _image_part() -> ImagePart:
    return ImagePart(
        media_type="image/png",
        base64_data="aGVsbG8=",
        size=5,
        sha256="c" * 64,
        detail="high",
    )


def test_plain_text_output_stays_string() -> None:
    """纯文本保持裸字符串 —— 非图片工具的 wire 与既有逐位一致。"""
    assert tool_output_content("ok") == "ok"


def test_empty_string_output_stays_string() -> None:
    """空字符串也不该被改写成数组。"""
    assert tool_output_content("") == ""


def test_parts_map_to_responses_content_items() -> None:
    """带图投影为 Responses content items：input_text + input_image。"""
    mapped = tool_output_content([TextPart(text="frame"), _image_part()])

    assert mapped == [
        {"type": "input_text", "text": "frame"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,aGVsbG8=",
            "detail": "high",
        },
    ]


def test_image_only_output_has_no_text_item() -> None:
    """只有图片时不产出空文本项 —— 空项白占 API 数组槽位。"""
    mapped = tool_output_content([_image_part()])

    assert [item["type"] for item in mapped] == ["input_image"]


def test_data_url_is_built_at_wire_layer_only() -> None:
    """Data URL 只在 wire 层临时构造，canonical base64 不含前缀。"""
    part = _image_part()
    mapped = tool_output_content([part])

    assert not part.base64_data.startswith("data:")
    assert mapped[0]["image_url"].startswith("data:image/png;base64,")
