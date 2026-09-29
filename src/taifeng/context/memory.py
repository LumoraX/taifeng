"""MemoryStore —— 长期记忆的 swap / 缺页接口（K3）。

参照 hermes `agent/memory_provider.py` 的生命周期，但**只保留内核机制、剔除业务字段**
（hermes 在 kwargs 里塞 hermes_home / user_id / platform —— 那是 R1 违例，本协议不带）。

定位：这是内核的「内存层级 / demand-paging」机制。短期工作集在 history（≈ 物理内存），
被压缩换页（≈ page replacement）后默认丢弃；本协议把「换出去的东西」接到长期存储，
让上下文可按需换入。**协议层不绑后端**（向量 / KV / RAG / PG 全是 userspace）。

四个钩子对应 VM/swap 语义：

| 钩子 | 语义 | 触发点 |
| --- | --- | --- |
| ``prefetch(query)`` | page-in / 按需取回 | turn 计算前一次（不进 per-tool 循环） |
| ``writeback(items)`` | dirty-page 写回 | turn 结束后（fire-and-forget） |
| ``on_pre_evict(items)`` | swap-out 抢救 | 压缩（换页）丢弃前；返回文本折进摘要（R2 面） |
| ``on_session_end(items)`` | teardown / 最终 flush | engine shutdown |

所有钩子由内核 best-effort 调用：实现抛异常**不得**打断主 turn（内核吞掉 + 记日志）。
返回文本的钩子（prefetch / on_pre_evict）异常时按返回空串处理。

**只读知识库最简接入**：继承 ``NullMemoryStore`` 仅覆写 ``prefetch`` 即构成合法
实现（其余三钩子 no-op）——接外部知识库/向量 DB 只需三行::

    class KnowledgeBase(NullMemoryStore):
        async def prefetch(self, query: str, *, thread_id: str) -> str:
            hits = await self._backend.search(query, top_k=3)
            return "\\n".join(h.text for h in hits) if hits else ""

多源(知识库 + 会话记忆)用 ``CompositeMemoryStore`` 组合；检索 query 的构造可经
``EnginePool.create(memory_query_builder=...)`` 定制（默认 = 最后一条用户消息）。

**可选删除语义**（ADR 0071）：``ForgettableMemoryStore`` 在四钩子之外多一个 ``forget``。它是
可选协议——``MemoryStore`` 本身不变，既有实现零改动；内核被动路径从不调用 ``forget``，只有
模型侧 ``memory`` 工具在 store 实现了它时才提供 ``delete`` 动作。``NullMemoryStore`` 刻意
**不**实现 ``forget``：继承它的只读知识库不会意外获得删除入口。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.models import ResponseItem

logger = logging.getLogger(__name__)


@runtime_checkable
class MemoryStore(Protocol):
    """长期记忆 swap 接口（协议层，后端业务实现）。"""

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        """page-in：按 query 取回相关长期记忆，注入本 turn 上下文。

        Returns:
            注入文本；空串表示无相关记忆（内核不注入）。应**非阻塞/快**
            （命中缓存即返回；重计算放 writeback / 后台）。
        """
        ...

    async def writeback(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        """dirty-page 写回：turn 结束后把本 turn 新增内容异步写入长期存储。"""
        ...

    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        """swap-out 抢救：压缩丢弃这些 items 前，把要点抢救进长期存储。

        Returns:
            一段「必须留在上下文里」的精简文本（会折进压缩摘要，参与 R2）；
            空串表示无补充。
        """
        ...

    async def on_session_end(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        """teardown：会话结束的最终 flush。"""
        ...


@runtime_checkable
class ForgettableMemoryStore(MemoryStore, Protocol):
    """可选扩展协议：支持按模型给出的依据删除长期记忆（ADR 0071）。

    只多一个 ``forget``；是否实现由后端决定（遗忘 / 覆盖策略因后端而异，属于规则③）。
    ``isinstance(store, ForgettableMemoryStore)`` 为真时，``make_memory_tool`` 缺省启用
    ``delete`` 动作。
    """

    async def forget(self, target: str, *, thread_id: str) -> int:
        """删除与 ``target`` 对应的长期记忆条目。

        ``prefetch`` 只返回一段文本、没有结构化条目，模型能表达的删除依据因此也只有文本：
        后端在 prefetch 结果里展示的记忆标识（如 ``[mem:42]``），或该条记忆的原文 / 足以唯一
        定位它的片段。如何解析由后端决定；**推荐精确匹配**（标识或原文），不要按语义相似度批量
        删除——删除不可逆，而模型只看得到 prefetch 返回的那一部分。

        Args:
            target: 模型给出的删除依据（非空，工具层已校验长度）。
            thread_id: 发起删除的 thread（与 prefetch / writeback 同一作用域语义）。

        Returns:
            实际删除的条目数（>= 0）；0 表示没有匹配，不是错误。

        Raises:
            Exception: 后端失败；memory 工具以 ``memory_error`` 显式返回给模型。
        """
        ...


class NullMemoryStore:
    """无操作默认实现 —— 不接长期记忆时的零开销占位（engine 默认用 None，不用它）。

    刻意不实现 ``forget``：继承它只覆写 ``prefetch`` 的只读知识库不应获得删除入口。
    """

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        return ""

    async def writeback(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        return None

    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        return ""

    async def on_session_end(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        return None

class CompositeMemoryStore:
    """多源组合器 —— 把多个 MemoryStore 以 fan-out 方式合成一个(R1:只组合不决策)。

    典型装配:「只读知识库 + 会话长期记忆」双源::

        memory = CompositeMemoryStore([KnowledgeBase(vec), SessionMemory(db)])
        pool = await EnginePool.create(..., memory_store=memory)

    聚合语义:
      - ``prefetch``:按注册序逐个调用,非空结果以空行拼接(全空返回空串);
      - ``writeback`` / ``on_session_end``:广播全部子 store;
      - ``on_pre_evict``:逐个调用,非空 digest 以换行拼接。
    单个子 store 抛异常 → 记日志后继续其余子(不传染,与内核对单 store 的
    best-effort 语义一致)。不去重、不限长:结果体积由各子 store 自律,
    prompt 层既有截断兜底——组合器只做 fan-out,不引入第二套护栏。

    删除语义(ADR 0071):任一子 store 实现 ``ForgettableMemoryStore`` 时,构造出的实例
    自动带 ``forget``(满足该协议),转发给可遗忘的子并返回删除总数;没有可遗忘的子时实例
    不带 ``forget``,memory 工具也就不会提供 delete——不会出现「给了入口却什么都删不掉」。
    """

    def __new__(cls, stores: Sequence[MemoryStore]) -> CompositeMemoryStore:
        """按子 store 能力选择实例类型:有可遗忘的子 → 带 ``forget`` 的私有子类。

        只在直接构造 ``CompositeMemoryStore`` 时切换;业务自定义子类保持原样。
        """
        if cls is CompositeMemoryStore and any(
            isinstance(s, ForgettableMemoryStore) for s in stores
        ):
            return super().__new__(_ForgettableCompositeMemoryStore)
        return super().__new__(cls)

    def __init__(self, stores: Sequence[MemoryStore]) -> None:
        """Args:
            stores: 子 store 序列(按序决定 prefetch 拼接顺序);空序列是
                无意义装配,显式 ``ValueError`` 拒绝。
        """
        if not stores:
            raise ValueError("CompositeMemoryStore: stores 不能为空")
        self._stores: list[MemoryStore] = list(stores)

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        """按注册序拼接各子的非空 prefetch 结果(空行分隔)。"""
        parts: list[str] = []
        for s in self._stores:
            try:
                text = await s.prefetch(query, thread_id=thread_id)
            except Exception:  # 单子崩溃不传染(best-effort,有日志非静默)
                logger.exception("composite memory prefetch failed (skipped)")
                continue
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    async def writeback(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        """广播写回到全部子 store;单子异常记日志后继续。"""
        for s in self._stores:
            try:
                await s.writeback(thread_id=thread_id, items=items)
            except Exception:
                logger.exception("composite memory writeback failed (skipped)")

    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        """逐个调用,拼接各子非空 digest(换行分隔)。"""
        parts: list[str] = []
        for s in self._stores:
            try:
                digest = await s.on_pre_evict(items)
            except Exception:
                logger.exception("composite memory on_pre_evict failed (skipped)")
                continue
            if digest:
                parts.append(digest)
        return "\n".join(parts)

    async def on_session_end(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        """广播 teardown 到全部子 store;单子异常记日志后继续。"""
        for s in self._stores:
            try:
                await s.on_session_end(thread_id=thread_id, items=items)
            except Exception:
                logger.exception("composite memory on_session_end failed (skipped)")


class _ForgettableCompositeMemoryStore(CompositeMemoryStore):
    """至少一个子 store 可遗忘时的组合器(由 ``CompositeMemoryStore.__new__`` 选中)。"""

    async def forget(self, target: str, *, thread_id: str) -> int:
        """按注册序转发给可遗忘的子 store,返回删除总数。

        与被动钩子的 best-effort 不同,这是模型主动发起的删除:某个子失败(抛错或返回非法计数)
        时其余子照做,最后抛 ``RuntimeError`` 写明已删除数与失败明细,让模型看见部分失败。

        Raises:
            RuntimeError: 至少一个可遗忘的子失败。
        """
        total = 0
        failures: list[str] = []
        for s in self._stores:
            if not isinstance(s, ForgettableMemoryStore):
                continue
            try:
                count = await s.forget(target, thread_id=thread_id)
            except Exception as exc:
                failures.append(f"{type(s).__name__}: {type(exc).__name__}: {exc}")
                continue
            # 子 store 是业务实现(系统边界):计数必须是非负整数,bool 不算
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                failures.append(f"{type(s).__name__}: invalid forget count {count!r}")
                continue
            total += count
        if failures:
            raise RuntimeError(
                f"forget partially failed after deleting {total} record(s): " + "; ".join(failures)
            )
        return total
