"""按 thread 观察历史输入来源，保留只增不减的事实（ADR 0130）。

不保留正文，不改变会话条目；冷恢复对原始 append-only 记录调用 observe。
公开的 InputTaint 是不可变快照，本观察器只在事件循环上由内核管理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.origin import InputTaint, origin_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from taifeng.conversation.journal.models import JournalEnvelope
    from taifeng.conversation.models import ResponseItem
    from taifeng.conversation.origin import InputOrigin


class _ObservedTaint:
    """单个 thread 的最小来源集合，不保存条目或正文。"""

    def __init__(self) -> None:
        """从空事实集合开始。"""
        self.item_ids: set[str] = set()
        self.kinds: set[str] = set()
        self.labels: set[str] = set()

    def snapshot(self) -> InputTaint:
        """返回与可变观察状态脱离的快照。"""
        return InputTaint(untrusted=bool(self.item_ids), kinds=tuple(sorted(self.kinds)),
                          labels=tuple(sorted(self.labels)), item_count=len(self.item_ids))


class ThreadTaintRegistry:
    """每个 thread 独立保留已观察的不可信声明。"""

    def __init__(self) -> None:
        """创建空的内部来源登记表。"""
        self._threads: dict[str, _ObservedTaint] = {}

    def observe(self, thread_id: str, history: Sequence[ResponseItem]) -> InputTaint:
        """幂等观察条目；已知不可信事实不会因条目移除或可信标记而撤销。"""
        state = self._threads.setdefault(thread_id, _ObservedTaint())
        for item in history:
            origin = origin_of(item)
            if origin is None or origin.trust != "untrusted":
                continue
            state.item_ids.add(item.id)
            state.kinds.add(origin.kind)
            if origin.label is not None:
                labels = origin.label.split(",") if origin.kind == "derived" else [origin.label]
                state.labels.update(label for label in labels if label)
        return state.snapshot()

    def observe_journal(self, envelopes: Sequence[JournalEnvelope]) -> None:
        """从审计已提交原始条目恢复各 thread 来源，折叠前的事实也保留。"""
        from taifeng.conversation.journal.item_records import deserialize_response_item
        from taifeng.conversation.journal.records import ConversationItemV1

        for envelope in envelopes:
            if envelope.record_type != "conversation_item":
                continue
            payload = ConversationItemV1.model_validate(envelope.payload)
            item = deserialize_response_item(payload)
            self.observe(item.thread_id, [item])

    def snapshot(self, thread_id: str) -> InputTaint:
        """读取 thread 的累计事实；没有任何观察时返回空快照。"""
        state = self._threads.get(thread_id)
        return state.snapshot() if state is not None else InputTaint()

    def derived_origin(self, thread_ids: Iterable[str]) -> InputOrigin | None:
        """聚合派生内容的各来源 thread，供子结果与 join 种子继承。"""
        taints = [self.snapshot(thread_id) for thread_id in thread_ids]
        labels = tuple(sorted({label for taint in taints for label in taint.labels}))
        kinds = tuple(sorted({kind for taint in taints for kind in taint.kinds}))
        return InputTaint(untrusted=any(taint.untrusted for taint in taints),
                          kinds=kinds, labels=labels).derived_origin()


__all__ = ["ThreadTaintRegistry"]
