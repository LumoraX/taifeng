"""Journal projector 可恢复 target 故障与 identity 不变量分类测试。"""

from __future__ import annotations

from typing import Never

import pytest

from taifeng.conversation.journal.materialization import ProjectionIdentityError
from taifeng.conversation.journal.projector import (
    JournalConversationProjector,
    ProjectionOrderError,
)
from taifeng.conversation.models import user_message
from tests.conversation.journal.projector_test_support import (
    _NOW,
    _conversation_record,
    _encoded,
    _MemoryProjectionStore,
)


class _ExpectedSessionIoFailingStore(_MemoryProjectionStore):
    """读取投影 Session metadata 时注入可恢复 IO 失败。"""

    async def expected_projection_session_id(self, thread_id: str) -> Never:
        """模拟 directory/metadata target 暂时不可读。"""
        raise OSError(f"injected projection identity IO failure: {thread_id}")


class _MaterializationIdentityFailingStore(_MemoryProjectionStore):
    """在 snapshot load 注入 audited metadata identity 失败。"""

    async def load_projection_snapshot(self, thread_id: str) -> Never:
        """模拟 materialization 发现 projection Session identity 被改写。"""
        raise ProjectionIdentityError(f"projection Journal Session identity changed: {thread_id}")


class _ExpectedSessionIdentityFailingStore(_MemoryProjectionStore):
    """expected-session 读取阶段注入 audited metadata identity 失败。"""

    async def expected_projection_session_id(self, thread_id: str) -> Never:
        """模拟 restart 校验自包含 metadata 时发现 Session 被改写。"""
        raise ProjectionIdentityError(
            f"projection metadata Journal Session identity changed: {thread_id}"
        )


@pytest.mark.anyio
async def test_projection_session_metadata_io_failure_returns_stale() -> None:
    """读取 expected identity 的 IO 失败只使可重建投影 stale。"""
    item = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _ExpectedSessionIoFailingStore()

    result = await JournalConversationProjector(store).project(envelopes, ack)

    assert result.stale is True
    assert result.projected_seq == 0
    assert result.failure_class == "OSError"
    assert result.failure_record_id == "rec_1"
    assert store.append_calls == 0


@pytest.mark.anyio
async def test_materialization_identity_invariant_is_not_downgraded_to_stale() -> None:
    """audited identity 不变量必须传播为 order error，不能伪装成普通 stale。"""
    item = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _MaterializationIdentityFailingStore()

    with pytest.raises(ProjectionOrderError, match="identity"):
        await JournalConversationProjector(store).project(envelopes, ack)

    assert store.projection_state("thr_1") == (None, None)
    assert store.append_calls == 0


@pytest.mark.anyio
async def test_expected_session_identity_invariant_uses_order_error() -> None:
    """expected-session identity 失败也必须进入 Engine 可冻结的统一异常。"""
    item = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _ExpectedSessionIdentityFailingStore()

    with pytest.raises(ProjectionOrderError, match="Journal Session"):
        await JournalConversationProjector(store).project(envelopes, ack)

    assert store.projection_state("thr_1") == (None, None)
