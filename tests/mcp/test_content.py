"""MCP tools/call 结果的内容投影（content.py + 桥 handler + 结算链）。

覆盖：text / image（附件 / 占位）/ audio / resource（text / blob）/ resource_link /
未知类型、structuredContent 进 data 与文本补全、非法形状显式报错、图片附件经 loop
admission 落进 fco（策略启用 / 未启用两侧）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.image_input import DISABLED_IMAGE_POLICY, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.mcp import register_mcp_tools_async
from taifeng.mcp.bridge import extract_text_content
from taifeng.mcp.content import McpContentError, convert_tool_result
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.spec import ToolContext, ToolResult
from tests.conftest import run_until_root_done

if TYPE_CHECKING:
    from pathlib import Path


def _png() -> bytes:
    """1×1 PNG 头（admission 只读 IHDR 尺寸）。"""
    return (
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


PNG_B64 = base64.b64encode(_png()).decode("ascii")


def _image_block(mime: str = "image/png", data: str = PNG_B64) -> dict[str, Any]:
    return {"type": "image", "data": data, "mimeType": mime}


class _FakeMcp:
    """进程内 McpClient 替身：tools/call 恒返回预置 result。"""

    server_info: dict[str, Any] = {"name": "fake"}

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    async def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": "shot", "description": "d", "inputSchema": {"type": "object"}}]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.result

    def add_tools_changed_listener(self, listener: Any) -> None:
        return None


async def _call_bridged(result: dict[str, Any], *, attach_images: bool = True) -> ToolResult:
    """经真实桥注册后调用一次 handler，返回 ToolResult。"""
    specs = await register_mcp_tools_async(
        _FakeMcp(result), ToolRegistry(), attach_images=attach_images)
    ctx = ToolContext(call_id="c", cancel=CancellationToken(), thread_id="t")
    return await specs[0].handler({}, ctx)


# ---------------------------------------------------------------- 纯投影


def test_text_blocks_join_in_order() -> None:
    """text 块按序换行拼接；isError 透传。"""
    out = convert_tool_result(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
         "isError": True}, attach_images=True)
    assert (out.text, out.is_error, out.attachments, out.structured_content) == (
        "a\nb", True, (), None)


def test_image_becomes_canonical_attachment_without_base64_in_text() -> None:
    """image → ImageAttachmentV1（size / sha256 由字节算出），文本侧不含 base64 正文。"""
    out = convert_tool_result(
        {"content": [{"type": "text", "text": "截图"}, _image_block()]}, attach_images=True)
    assert out.text == "截图"
    (attachment,) = out.attachments
    assert attachment.media_type == "image/png"
    assert attachment.size == len(_png())
    assert attachment.sha256 == hashlib.sha256(_png()).hexdigest()
    assert PNG_B64 not in out.text


def test_image_placeholder_when_attachments_disabled() -> None:
    """attach_images=False → 带 MIME 与字节数的显式占位，不产附件。"""
    out = convert_tool_result({"content": [_image_block()]}, attach_images=False)
    assert out.attachments == ()
    size = len(_png())
    assert out.text == f"[image: image/png, {size} bytes, not attached (attach_images=False)]"


@pytest.mark.parametrize(("block", "match"), [
    (_image_block(mime="image/svg+xml"), "cannot be attached"),
    (_image_block(data="不是base64!!"), "not valid base64"),
    (_image_block(data=""), "empty"),
    ({"type": "image", "data": PNG_B64}, "mimeType"),
])
def test_unattachable_image_raises(block: dict[str, Any], match: str) -> None:
    """MIME 不可作附件 / base64 非法 / 空图 / 缺字段 → 显式报错，不静默丢。"""
    with pytest.raises(McpContentError, match=match):
        convert_tool_result({"content": [block]}, attach_images=True)


def test_audio_placeholder_has_mime_and_size_but_no_base64() -> None:
    """audio → 显式占位（内核无音频模态），base64 不进文本。"""
    data = base64.b64encode(b"\x00" * 10).decode("ascii")
    out = convert_tool_result(
        {"content": [{"type": "audio", "data": data, "mimeType": "audio/wav"}]},
        attach_images=True)
    assert out.text == "[audio: audio/wav, 10 bytes, not shown: audio content is unsupported]"
    assert data not in out.text


def test_embedded_text_resource_is_inlined_with_uri() -> None:
    """text 资源：标注 uri + MIME 后内联原文。"""
    out = convert_tool_result({"content": [{"type": "resource", "resource": {
        "uri": "file:///a.rs", "mimeType": "text/x-rust", "text": "fn main() {}"}}]},
        attach_images=True)
    assert out.text == "[resource file:///a.rs (text/x-rust)]\nfn main() {}"


def test_embedded_blob_resource_is_placeholder() -> None:
    """blob 资源：带 MIME 与字节数的占位，base64 不进文本。"""
    blob = base64.b64encode(b"x" * 7).decode("ascii")
    out = convert_tool_result({"content": [{"type": "resource", "resource": {
        "uri": "file:///b.bin", "mimeType": "application/octet-stream", "blob": blob}}]},
        attach_images=True)
    assert out.text == (
        "[resource file:///b.bin: application/octet-stream, 7 bytes binary, not shown]")


def test_resource_without_text_or_blob_raises() -> None:
    """resource 既无 text 也无 blob → 形状非法。"""
    with pytest.raises(McpContentError, match="'text' or 'blob'"):
        convert_tool_result({"content": [{"type": "resource", "resource": {"uri": "x:/"}}]},
                            attach_images=True)


def test_resource_link_is_rendered_as_reference() -> None:
    """resource_link → 引用行（name / uri / MIME / 描述）。"""
    out = convert_tool_result({"content": [{
        "type": "resource_link", "uri": "file:///m.rs", "name": "m.rs",
        "mimeType": "text/x-rust", "description": "入口"}]}, attach_images=True)
    assert out.text == "[resource link m.rs <file:///m.rs> (text/x-rust): 入口]"


def test_unknown_content_type_is_explicit_placeholder() -> None:
    """未知类型 → 显式占位，不把整块（可能含二进制）塞进文本。"""
    out = convert_tool_result({"content": [{"type": "hologram", "data": "zzz"}]},
                              attach_images=True)
    assert out.text == "[unsupported MCP content type 'hologram']"


@pytest.mark.parametrize(("result", "match"), [
    ({"content": "oops"}, "must be an array"),
    ({"content": ["oops"]}, r"content\[0\] must be an object"),
    ({"content": [{"type": "text", "text": 3}]}, "'text' must be a string"),
    ({"content": [], "structuredContent": [1, 2]}, "structuredContent must be an object"),
])
def test_malformed_result_raises(result: dict[str, Any], match: str) -> None:
    """非法形状显式报错（旧实现静默跳过非对象块）。"""
    with pytest.raises(McpContentError, match=match):
        convert_tool_result(result, attach_images=True)


def test_structured_content_only_is_serialized_into_text() -> None:
    """content 为空 / 缺失 → 文本侧给出 structuredContent 的 JSON，供模型阅读。"""
    structured = {"temperature": 22.5, "conditions": "多云"}
    for result in ({"structuredContent": structured},
                   {"content": [], "structuredContent": structured}):
        out = convert_tool_result(result, attach_images=True)
        assert json.loads(out.text) == structured
        assert "多云" in out.text  # ensure_ascii=False：中文可读
        assert out.structured_content == structured


def test_structured_content_not_duplicated_when_server_already_serialized() -> None:
    """server 已按规范 SHOULD 把 JSON 放进 text 块（格式不同也算）→ 不重复补。"""
    structured = {"a": 1, "b": [1, 2]}
    out = convert_tool_result({
        "content": [{"type": "text", "text": '{"b": [1,2], "a": 1}'}],
        "structuredContent": structured}, attach_images=True)
    assert out.text == '{"b": [1,2], "a": 1}'


def test_structured_content_appended_after_human_summary() -> None:
    """text 块只有人类摘要、没有等价 JSON → 在其后补序列化 JSON。"""
    out = convert_tool_result({
        "content": [{"type": "text", "text": "北京 22.5 度"}],
        "structuredContent": {"t": 22.5}}, attach_images=True)
    assert out.text == '北京 22.5 度\n{"t": 22.5}'


def test_extract_text_content_is_text_only_projection() -> None:
    """公共符号 extract_text_content 保留：纯文本投影，图片为占位。"""
    text, is_error = extract_text_content(
        {"content": [{"type": "text", "text": "x"}, _image_block()], "isError": False})
    assert is_error is False
    assert text.startswith("x\n[image: image/png, ")


# ---------------------------------------------------------------- 桥 handler


async def test_bridge_puts_structured_content_into_data_and_keeps_mcp_tool() -> None:
    """structuredContent 进 ToolResult.data（保留 mcp_tool 键）。"""
    result = await _call_bridged({"content": [], "structuredContent": {"k": "v"}})
    assert result.data == {"mcp_tool": "shot", "structured_content": {"k": "v"}}
    assert json.loads(result.output) == {"k": "v"}
    assert result.is_error is False


async def test_bridge_attaches_images_by_default() -> None:
    """默认 attach_images=True：image 块进 ToolResult.attachments。"""
    result = await _call_bridged({"content": [{"type": "text", "text": "t"}, _image_block()]})
    assert result.output == "t"
    assert [a.media_type for a in result.attachments] == ["image/png"]


async def test_bridge_turns_invalid_content_into_error_result() -> None:
    """形状非法 → 该次调用判错（reason=mcp_invalid_content），原因对模型可见。"""
    result = await _call_bridged({"content": [_image_block(mime="image/bmp")]})
    assert result.is_error is True
    assert result.data["reason"] == "mcp_invalid_content"
    assert result.data["mcp_tool"] == "shot"
    assert "image/bmp" in result.output


async def test_bridge_placeholder_mode_produces_no_attachments() -> None:
    """attach_images=False → 占位文本、无附件（text-only 宿主用）。"""
    result = await _call_bridged({"content": [_image_block()]}, attach_images=False)
    assert result.attachments == ()
    assert result.output.startswith("[image: image/png, ")


# ---------------------------------------------------------------- 结算链（loop admission）


_SKILL = """---
name: viewer
description: 看截图
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [shot]
max_call_depth: 3
---
# viewer
需要看画面时调用 shot。
"""

_IMAGE_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}), provider="sim", protocol="sim",
    tool_output_modalities=frozenset({"text", "image"}))


async def _run_turn(tmp_path: Path, threads_dir: Path, policy: ImageInputPolicy) -> dict[str, Any]:
    """模型调 MCP 桥接工具 shot → 结算 → 返回 fco payload。"""
    skills = tmp_path / "skills"
    (skills / "viewer").mkdir(parents=True)
    (skills / "viewer" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    specs = await register_mcp_tools_async(
        _FakeMcp({"content": [{"type": "text", "text": "页面截图"}, _image_block()]}),
        ToolRegistry())
    client = SimClient(turns=[
        SimTurn(tool_calls=[{"id": "call_1", "name": "shot", "arguments": "{}"}]),
        SimTurn(text="看到了")], capabilities=_IMAGE_CAPS)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=policy, extra_tools=specs)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="viewer")
        events = await run_until_root_done(engine, taifeng.UserMessage(text="看一下"))
        assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
        return next(it.payload for it in engine.history_snapshot()
                    if it.kind == "function_call_output")
    finally:
        await pool.close()


async def test_mcp_image_lands_in_fco_when_policy_enabled(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """策略启用：MCP 图片经 admission 落进 fco 附件，文本为 content 的 text 部分。"""
    payload = await _run_turn(tmp_path, threads_dir, ImageInputPolicy(enabled=True))
    assert payload["output"] == "页面截图"
    assert payload["is_error"] is False
    assert [a["media_type"] for a in payload["attachments"]] == ["image/png"]


async def test_mcp_image_rejected_explicitly_when_policy_disabled(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """策略未启用：按 tool-image-attachment 契约该次调用判错，原因对模型可见。"""
    payload = await _run_turn(tmp_path, threads_dir, DISABLED_IMAGE_POLICY)
    assert payload["is_error"] is True
    assert "tool_attachment_rejected" in payload["output"]
    assert "attachments" not in payload
