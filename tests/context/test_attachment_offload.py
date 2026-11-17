"""restorable-attachments —— 被压缩淘汰的附件落盘、留路径、可读回（ADR 0147）。

覆盖四段链路：落盘 helper → 驱逐策略带路径 → handoff 原样保留指针 → file_read 读回图片。
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

import pytest

from taifeng.context.attachment_offload import (
    POINTER_BLOCK_OPEN,
    collect_attachment_pointers,
    describe_attachment,
    format_pointer_block,
    remove_thread_offload_dir,
    save_attachment,
)
from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import HandoffCompactionStrategy, MultimodalEvictionStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    compacted,
    function_call,
    function_call_output,
    user_message,
)
from taifeng.llm.image_input import ImageAttachmentV1
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.builtins.file_io import make_file_read_tool
from taifeng.tool.spec import ToolContext
from taifeng.tool.workspace import LocalWorkspaceFS

if TYPE_CHECKING:
    from pathlib import Path

TID = "t-restore"
# 最小合法 PNG 文件头 + 填充：file_read 按文件头识别图片
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pixel-data-" * 40
PDF_BYTES = b"%PDF-1.7\n" + b"page " * 60


def _image(data: bytes = PNG_BYTES) -> dict[str, object]:
    return ImageAttachmentV1.from_bytes(data, media_type="image/png", detail="high").model_dump()


def _pdf(name: str = "report.pdf") -> dict[str, object]:
    return {
        "kind": "file", "media_type": "application/pdf", "size": len(PDF_BYTES),
        "sha256": hashlib.sha256(PDF_BYTES).hexdigest(), "encoding": "base64",
        "content": base64.b64encode(PDF_BYTES).decode("ascii"), "filename": name,
    }


def _user(text: str, *attachments: dict[str, object]) -> ResponseItem:
    return user_message(text, thread_id=TID, attachments=list(attachments))


def _reply(text: str = "好的") -> ResponseItem:
    return assistant_message(text, thread_id=TID, model="m")


def _ctx(
    history: list[ResponseItem], *, window: int = 10_000, tail: int = 4,
) -> CompressionContext:
    budget = ContextBudget(context_window=window, preserve_tail_messages=tail)
    return CompressionContext(
        history=history, token_estimate=int(window * 0.9), budget=budget,
        cache_anchor_index=-1, phase="pre_turn",
        available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
    )


def _tool_ctx() -> ToolContext:
    return ToolContext(call_id="r1", cancel=CancellationToken(), thread_id=TID)


# ---- 落盘 helper ----


async def test_save_writes_original_bytes_under_thread_dir(tmp_path: Path) -> None:
    ws = LocalWorkspaceFS(tmp_path)
    attachment = _image()

    path = await save_attachment(ws, TID, attachment)

    digest = hashlib.sha256(PNG_BYTES).hexdigest()
    assert path == f"_offload/{TID}/att-{digest[:16]}.png"
    assert (tmp_path / path).read_bytes() == PNG_BYTES


async def test_save_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    """同一附件再次落盘得到同一路径，且不重写已有文件。"""
    ws = LocalWorkspaceFS(tmp_path)
    first = await save_attachment(ws, TID, _image())
    assert first is not None
    target = tmp_path / first
    target.write_bytes(b"sentinel")  # 若被重写，这里会变回 PNG_BYTES

    second = await save_attachment(ws, TID, _image())

    assert second == first
    assert target.read_bytes() == b"sentinel"


async def test_save_uses_extension_by_media_type(tmp_path: Path) -> None:
    ws = LocalWorkspaceFS(tmp_path)
    pdf_path = await save_attachment(ws, TID, _pdf())
    unknown = {**_pdf(), "media_type": "application/x-unknown"}
    assert pdf_path is not None and pdf_path.endswith(".pdf")
    unknown_path = await save_attachment(ws, TID, unknown)
    assert unknown_path is not None and unknown_path.endswith(".bin")


@pytest.mark.parametrize("thread_id", ["", "..", "a/b", "a\\b", "/abs", "x\x00y"])
async def test_save_rejects_unsafe_thread_id(tmp_path: Path, thread_id: str) -> None:
    """thread_id 是外部输入：路径穿越型取值不落盘。"""
    assert await save_attachment(LocalWorkspaceFS(tmp_path), thread_id, _image()) is None
    assert not list(tmp_path.rglob("att-*"))


@pytest.mark.parametrize("content", [None, "", "not base64 !!!"])
async def test_save_without_decodable_content_returns_none(
    tmp_path: Path, content: object,
) -> None:
    attachment = {**_image(), "content": content}
    assert await save_attachment(LocalWorkspaceFS(tmp_path), TID, attachment) is None


async def test_save_computes_digest_when_declared_one_is_invalid(tmp_path: Path) -> None:
    attachment = {**_image(), "sha256": "../../etc"}
    path = await save_attachment(LocalWorkspaceFS(tmp_path), TID, attachment)
    assert path == f"_offload/{TID}/att-{hashlib.sha256(PNG_BYTES).hexdigest()[:16]}.png"


async def test_remove_thread_dir_is_idempotent(tmp_path: Path) -> None:
    ws = LocalWorkspaceFS(tmp_path)
    await save_attachment(ws, TID, _image())
    await remove_thread_offload_dir(ws, TID)
    await remove_thread_offload_dir(ws, TID)  # 已删除 → noop
    assert not (tmp_path / "_offload" / TID).exists()


def test_describe_sanitizes_filename_and_appends_path() -> None:
    attachment = {**_pdf("a;b[c].pdf")}
    text = describe_attachment(attachment, path="_offload/t/att-1.pdf")
    assert "a,b(c).pdf" in text
    assert text.endswith("path=_offload/t/att-1.pdf")
    assert "path=" not in describe_attachment(attachment)


# ---- 驱逐策略 ----


async def test_eviction_saves_original_and_stub_carries_path(tmp_path: Path) -> None:
    history = [_user("看这张图", _image()), _reply(), _user("继续"), _reply()]
    strategy = MultimodalEvictionStrategy(
        keep_recent=0, protect_tail_messages=0, file_root=tmp_path,
    )

    result = await strategy.compress(
        _ctx(history), InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )

    assert result.success
    text = result.new_history[0].payload["text"]
    (pointer,) = collect_attachment_pointers(result.new_history)
    path = pointer.rsplit("path=", 1)[1]
    assert f"path={path}" in text and "file_read" in text
    assert (tmp_path / path).read_bytes() == PNG_BYTES
    assert result.new_history[0].payload["attachments"] == []
    assert result.detail["offloaded_attachments"] == 1


async def test_eviction_without_workspace_keeps_previous_stub_shape() -> None:
    """未配置工作区：描述里没有路径、没有取回提示，detail 不多出键。"""
    history = [_user("看这张图", _image()), _reply()]
    strategy = MultimodalEvictionStrategy(keep_recent=0, protect_tail_messages=0)

    result = await strategy.compress(
        _ctx(history), InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )

    text = result.new_history[0].payload["text"]
    assert "path=" not in text and "file_read" not in text
    assert set(result.detail) == {"evicted_items", "evicted_attachments", "evicted_bytes"}
    assert collect_attachment_pointers(result.new_history) == []


async def test_eviction_still_evicts_when_save_fails(tmp_path: Path) -> None:
    """落盘不了（正文不可解码）仍驱逐，只是不带路径；计数如实为 0。"""
    broken = {**_image(), "content": "not base64 !!!"}
    history = [_user("看图", broken), _reply()]
    strategy = MultimodalEvictionStrategy(
        keep_recent=0, protect_tail_messages=0, file_root=tmp_path,
    )

    result = await strategy.compress(
        _ctx(history), InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )

    assert result.success
    assert "path=" not in result.new_history[0].payload["text"]
    assert result.detail["offloaded_attachments"] == 0


async def test_eviction_cleanup_thread_removes_saved_files(tmp_path: Path) -> None:
    strategy = MultimodalEvictionStrategy(
        keep_recent=0, protect_tail_messages=0, file_root=tmp_path,
    )
    await strategy.compress(
        _ctx([_user("图", _image()), _reply()]),
        InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )
    await strategy.cleanup_thread(TID)
    assert not (tmp_path / "_offload" / TID).exists()
    await MultimodalEvictionStrategy().cleanup_thread(TID)  # 无工作区 → noop


def test_eviction_rejects_both_file_root_and_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MultimodalEvictionStrategy(file_root=tmp_path, workspace=LocalWorkspaceFS(tmp_path))


# ---- 指针收集 ----


def test_collect_reads_evicted_lines_from_messages_and_tool_outputs() -> None:
    items = [
        _user("图\n[evicted: 2 attachments: image/png 1.0KB sha256=aa path=_offload/t/att-1.png; "
              "application/pdf 2.0KB r.pdf sha256=bb path=_offload/t/att-2.pdf]\n原件已落盘。"),
        function_call("c1", "shot", "{}", thread_id=TID),
        function_call_output(
            call_id="c1", thread_id=TID,
            output="截图\n[evicted: 1 attachment: image/png 3.0KB sha256=cc path=_offload/t/att-3.png]",
        ),
        _user("- image/png 9KB path=_offload/t/att-fake.png"),  # 用户自己写的列表项不算
        _user("[evicted: 1 attachment: image/png 1.0KB sha256=dd]"),  # 没有路径 → 不可取回
    ]
    assert [p.rsplit("path=", 1)[1] for p in collect_attachment_pointers(items)] == [
        "_offload/t/att-1.png", "_offload/t/att-2.pdf", "_offload/t/att-3.png",
    ]


def test_collect_carries_pointers_over_from_previous_summary_and_dedups() -> None:
    block = format_pointer_block(["image/png 1.0KB path=_offload/t/att-1.png"], limit=8)
    items = [
        compacted(f"前言\n\n<summary>\ns\n</summary>\n\n{block}", thread_id=TID,
                  replaced_range=(0, 4), cache_invalidated=True),
        _user("[evicted: 1 attachment: image/png 1.0KB sha256=aa path=_offload/t/att-1.png]"),
        _user("[evicted: 1 attachment: image/png 2.0KB sha256=bb path=_offload/t/att-2.png]"),
    ]
    assert [p.rsplit("path=", 1)[1] for p in collect_attachment_pointers(items)] == [
        "_offload/t/att-1.png", "_offload/t/att-2.png",
    ]


def test_format_block_keeps_most_recent_within_limit() -> None:
    pointers = [f"image/png path=_offload/t/att-{i}.png" for i in range(5)]
    block = format_pointer_block(pointers, limit=2)
    assert "att-3.png" in block and "att-4.png" in block and "att-2.png" not in block
    assert format_pointer_block(pointers, limit=0) == ""
    assert format_pointer_block([], limit=8) == ""


# ---- handoff 保留指针 ----


async def test_handoff_keeps_evicted_pointers_verbatim(tmp_path: Path) -> None:
    """驱逐留下的路径在 LLM 摘要之后仍在：模型没写，内核原样补上。"""
    evict = MultimodalEvictionStrategy(
        keep_recent=0, protect_tail_messages=0, file_root=tmp_path,
    )
    history = [_user("看这张图", _image()), _reply("看到了"), _user("继续"), _reply("做完了"),
               _user("再继续"), _reply("收尾")]
    evicted = (await evict.compress(
        _ctx(history), InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )).new_history
    (pointer,) = collect_attachment_pointers(evicted)
    # 摘要模型故意不提路径：指针必须由内核原样补上，而不是指望模型转述
    client = SimClient(turns=[SimTurn(text="## 进度\n- 看过图")])
    handoff = HandoffCompactionStrategy(model_client=client, model="m")

    result = await handoff.compress(
        _ctx(evicted, window=1000, tail=2), InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )

    assert result.success, result.reason
    (item,) = [it for it in result.new_history if it.kind == "compacted"]
    summary = item.payload["summary"]
    assert summary.index("</summary>") < summary.index(POINTER_BLOCK_OPEN)
    assert collect_attachment_pointers(result.new_history) == [pointer]


async def test_handoff_saves_inline_attachments_when_workspace_given(tmp_path: Path) -> None:
    """被压缩区间里仍内联的图片：配置了工作区就落盘并登记，否则不产生指针块。"""
    history = [_user("看这张图", _image()), _reply("看到了"), _user("继续"), _reply("做完了"),
               _user("再继续"), _reply("收尾")]

    def _client() -> SimClient:
        return SimClient(turns=[SimTurn(text="## 进度\n- 看过图")])

    plain = await HandoffCompactionStrategy(model_client=_client(), model="m").compress(
        _ctx(history, window=1000, tail=2), InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )
    assert plain.success, plain.reason
    assert collect_attachment_pointers(plain.new_history) == []

    saving = await HandoffCompactionStrategy(
        model_client=_client(), model="m", file_root=tmp_path,
    ).compress(
        _ctx(history, window=1000, tail=2), InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )
    assert saving.success, saving.reason
    (pointer,) = collect_attachment_pointers(saving.new_history)
    assert (tmp_path / pointer.rsplit("path=", 1)[1]).read_bytes() == PNG_BYTES


def test_handoff_rejects_negative_pointer_limit() -> None:
    with pytest.raises(ValueError):
        HandoffCompactionStrategy(model_client=SimClient(turns=[]), max_attachment_pointers=-1)


# ---- file_read 读回 ----


async def test_evicted_image_round_trips_through_file_read(tmp_path: Path) -> None:
    """全链路：驱逐 → 据描述里的路径 file_read → 拿回与原件逐字节一致的图片附件。"""
    original = _image()
    strategy = MultimodalEvictionStrategy(
        keep_recent=0, protect_tail_messages=0, file_root=tmp_path,
    )
    result = await strategy.compress(
        _ctx([_user("看这张图", original), _reply()]),
        InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )
    (pointer,) = collect_attachment_pointers(result.new_history)
    path = pointer.rsplit("path=", 1)[1]

    reader = make_file_read_tool(root_dir=tmp_path, attach_images=True)
    read = await reader.handler({"path": path}, _tool_ctx())

    assert not read.is_error
    (attachment,) = read.attachments
    assert attachment.sha256 == original["sha256"]
    assert base64.b64decode(attachment.content) == PNG_BYTES
    assert "image/png" in read.output and "base64" not in read.output
    assert path in read.output and str(tmp_path) not in read.output  # 不泄露宿主绝对路径


async def test_file_read_image_without_opt_in_is_an_explicit_error(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PNG_BYTES)
    read = await make_file_read_tool(root_dir=tmp_path).handler({"path": "shot.png"}, _tool_ctx())
    assert read.is_error and read.data["reason"] == "image_not_attached"
    assert read.attachments == ()


async def test_file_read_image_over_limit_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PNG_BYTES)
    reader = make_file_read_tool(root_dir=tmp_path, attach_images=True, max_image_bytes=16)
    read = await reader.handler({"path": "shot.png"}, _tool_ctx())
    assert read.is_error and read.data["reason"] == "too_large"


@pytest.mark.parametrize(
    ("header", "media_type"),
    [
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"GIF89a", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
    ],
)
async def test_file_read_detects_image_by_content_not_extension(
    tmp_path: Path, header: bytes, media_type: str,
) -> None:
    (tmp_path / "data.txt").write_bytes(header + b"x" * 32)
    reader = make_file_read_tool(root_dir=tmp_path, attach_images=True)
    read = await reader.handler({"path": "data.txt"}, _tool_ctx())
    assert not read.is_error
    assert read.attachments[0].media_type == media_type


async def test_file_read_text_is_unchanged_with_attach_images(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    reader = make_file_read_tool(root_dir=tmp_path, attach_images=True)
    read = await reader.handler({"path": "notes.txt", "offset": 1, "limit": 1}, _tool_ctx())
    assert read.output == "第二行" and read.attachments == ()


def test_file_read_rejects_non_positive_image_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_file_read_tool(root_dir=tmp_path, max_image_bytes=0)
