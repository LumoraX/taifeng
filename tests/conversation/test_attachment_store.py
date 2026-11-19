"""附件存储协议、本机实现与引用形态（attachment-store，ADR 0148）。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import anyio
import pytest
from pydantic import ValidationError

from taifeng.conversation.attachment_store import (
    AttachmentNotFoundError,
    AttachmentStore,
    LocalAttachmentStore,
    collect_attachment_refs,
)
from taifeng.conversation.models import (
    compacted,
    function_call_output,
    user_message,
)
from taifeng.llm.attachment_refs import FileAttachmentRefV1, ImageAttachmentRefV1
from tests.conversation.attachment_store_conformance import CONFORMANCE_CASES, DATA, DIGEST

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

T = "t-store"


@pytest.mark.parametrize("case", CONFORMANCE_CASES, ids=lambda case: case.__name__)
async def test_local_store_conformance(
    case: Callable[[Callable[[], AttachmentStore]], Awaitable[None]], tmp_path: Path,
) -> None:
    await case(lambda: LocalAttachmentStore(tmp_path))


def test_local_store_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalAttachmentStore(tmp_path), AttachmentStore)


async def test_local_store_layout_and_no_temp_leftovers(tmp_path: Path) -> None:
    """对象按摘要分目录；写完不留临时文件。"""
    store = LocalAttachmentStore(tmp_path)
    ref = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    assert ref == f"sha256-{DIGEST}"
    target = tmp_path / "v1" / DIGEST[:2] / DIGEST
    assert target.read_bytes() == DATA
    assert [p.name for p in target.parent.iterdir()] == [DIGEST]


async def test_local_store_does_not_rewrite_existing_object(tmp_path: Path) -> None:
    store = LocalAttachmentStore(tmp_path)
    ref = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    target = tmp_path / "v1" / DIGEST[:2] / DIGEST
    stamp = target.stat().st_mtime_ns
    await store.put(DATA, media_type="image/png", sha256=DIGEST)
    assert target.stat().st_mtime_ns == stamp
    assert await store.get(ref) == DATA


async def test_cancelled_put_leaves_no_partial_object(tmp_path: Path) -> None:
    """取消发生在写入期间：目标要么完整存在，要么不存在；没有残留临时文件。"""
    store = LocalAttachmentStore(tmp_path)
    big = b"x" * (4 * 1024 * 1024)
    digest = hashlib.sha256(big).hexdigest()
    with anyio.move_on_after(0):
        await store.put(big, media_type="application/octet-stream", sha256=digest)
    target = tmp_path / "v1" / digest[:2] / digest
    assert not target.exists() or target.read_bytes() == big
    leftovers = list((tmp_path / "v1").rglob("*.tmp")) if (tmp_path / "v1").exists() else []
    assert leftovers == []


@pytest.mark.parametrize("ref", ["", "../etc/passwd", "sha256-" + "g" * 64, "sha256-abc", "other"])
async def test_local_store_unknown_refs(tmp_path: Path, ref: str) -> None:
    """不是本实现给出的引用：get 视为不存在，exists 为假，delete 为 noop。"""
    store = LocalAttachmentStore(tmp_path)
    with pytest.raises(AttachmentNotFoundError):
        await store.get(ref)
    assert not await store.exists(ref)
    await store.delete(ref)


# ---- 引用形态 ----


def _image_ref(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ref": f"sha256-{DIGEST}", "media_type": "image/png", "size": len(DATA),
        "sha256": DIGEST, "width": 4, "height": 3,
    }
    return {**base, **overrides}


def test_image_ref_round_trip_and_defaults() -> None:
    ref = ImageAttachmentRefV1.model_validate(_image_ref())
    dumped = ref.model_dump()
    assert dumped["encoding"] == "store" and dumped["kind"] == "image"
    assert dumped["detail"] == "auto"
    assert "content" not in dumped


def test_image_ref_rejects_body_field() -> None:
    with pytest.raises(ValidationError):
        ImageAttachmentRefV1.model_validate(_image_ref(content="QUJD"))


@pytest.mark.parametrize("bad", ["", "a b", "a/b", "a\nb", "x" * 201, "a]b", "a;b"])
def test_ref_charset_is_enforced(bad: str) -> None:
    with pytest.raises(ValidationError):
        ImageAttachmentRefV1.model_validate(_image_ref(ref=bad))


@pytest.mark.parametrize(
    "overrides", [{"width": 0}, {"height": -1}, {"size": 0}, {"sha256": "XYZ"}],
)
def test_image_ref_rejects_bad_metadata(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ImageAttachmentRefV1.model_validate(_image_ref(**overrides))


def test_file_ref_allows_unknown_pages() -> None:
    ref = FileAttachmentRefV1(
        ref="r1", media_type="application/pdf", size=10, sha256=DIGEST, pages=None,
        filename="a.pdf",
    )
    assert ref.model_dump()["pages"] is None


# ---- 引用枚举 ----


def test_collect_refs_covers_items_descriptions_and_pointer_blocks() -> None:
    items = [
        user_message(
            "图", thread_id=T,
            attachments=[ImageAttachmentRefV1.model_validate(_image_ref(ref="r-on-item")).model_dump()],
        ),
        user_message(
            "[evicted: 1 attachment: image/png 1.0KB sha256=aa path=attachment:r-evicted]",
            thread_id=T,
        ),
        function_call_output(
            call_id="c1", output="see path=attachment:r-in-output", thread_id=T,
        ),
        compacted(
            "<offloaded_attachments>\n- image/png path=attachment:r-in-summary\n"
            "</offloaded_attachments>",
            thread_id=T, replaced_range=(0, 2), cache_invalidated=True,
        ),
        user_message("ordinary text mentioning attachment: nothing", thread_id=T),
    ]
    assert collect_attachment_refs(items) == {
        "r-on-item", "r-evicted", "r-in-output", "r-in-summary",
    }


def test_collect_refs_ignores_inline_attachments() -> None:
    inline = {"kind": "image", "encoding": "base64", "content": "QUJD", "sha256": DIGEST}
    assert collect_attachment_refs([user_message("x", thread_id=T, attachments=[inline])]) == set()
