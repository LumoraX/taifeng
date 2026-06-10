"""CompositeMemoryStore 单元测试:拼接 / 广播 / 单子异常不传染 / 空序列拒绝。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.context.memory import CompositeMemoryStore, MemoryStore, NullMemoryStore
from taifeng.conversation.models import ResponseItem, user_message

if TYPE_CHECKING:
    from collections.abc import Sequence


class _Src(NullMemoryStore):
    """可配置的子 store:固定 prefetch/digest 返回值,记录写钩子调用。"""

    def __init__(self, *, pf: str = "", digest: str = "", boom: str = "") -> None:
        self._pf = pf
        self._digest = digest
        self._boom = boom  # 钩子名;命中即抛异常
        self.writeback_count = 0
        self.session_end = False

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        if self._boom == "prefetch":
            raise RuntimeError("prefetch exploded")
        return self._pf

    async def writeback(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        if self._boom == "writeback":
            raise RuntimeError("writeback exploded")
        self.writeback_count += 1

    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        return self._digest

    async def on_session_end(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        self.session_end = True


def test_empty_stores_rejected():
    """空序列装配无意义 → 显式 ValueError。"""
    with pytest.raises(ValueError):
        CompositeMemoryStore([])


def test_satisfies_protocol():
    """组合器自身满足 MemoryStore 协议(可嵌套/可直接注入 engine)。"""
    comp = CompositeMemoryStore([_Src()])
    assert isinstance(comp, MemoryStore)


async def test_prefetch_joins_nonempty_in_order():
    """prefetch 按注册序拼接非空结果;全空返回空串。"""
    comp = CompositeMemoryStore([
        _Src(pf="知识库命中"), _Src(pf=""), _Src(pf="会话记忆命中")])
    out = await comp.prefetch("q", thread_id="t")
    assert out == "知识库命中\n\n会话记忆命中"
    empty = CompositeMemoryStore([_Src(), _Src()])
    assert await empty.prefetch("q", thread_id="t") == ""


async def test_write_hooks_broadcast_and_isolate():
    """writeback 广播全部子;单子异常记日志不传染其余。"""
    a, b = _Src(boom="writeback"), _Src()
    comp = CompositeMemoryStore([a, b])
    await comp.writeback(thread_id="t", items=[user_message("x", thread_id="t")])
    assert b.writeback_count == 1  # a 崩溃,b 仍被调用
    await comp.on_session_end(thread_id="t", items=[])
    assert a.session_end and b.session_end


async def test_prefetch_exception_isolated():
    """prefetch 单子异常 → 跳过该子,其余结果照常拼接。"""
    comp = CompositeMemoryStore([_Src(boom="prefetch"), _Src(pf="仍然命中")])
    assert await comp.prefetch("q", thread_id="t") == "仍然命中"


async def test_pre_evict_digests_joined():
    """on_pre_evict 拼接各子非空 digest。"""
    comp = CompositeMemoryStore([
        _Src(digest="要点A"), _Src(), _Src(digest="要点B")])
    out = await comp.on_pre_evict([user_message("x", thread_id="t")])
    assert out == "要点A\n要点B"
