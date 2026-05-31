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
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from taifeng.conversation.models import ResponseItem


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


class NullMemoryStore:
    """无操作默认实现 —— 不接长期记忆时的零开销占位（engine 默认用 None，不用它）。"""

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
