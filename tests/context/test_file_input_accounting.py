"""文件附件的 token / 字节计量与压缩视图占位（llm-file-input 契约）。"""

from __future__ import annotations

from taifeng.context.budget import (
    estimate_history_tokens,
    estimate_item_bytes,
    estimate_item_tokens,
    estimate_text_tokens,
)
from taifeng.context.compaction_view import CompactionView
from taifeng.conversation.models import user_message
from taifeng.llm.file_input import DISABLED_FILE_POLICY, FileInputPolicy
from tests.pdf_fixtures import pdf_attachment


def test_enabled_policy_estimates_pages_times_ceiling() -> None:
    policy = FileInputPolicy(enabled=True, page_token_ceiling=700)
    item = user_message("hi", thread_id="t", attachments=[pdf_attachment(pages=4)])

    assert estimate_item_tokens(item, file_input_policy=policy) == (
        estimate_text_tokens("hi") + 4 * 700
    )


def test_disabled_or_missing_policy_uses_non_zero_ceiling_without_decoding() -> None:
    item = user_message("hi", thread_id="t", attachments=[pdf_attachment(), pdf_attachment()])
    expected = estimate_text_tokens("hi") + 2 * DISABLED_FILE_POLICY.unknown_file_token_ceiling

    assert estimate_item_tokens(item) == expected
    assert estimate_item_tokens(item, file_input_policy=DISABLED_FILE_POLICY) == expected
    assert estimate_history_tokens([item]) == expected


def test_text_only_items_are_unaffected() -> None:
    item = user_message("plain text", thread_id="t")

    assert estimate_item_tokens(item, file_input_policy=FileInputPolicy(enabled=True)) == (
        estimate_text_tokens("plain text")
    )


def test_item_bytes_include_file_body_for_request_guard() -> None:
    attachment = pdf_attachment()
    item = user_message("hi", thread_id="t", attachments=[attachment])

    assert estimate_item_bytes(item) > len(str(attachment["content"]))


def test_compaction_view_keeps_only_file_descriptor() -> None:
    named = pdf_attachment("body-a")
    unnamed = pdf_attachment("body-b", filename=None)
    item = user_message("see files", thread_id="t", attachments=[named, unnamed])

    rendered = CompactionView.from_items([item]).format_for_summary()

    assert "see files" in rendered
    assert f"[附件文件 note.pdf（application/pdf，{named['size']} 字节）]" in rendered
    assert f"[附件文件 （未命名）（application/pdf，{unnamed['size']} 字节）]" in rendered
    assert str(named["content"]) not in rendered
    assert str(unnamed["content"]) not in rendered


def test_compaction_view_file_only_message_has_no_blank_text_line() -> None:
    item = user_message("", thread_id="t", attachments=[pdf_attachment()])

    view = CompactionView.from_items([item])

    assert view.items[0].text.startswith("[附件文件 note.pdf")
