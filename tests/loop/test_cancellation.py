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


def test_token_has_no_public_detach_api() -> None:
    """parent edge 只能由 package 内部 owner 管理，不暴露稳定 public detach。"""
    parent = CancellationToken(name="p")
    child = parent.child("c")

    assert not hasattr(child, "detach")
    assert child in tuple(parent.descendants())


def test_private_parent_unlink_is_idempotent_and_preserves_subtree() -> None:
    """package-private 脱链只移除直接 parent edge，不取消 token/subtree。"""
    parent = CancellationToken(name="p")
    child = parent.child("c")
    grandchild = child.child("g")

    assert child._detach_from_parent() is True  # noqa: SLF001
    assert child._detach_from_parent() is False  # noqa: SLF001
    assert tuple(parent.descendants()) == ()
    assert tuple(child.descendants()) == (grandchild,)
    assert not child.is_cancelled and not grandchild.is_cancelled


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
