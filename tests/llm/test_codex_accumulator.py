"""Codex done-item SSE 状态机测试。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from taifeng.llm.errors import ContentFilterError, InvalidResponseError
from taifeng.llm.providers.codex.accumulator import CodexResponsesAccumulator
from taifeng.llm.responses_types import NormalizedMessageItem


def _done_message(text: str = "库存 A-17") -> dict[str, object]:
    """构造 terminal message item。"""
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }


def _message_events(text: str = "库存 A-17") -> list[dict[str, object]]:
    """构造探针观察到的完整 message SSE 顺序。"""
    return [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.in_progress", "response": {"id": "resp_1"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text},
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": _done_message(text),
        },
    ]


def _completed(output: list[object] | None = None) -> dict[str, object]:
    """构造 strict completed completion gate。"""
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "status": "completed",
            "output": [] if output is None else output,
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "total_tokens": 28,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        },
    }


def _consume(*events: dict[str, object]):
    """吸收 events，并在 clean EOF finalize。"""
    accumulator = CodexResponsesAccumulator()
    preview = []
    for event in events:
        preview.extend(accumulator.accept(event))
    return accumulator.finalize(), preview


def test_done_items_are_fact_source_when_completed_output_is_empty() -> None:
    """代理 completed.output 为空时必须使用验证后的 done item。"""
    terminal, preview = _consume(*_message_events(), _completed())

    assert terminal.response_id == "resp_1"
    assert terminal.usage.total_tokens == 28
    assert len(terminal.items) == 1
    assert isinstance(terminal.items[0], NormalizedMessageItem)
    assert terminal.items[0].text == "库存 A-17"
    assert "".join(
        event.data["text"] for event in preview if event.kind == "text_delta"
    ) == "库存 A-17"


def test_nonempty_completed_output_must_equal_done_items_by_position() -> None:
    """completed 非空 output 只可作为 done facts 的一致性副本。"""
    matching = deepcopy(_done_message())
    matching["output_index"] = 0
    terminal, _ = _consume(*_message_events(), _completed([matching]))
    assert terminal.items[0].text == "库存 A-17"

    conflicting = deepcopy(matching)
    conflicting["content"] = [{"type": "output_text", "text": "other"}]
    with pytest.raises(InvalidResponseError, match="completed output conflicts"):
        _consume(*_message_events(), _completed([conflicting]))


@pytest.mark.parametrize(
    ("events", "match"),
    [
        (
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": _done_message(),
                }
            ],
            "continuous",
        ),
        (
            [
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "delta": "orphan",
                }
            ],
            "unknown output index",
        ),
        (
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "tool_1", "type": "web_search_call"},
                }
            ],
            "unsupported Codex output item",
        ),
    ],
)
def test_output_indexes_and_item_types_fail_closed(
    events: list[dict[str, object]],
    match: str,
) -> None:
    """不得靠排序或跳过未知 item 掩盖 provider 违规。"""
    accumulator = CodexResponsesAccumulator()
    with pytest.raises(InvalidResponseError, match=match):
        for event in events:
            accumulator.accept(event)


def test_identity_drift_delta_mismatch_and_duplicate_done_are_rejected() -> None:
    """added 后身份/正文不可漂移，done 只能出现一次。"""
    drift = _message_events()
    drift[-1] = deepcopy(drift[-1])
    drift[-1]["item"] = {**_done_message(), "id": "msg_other"}
    with pytest.raises(InvalidResponseError, match="identity changed"):
        _consume(*drift, _completed())

    mismatch = _message_events()
    mismatch[-1] = deepcopy(mismatch[-1])
    mismatch[-1]["item"] = _done_message("terminal differs")
    with pytest.raises(
        InvalidResponseError,
        match="terminal content part changed|delta does not match",
    ):
        _consume(*mismatch, _completed())

    duplicate = _message_events()
    duplicate.insert(-1, deepcopy(duplicate[-1]))
    with pytest.raises(InvalidResponseError, match="after output item done"):
        _consume(*duplicate, _completed())


def test_completed_requires_done_items_and_forbids_later_events() -> None:
    """completed 是唯一完成门，且之后只能 clean EOF。"""
    with pytest.raises(InvalidResponseError, match="at least one done item"):
        _consume(_completed())

    accumulator = CodexResponsesAccumulator()
    for event in [*_message_events(), _completed()]:
        accumulator.accept(event)
    with pytest.raises(InvalidResponseError, match="after response.completed"):
        accumulator.accept({"type": "response.in_progress"})


@pytest.mark.parametrize(
    "patch",
    [
        {"id": ""},
        {"status": "in_progress"},
        {"usage": None},
        {
            "usage": {
                "input_tokens": True,
                "output_tokens": 8,
                "total_tokens": 9,
            }
        },
        {
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "total_tokens": 29,
            }
        },
    ],
)
def test_completed_identity_status_and_usage_are_strict(
    patch: dict[str, object],
) -> None:
    """completed metadata 不得由宽松 int coercion 或缺省值伪造。"""
    completed = _completed()
    completed["response"] = {**completed["response"], **patch}  # type: ignore[dict-item]
    with pytest.raises(InvalidResponseError):
        _consume(*_message_events(), completed)


def test_refusal_is_terminal_failure_and_cannot_mix_with_text() -> None:
    """refusal 不进入 durable conversation。"""
    added = _message_events()[:3]
    refusal = [
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "refusal", "refusal": ""},
        },
        {
            "type": "response.refusal.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "blocked",
        },
        {
            "type": "response.refusal.done",
            "output_index": 0,
            "content_index": 0,
            "refusal": "blocked",
        },
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "refusal", "refusal": "blocked"},
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                **_done_message(),
                "content": [{"type": "refusal", "refusal": "blocked"}],
            },
        },
    ]
    with pytest.raises(ContentFilterError, match="blocked"):
        _consume(*added, *refusal, _completed())

    empty = deepcopy(refusal)
    empty[1]["delta"] = ""
    empty[2]["refusal"] = ""
    empty[3]["part"] = {"type": "refusal", "refusal": ""}
    empty[4]["item"] = {
        **_done_message(),
        "content": [{"type": "refusal", "refusal": ""}],
    }
    with pytest.raises(InvalidResponseError, match="empty refusal"):
        _consume(*added, *empty, _completed())
