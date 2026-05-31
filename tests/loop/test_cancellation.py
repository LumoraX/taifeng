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
