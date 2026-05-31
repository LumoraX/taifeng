"""对话持久化 —— ResponseItem 统一消息单元、三协议（MessageWriter / ThreadDirectory / IndexHook）+ JSONL 主存 + SQLite 索引。

设计文档：docs/architecture/conversation.md
spec：docs/architecture/conversation.md
"""

from taifeng.conversation.errors import DirectoryError, ThreadNotFoundError
from taifeng.conversation.models import (
    ItemKind,
    RebuildReport,
    ResponseItem,
    ThreadFilter,
    ThreadInfo,
    ThreadMetadata,
    ThreadPage,
    assistant_message,
    compacted,
    function_call,
    function_call_output,
    reasoning,
    system_injection,
    user_message,
)
from taifeng.conversation.protocols import (
    IndexHook,
    MessageWriter,
    NoopIndexHook,
    NullThreadDirectory,
    ThreadDirectory,
)
from taifeng.conversation.rebuild import rebuild_index
from taifeng.conversation.sqlite_directory import SqliteThreadDirectory
from taifeng.conversation.store import MessageStore
from taifeng.conversation.transcript import (
    JsonlMessageStore,
    JsonlMessageWriter,
    iter_thread_files,
)

__all__ = [
    "DirectoryError",
    "IndexHook",
    "ItemKind",
    "JsonlMessageStore",
    "JsonlMessageWriter",
    "MessageStore",
    "MessageWriter",
    "NoopIndexHook",
    "NullThreadDirectory",
    "RebuildReport",
    "ResponseItem",
    "SqliteThreadDirectory",
    "ThreadDirectory",
    "ThreadFilter",
    "ThreadInfo",
    "ThreadMetadata",
    "ThreadNotFoundError",
    "ThreadPage",
    "assistant_message",
    "compacted",
    "function_call",
    "function_call_output",
    "iter_thread_files",
    "rebuild_index",
    "reasoning",
    "system_injection",
    "user_message",
]
