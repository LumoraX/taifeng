"""共享 MCP 帧解析与响应形状校验。"""

from __future__ import annotations

import pytest

from taifeng.mcp.bridge import McpToolError


def test_decoder_accepts_utf8_object() -> None:
    """UTF-8 帧保留 JSON 对象。"""
    from taifeng.mcp.message_decode import decode_message

    assert decode_message(b'{"result": {"text":"ok"}}') == {"result": {"text": "ok"}}


@pytest.mark.parametrize("frame", [b'\xffsecret', 'secret', '["secret"]'])
def test_decoder_rejects_without_logging_payload(frame, caplog: pytest.LogCaptureFixture) -> None:
    """坏编码、坏 JSON 与非对象统一记录元数据。"""
    from taifeng.mcp.message_decode import decode_message

    assert decode_message(frame) is None
    assert "secret" not in caplog.text
    assert "length=" in caplog.text


@pytest.mark.parametrize("error", ["secret", {"code": [], "message": "bad"},
                                    {"code": 1, "message": {}}, {"code": True, "message": "bad"}])
def test_response_error_shape_is_validated(error) -> None:
    """error 必须是有整数 code 和文本 message 的对象。"""
    from taifeng.mcp.message_decode import unwrap_response

    with pytest.raises(McpToolError, match="invalid JSON-RPC"):
        unwrap_response({"error": error})


def test_remote_value_metadata_contains_no_content() -> None:
    """日志描述不返回远端字符串或对象内容。"""
    from taifeng.mcp.message_decode import value_metadata

    assert value_metadata("secret") == ("str", 6)
    assert value_metadata({"secret": True}) == ("dict", 1)
    assert value_metadata(None) == ("NoneType", 0)
