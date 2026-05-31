"""MessageStore 协议 —— 业务侧可替换。

参照：codex codex-rs/rollout/src/recorder.rs
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from taifeng.conversation.models import ResponseItem, ThreadInfo


@runtime_checkable
class MessageStore(Protocol):
    """会话存储协议。

    默认实现：``JsonlMessageStore``（追加写 + SQLite 旁路索引）
    业务实现示例：``PostgresMessageStore``（直接落 DB，无 resume）
    """

    async def append(self, item: ResponseItem) -> None:
        """追加单条消息。热路径，必须 < 5ms。"""
        ...

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """批量追加。可优化 fsync。"""
        ...

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """按 thread_id 加载全部消息。"""
        ...

    async def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int = 50,
    ) -> list[ThreadInfo]:
        """按 cwd 过滤列出最近 thread。"""
        ...

    async def create_thread(
        self,
        *,
        cwd: str | None = None,
        entry_skill_id: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """创建新 thread，返回 thread_id。

        ``extra``：JSON-safe 业务/内核元数据，落入 ``ThreadMetadata.extra``（K7：子 skill
        派发时记 ``parent_thread_id`` / ``spawn_depth``，使 resume 可重导谱系与深度）。
        """
        ...

    async def select_resume_path(self, cwd: str) -> str | None:
        """按 cwd 启发式选择 resume thread_id。"""
        ...

    async def close(self) -> None:
        """关闭底层资源（文件句柄 / 数据库连接）。"""
        ...
