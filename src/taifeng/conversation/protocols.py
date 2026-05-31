"""三协议定义 —— MessageWriter / ThreadDirectory / IndexHook + 零成本默认实现。

设计参见 ``docs/architecture/conversation.md``：

- ``MessageWriter``: append-only 消息体写 + replay；JSONL 主存是 source-of-truth
- ``ThreadDirectory``: 列表 / 元数据查询；默认 ``SqliteThreadDirectory`` 是 derived index 可重建
- ``IndexHook``: fire-and-forget 业务事件订阅（审计 / metrics / 异步投递）

红线 R1：本文件**不**包含具体存储实现，仅是协议 + 零成本默认（NoopIndexHook / NullThreadDirectory）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from taifeng.conversation.models import (
    ResponseItem,
    ThreadFilter,
    ThreadMetadata,
    ThreadPage,
)


@runtime_checkable
class MessageWriter(Protocol):
    """消息体追加写协议 —— 主存 source-of-truth。

    所有 thread body 的写 / 读路径必须满足：

    - ``create_thread`` 生成全局唯一 thread_id 并在主存内首行写入 ``ThreadMetadata``（自包含，rebuild 时可恢复）
    - ``append`` 仅追加，不修改已写入数据；并发 append 不撕裂单 item 的序列化结果
    - ``load_history`` 完整回放，跳过首条 metadata，跳过损坏行（不抛异常）
    """

    async def create_thread(
        self,
        *,
        entry_skill_id: str,
        source: str = "user",
        tags: tuple[str, ...] = (),
        extra: dict[str, Any] | None = None,
    ) -> str:
        """创建新 thread；返回 thread_id。"""
        ...

    async def append(self, thread_id: str, items: list[ResponseItem]) -> None:
        """追加一组 item 到指定 thread 主存末尾。append-only。"""
        ...

    async def load_history(self, thread_id: str) -> list[ResponseItem]:
        """按写入顺序回放 thread 全部 item（不含首条 metadata）。"""
        ...


@runtime_checkable
class ThreadDirectory(Protocol):
    """thread 元数据索引协议 —— derived，可从 MessageWriter 主存重建。

    默认实现 ``SqliteThreadDirectory`` 在协议契约之上额外承诺：

    - 启动期 schema 版本不匹配 → drop tables + ``rebuild_index`` + 发 ``sqlite_schema_rebuilt``
    - 启动期 ``integrity_check`` 损坏 → rename 备份 + 重建 + 发 ``sqlite_db_corrupt_rebuilt``
    - 所有 async 方法不阻塞 event loop（同步 IO 派发到 thread pool）

    业务侧自定义实现（Redis / PG / ES）可只满足协议契约，无需上述额外行为。
    """

    async def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        filter: ThreadFilter | None = None,
    ) -> ThreadPage:
        """按 ``updated_at`` 倒序 + 过滤 + 游标分页列出 thread。"""
        ...

    async def get_metadata(self, thread_id: str) -> ThreadMetadata | None:
        """按 thread_id 取元数据；不存在返回 None。"""
        ...

    async def update_metadata(self, thread_id: str, patch: dict[str, Any]) -> None:
        """部分合并 patch 到现有 metadata（last-write-wins + 自动刷新 ``updated_at``）。

        thread_id 不存在 SHALL raise ``ThreadNotFoundError``。
        """
        ...

    async def upsert_metadata(self, meta: ThreadMetadata) -> None:
        """不存在则插入；存在则整体替换。由 ``MessageWriter.create_thread`` 与 ``rebuild_index`` 调用。"""
        ...


@runtime_checkable
class IndexHook(Protocol):
    """thread 生命周期事件订阅协议 —— fire-and-forget 调用，失败发 EventMsg 不阻塞主路径。

    业务侧实现示例：

    - 投递审计日志 / metrics
    - 异步写入业务表（ES / Kafka / 业务 DB）
    - 触发下游 webhook

    引擎 spawn background task 调用本协议方法；hook 异常 → ``index_hook_failed`` 事件。
    """

    async def on_thread_created(self, meta: ThreadMetadata) -> None:
        """新 thread 已创建。"""
        ...

    async def on_message_appended(self, thread_id: str, items: list[ResponseItem]) -> None:
        """一批 item 已追加到 thread。"""
        ...

    async def on_metadata_updated(self, thread_id: str, patch: dict[str, Any]) -> None:
        """thread metadata 已被 patch 更新。"""
        ...


class NoopIndexHook:
    """默认 IndexHook 实现 —— 三方法都 ``pass``，零开销。

    业务侧无 hook 需求时的零成本默认值；构造 ``EnginePool`` 未传 ``index_hook`` 时自动使用。
    """

    async def on_thread_created(self, meta: ThreadMetadata) -> None:
        pass

    async def on_message_appended(self, thread_id: str, items: list[ResponseItem]) -> None:
        pass

    async def on_metadata_updated(self, thread_id: str, patch: dict[str, Any]) -> None:
        pass


class NullThreadDirectory:
    """显式不要索引的 ThreadDirectory 实现 —— list 永远空、update/upsert 静默丢弃。

    用途：纯单 thread CLI / 测试场景跳过 SQLite 初始化。
    调任意方法 SHALL 不写文件 / 不打开连接 / 不抛异常 / 不发事件。
    """

    async def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        filter: ThreadFilter | None = None,
    ) -> ThreadPage:
        return ThreadPage(items=[], next_cursor=None)

    async def get_metadata(self, thread_id: str) -> ThreadMetadata | None:
        return None

    async def update_metadata(self, thread_id: str, patch: dict[str, Any]) -> None:
        # 静默丢弃（与默认 SqliteThreadDirectory 的 ThreadNotFoundError 行为不同）
        return None

    async def upsert_metadata(self, meta: ThreadMetadata) -> None:
        return None


__all__ = [
    "IndexHook",
    "MessageWriter",
    "NoopIndexHook",
    "NullThreadDirectory",
    "ThreadDirectory",
]
