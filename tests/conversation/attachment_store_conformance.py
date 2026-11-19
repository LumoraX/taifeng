"""``AttachmentStore`` 协议一致性套件（attachment-store，ADR 0148）。

宿主实现自己的后端（S3 / DB …）时，导入 ``CONFORMANCE_CASES`` 并对自己的工厂逐个运行即可：

    @pytest.mark.parametrize("case", CONFORMANCE_CASES)
    async def test_my_store(case, tmp_path):
        await case(lambda: MyStore(...))

每个用例只依赖协议本身，不假设引用格式。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.attachment_store import (
    AttachmentIntegrityError,
    AttachmentNotFoundError,
)
from taifeng.llm.attachment_refs import is_valid_ref

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.conversation.attachment_store import AttachmentStore

    StoreFactory = Callable[[], AttachmentStore]

DATA = b"\x89PNG\r\n\x1a\n" + b"conformance-bytes" * 8
DIGEST = hashlib.sha256(DATA).hexdigest()


async def put_returns_valid_ref_and_round_trips(factory: StoreFactory) -> None:
    """写入返回合法引用，读回逐字节一致，exists 为真。"""
    store = factory()
    ref = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    assert is_valid_ref(ref)
    assert await store.get(ref) == DATA
    assert await store.exists(ref)


async def put_is_idempotent(factory: StoreFactory) -> None:
    """同一内容两次写入返回同一引用。"""
    store = factory()
    first = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    second = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    assert first == second


async def put_rejects_wrong_digest(factory: StoreFactory) -> None:
    """声明的摘要与内容不符：抛 AttachmentIntegrityError，且不写入。"""
    store = factory()
    with pytest.raises(AttachmentIntegrityError):
        await store.put(DATA, media_type="image/png", sha256="0" * 64)
    ref = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    assert await store.get(ref) == DATA  # 失败的写入不影响之后的正确写入


async def get_missing_raises(factory: StoreFactory) -> None:
    """读取不存在的对象抛 AttachmentNotFoundError；exists 为假。"""
    store = factory()
    ref = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    await store.delete(ref)
    with pytest.raises(AttachmentNotFoundError):
        await store.get(ref)
    assert not await store.exists(ref)


async def delete_is_idempotent(factory: StoreFactory) -> None:
    """删除不存在的对象为 noop。"""
    store = factory()
    ref = await store.put(DATA, media_type="image/png", sha256=DIGEST)
    await store.delete(ref)
    await store.delete(ref)


async def concurrent_puts_agree(factory: StoreFactory) -> None:
    """并发写入同一内容：全部成功、引用相同、读回一致。"""
    store = factory()
    refs: list[str] = []

    async def put() -> None:
        refs.append(await store.put(DATA, media_type="image/png", sha256=DIGEST))

    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(put)
    assert len(set(refs)) == 1
    assert await store.get(refs[0]) == DATA


CONFORMANCE_CASES: tuple[Callable[[StoreFactory], Awaitable[None]], ...] = (
    put_returns_valid_ref_and_round_trips,
    put_is_idempotent,
    put_rejects_wrong_digest,
    get_missing_raises,
    delete_is_idempotent,
    concurrent_puts_agree,
)

__all__ = ["CONFORMANCE_CASES"]
