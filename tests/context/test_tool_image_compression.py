"""压缩策略对 fco 图片附件的感知（surgical_trim 去重/剪枝、offload 回避）。"""

from __future__ import annotations

import base64
import hashlib

from taifeng.context.strategies.surgical_trim import _output_digest, _rewrite
from taifeng.conversation.models import function_call_output

T = "t1"


def _payload(seed: int) -> dict:
    """构造 sha256 互不相同的 attachment payload。"""
    data = b"\x89PNG\r\n\x1a\n" + seed.to_bytes(4, "big")
    return {
        "kind": "image",
        "media_type": "image/png",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "detail": "auto",
    }


def test_digest_differs_when_only_images_differ() -> None:
    """文本相同、图片不同的两条 fco 不得被判为重复 —— 否则静默丢掉一张图。"""
    same_text = "frame captured" * 40
    a = function_call_output("c1", same_text, thread_id=T, attachments=[_payload(1)])
    b = function_call_output("c2", same_text, thread_id=T, attachments=[_payload(2)])

    assert _output_digest(a) != _output_digest(b)


def test_digest_matches_when_text_and_images_match() -> None:
    """文本与图片都相同才算重复 —— 去重仍然有效，只是变准了。"""
    same_text = "frame captured" * 40
    payload = _payload(1)
    a = function_call_output("c1", same_text, thread_id=T, attachments=[payload])
    b = function_call_output("c2", same_text, thread_id=T, attachments=[payload])

    assert _output_digest(a) == _output_digest(b)


def test_digest_ignores_attachment_order() -> None:
    """同一组图片的顺序差异不构成内容差异 —— 摘要按 sha256 排序。"""
    text = "two frames" * 40
    one, two = _payload(1), _payload(2)
    a = function_call_output("c1", text, thread_id=T, attachments=[one, two])
    b = function_call_output("c2", text, thread_id=T, attachments=[two, one])

    assert _output_digest(a) == _output_digest(b)


def test_digest_unchanged_for_plain_text_output() -> None:
    """无附件时摘要仍只由文本决定 —— 既有去重行为零变化。"""
    a = function_call_output("c1", "same", thread_id=T)
    b = function_call_output("c2", "same", thread_id=T)

    assert _output_digest(a) == _output_digest(b)


def test_rewrite_drops_attachments_with_the_text() -> None:
    """剪枝必须同时丢附件 —— 只剪文本会留下真正昂贵的那半边。"""
    item = function_call_output(
        "c1", "frame 1023", thread_id=T, attachments=[_payload(1), _payload(2)]
    )

    pruned = _rewrite(item, "[pruned]")

    assert "attachments" not in pruned.payload
    assert "2" in pruned.payload["output"]  # 占位符须留下图片计数痕迹


def test_rewrite_without_attachments_is_unchanged() -> None:
    """无附件时占位符形态与既有逐字一致。"""
    item = function_call_output("c1", "long output", thread_id=T)

    assert _rewrite(item, "[pruned]").payload["output"] == "[pruned]"


def test_rewrite_preserves_item_identity() -> None:
    """R5：就地重写不得改变 item 身份（id / thread_id / call_id）。"""
    item = function_call_output("c1", "x", thread_id=T, attachments=[_payload(1)])

    pruned = _rewrite(item, "[pruned]")

    assert pruned.id == item.id
    assert pruned.thread_id == item.thread_id
    assert pruned.payload["call_id"] == "c1"
