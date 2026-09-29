"""输入来源标记的数据契约（ADR 0085）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from taifeng.conversation.models import (
    assistant_message,
    function_call_output,
    user_message,
)
from taifeng.conversation.origin import (
    ORIGIN_METADATA_KEY,
    InputOrigin,
    InputTaint,
    origin_of,
    summarize_taint,
    tag_origin,
)

T = "thr"
_WEB = InputOrigin(kind="tool", trust="untrusted", label="http_request")
_MAIL = InputOrigin(kind="user", trust="untrusted", label="email")
_OPERATOR = InputOrigin(kind="user", trust="trusted")


def test_tag_and_read_back() -> None:
    item = tag_origin(user_message("hi", thread_id=T), _MAIL)

    assert item.metadata[ORIGIN_METADATA_KEY] == {
        "kind": "user", "trust": "untrusted", "label": "email",
    }
    assert origin_of(item) == _MAIL


def test_label_is_omitted_when_absent() -> None:
    item = tag_origin(user_message("hi", thread_id=T), _OPERATOR)
    assert item.metadata[ORIGIN_METADATA_KEY] == {"kind": "user", "trust": "trusted"}


def test_tagging_keeps_identity_payload_and_other_metadata() -> None:
    base = user_message("hi", thread_id=T).model_copy(update={"metadata": {"k": 1}})

    tagged = tag_origin(base, _MAIL)

    assert (tagged.id, tagged.payload, tagged.created_at) == (
        base.id, base.payload, base.created_at,
    )
    assert tagged.metadata["k"] == 1
    assert ORIGIN_METADATA_KEY not in base.metadata


def test_none_origin_returns_the_same_item() -> None:
    item = user_message("hi", thread_id=T)
    assert tag_origin(item, None) is item


def test_untagged_item_has_no_origin() -> None:
    assert origin_of(user_message("hi", thread_id=T)) is None


@pytest.mark.parametrize(
    "raw",
    [
        "untrusted",
        {"kind": "user"},
        {"kind": "alien", "trust": "trusted"},
        {"kind": "user", "trust": "maybe"},
        {"kind": "user", "trust": "trusted", "label": ""},
        {"kind": "user", "trust": "trusted", "extra": 1},
    ],
)
def test_malformed_origin_metadata_fails_loudly(raw: object) -> None:
    item = user_message("hi", thread_id=T).model_copy(
        update={"metadata": {ORIGIN_METADATA_KEY: raw}}
    )
    with pytest.raises(ValueError, match="malformed origin"):
        origin_of(item)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "alien", "trust": "trusted"},
        {"kind": "user", "trust": "maybe"},
        {"kind": "user", "trust": "trusted", "label": ""},
        {"kind": "user", "trust": "trusted", "label": "x" * 129},
    ],
)
def test_invalid_origin_is_rejected(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        InputOrigin(**kwargs)  # type: ignore[arg-type]


def test_summary_counts_only_declared_untrusted_items() -> None:
    history = [
        tag_origin(user_message("操作员", thread_id=T), _OPERATOR),
        user_message("未声明", thread_id=T),
        assistant_message("答", thread_id=T, model="m"),
        tag_origin(user_message("邮件正文", thread_id=T), _MAIL),
        tag_origin(function_call_output("c1", "网页", thread_id=T), _WEB),
        tag_origin(function_call_output("c2", "网页二", thread_id=T), _WEB),
    ]

    taint = summarize_taint(history)

    assert taint == InputTaint(
        untrusted=True, kinds=("tool", "user"), labels=("email", "http_request"),
        item_count=3,
    )
    assert taint.to_dict() == {
        "untrusted": True, "kinds": ["tool", "user"],
        "labels": ["email", "http_request"], "item_count": 3,
    }


def test_clean_history_has_no_taint() -> None:
    history = [tag_origin(user_message("操作员", thread_id=T), _OPERATOR)]
    assert summarize_taint(history) == InputTaint()
    assert summarize_taint([]) == InputTaint()
    assert InputTaint().derived_origin() is None


def test_derived_origin_carries_the_source_labels() -> None:
    taint = summarize_taint([
        tag_origin(user_message("邮件", thread_id=T), _MAIL),
        tag_origin(function_call_output("c1", "网页", thread_id=T), _WEB),
    ])

    derived = taint.derived_origin()

    assert derived == InputOrigin(
        kind="derived", trust="untrusted", label="email,http_request"
    )


def test_derived_labels_are_split_back_when_summarized() -> None:
    """派生内容再被汇总时，标签还原成各自的来源，不产生「a,b」这样的合成标签。"""
    derived = InputOrigin(kind="derived", trust="untrusted", label="email,http_request")
    history = [tag_origin(user_message("摘要", thread_id=T), derived)]

    taint = summarize_taint(history)

    assert taint.labels == ("email", "http_request")
    assert taint.kinds == ("derived",)


def test_derived_origin_without_labels() -> None:
    unlabeled = InputOrigin(kind="peer", trust="untrusted")
    taint = summarize_taint([tag_origin(user_message("x", thread_id=T), unlabeled)])
    assert taint.derived_origin() == InputOrigin(kind="derived", trust="untrusted")


def test_overlong_joined_labels_are_truncated() -> None:
    history = [
        tag_origin(
            user_message("x", thread_id=T),
            InputOrigin(kind="tool", trust="untrusted", label=f"tool-{i:03d}-" + "n" * 20),
        )
        for i in range(10)
    ]
    derived = summarize_taint(history).derived_origin()
    assert derived is not None and derived.label is not None
    assert len(derived.label) == 128


def test_summary_refuses_malformed_metadata() -> None:
    bad = user_message("hi", thread_id=T).model_copy(
        update={"metadata": {ORIGIN_METADATA_KEY: {"kind": "user"}}}
    )
    with pytest.raises(ValueError, match="malformed origin"):
        summarize_taint([bad])
