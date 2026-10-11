"""历史来源观察器的去重、单调与跨 thread 边界。"""

from __future__ import annotations

from taifeng.conversation.models import user_message
from taifeng.conversation.origin import InputOrigin, tag_origin


def test_taint_survives_removal_and_repeated_observation() -> None:
    """移除标记、改写可信度或重复扫描都不能撤销历史事实。"""
    from taifeng.conversation.taint import ThreadTaintRegistry

    registry = ThreadTaintRegistry()
    item = user_message("external", thread_id="root")
    marked = tag_origin(item, InputOrigin(kind="host", trust="untrusted", label="source"))
    assert registry.observe("root", [marked]).item_count == 1
    assert registry.observe("root", [marked]).item_count == 1
    assert registry.observe("root", []).untrusted
    trusted = tag_origin(item, InputOrigin(kind="host", trust="trusted"))
    assert registry.observe("root", [trusted]).untrusted
    assert registry.snapshot("root").labels == ("source",)


def test_taint_is_scoped_to_thread() -> None:
    """子 thread 的输入不会在其内容返回之前污染父 thread。"""
    from taifeng.conversation.taint import ThreadTaintRegistry

    registry = ThreadTaintRegistry()
    registry.observe("child", [tag_origin(user_message("x", thread_id="child"),
                                          InputOrigin(kind="tool", trust="untrusted"))])
    assert registry.snapshot("child").untrusted
    assert not registry.snapshot("root").untrusted
    origin = registry.derived_origin(["child"])
    assert origin is not None and origin.trust == "untrusted"
    parent = tag_origin(user_message("derived", thread_id="root"), origin)
    assert registry.observe("root", [parent]).untrusted


def test_taint_keeps_labels_and_kinds_when_item_changes() -> None:
    """同一条目的新标记不能覆盖已见过的来源信息。"""
    from taifeng.conversation.taint import ThreadTaintRegistry

    registry = ThreadTaintRegistry()
    item = user_message("x", thread_id="root")
    registry.observe("root", [tag_origin(item, InputOrigin(kind="user", trust="untrusted", label="a"))])
    taint = registry.observe("root", [tag_origin(item, InputOrigin(kind="tool", trust="untrusted", label="b"))])
    assert taint.item_count == 1
    assert taint.labels == ("a", "b")
    assert taint.kinds == ("tool", "user")
