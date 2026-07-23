"""CancellationToken 父子级联取消测试。"""

from __future__ import annotations

import asyncio

import pytest

from taifeng.loop.cancellation import CancellationToken


def test_root_cancel_no_parent() -> None:
    t = CancellationToken(name="r")
    assert not t.is_cancelled
    t.cancel()
    assert t.is_cancelled


def test_child_cancel_propagates_from_parent() -> None:
    parent = CancellationToken(name="p")
    child = parent.child("c")
    grandchild = child.child("g")
    parent.cancel()
    assert parent.is_cancelled
    assert child.is_cancelled
    assert grandchild.is_cancelled


def test_child_cancel_does_not_affect_parent() -> None:
    parent = CancellationToken(name="p")
    child = parent.child("c")
    child.cancel()
    assert child.is_cancelled
    assert not parent.is_cancelled


def test_detach_unlinks_only_exact_parent_child_edge() -> None:
    """detach 只移除当前 token 与 parent 的边，不取消 token/subtree。"""
    parent = CancellationToken(name="p")
    child = parent.child("c")
    grandchild = child.child("g")

    assert child.detach() is True
    assert child.detach() is False
    assert tuple(parent.descendants()) == ()
    assert tuple(child.descendants()) == (grandchild,)
    assert not child.is_cancelled and not grandchild.is_cancelled


def test_root_and_unparented_tokens_cannot_detach() -> None:
    """root 或已无 parent 的 token detach 是安全幂等 no-op。"""
    root = CancellationToken(name="root")

    assert root.detach() is False
    assert tuple(root.descendants()) == ()


def test_already_cancelled_parent_propagates_to_new_child() -> None:
    parent = CancellationToken(name="p")
    parent.cancel()
    child = parent.child("c")
    assert child.is_cancelled


def test_raise_if_cancelled() -> None:
    t = CancellationToken()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        t.raise_if_cancelled()


@pytest.mark.asyncio
async def test_wait_cancelled() -> None:
    t = CancellationToken()

    async def cancel_later() -> None:
        await asyncio.sleep(0.05)
        t.cancel()

    await asyncio.gather(t.wait_cancelled(), cancel_later())
    assert t.is_cancelled
