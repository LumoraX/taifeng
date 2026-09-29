"""CompositeMemoryStore 单元测试:拼接 / 广播 / 单子异常不传染 / 空序列拒绝 /
可遗忘子的 forget 转发与部分失败显式报错(ADR 0071)。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.context.memory import (
    CompositeMemoryStore,
    ForgettableMemoryStore,
    MemoryStore,
    NullMemoryStore,
)
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


class _Forgetful(_Src):
    """可遗忘的子 store:返回预置计数或抛错,记录收到的删除依据。"""

    def __init__(self, deleted: object = 1, *, fail: bool = False) -> None:
        super().__init__()
        self._deleted = deleted
        self._fail = fail
        self.targets: list[tuple[str, str]] = []

    async def forget(self, target: str, *, thread_id: str) -> int:
        self.targets.append((target, thread_id))
        if self._fail:
            raise ConnectionError("kv down")
        return self._deleted  # type: ignore[return-value]


def test_null_store_is_not_forgettable():
    """NullMemoryStore 刻意不实现 forget:继承它的只读知识库不获得删除入口。"""
    assert not isinstance(NullMemoryStore(), ForgettableMemoryStore)
    assert isinstance(_Forgetful(), ForgettableMemoryStore)


def test_composite_forgettable_iff_some_child_is():
    """有可遗忘的子 → 组合实例满足 ForgettableMemoryStore;全都不可遗忘 → 不满足。"""
    assert not isinstance(CompositeMemoryStore([_Src(), _Src()]), ForgettableMemoryStore)
    mixed = CompositeMemoryStore([_Src(pf="kb"), _Forgetful()])
    assert isinstance(mixed, ForgettableMemoryStore)
    assert isinstance(mixed, CompositeMemoryStore)
    assert isinstance(mixed, MemoryStore)


async def test_composite_forget_sums_forgettable_children():
    """forget 只转发给可遗忘的子,返回删除总数;其余钩子行为不变。"""
    a, b = _Forgetful(2), _Forgetful(0)
    comp = CompositeMemoryStore([_Src(pf="kb"), a, b])
    assert isinstance(comp, ForgettableMemoryStore)
    assert await comp.forget("[mem:1]", thread_id="t") == 2
    assert a.targets == b.targets == [("[mem:1]", "t")]
    assert await comp.prefetch("q", thread_id="t") == "kb"


async def test_composite_forget_partial_failure_is_explicit():
    """某子失败(抛错 / 非法计数)时其余照做,最后抛错写明已删数与失败明细。"""
    broken, bad_count, ok = _Forgetful(fail=True), _Forgetful(-3), _Forgetful(1)
    comp = CompositeMemoryStore([broken, bad_count, ok])
    assert isinstance(comp, ForgettableMemoryStore)
    with pytest.raises(RuntimeError) as exc_info:
        await comp.forget("x", thread_id="t")
    message = str(exc_info.value)
    assert "after deleting 1 record(s)" in message
    assert "ConnectionError: kv down" in message
    assert "invalid forget count -3" in message
    assert ok.targets == [("x", "t")]  # 失败的子不挡住后续子
