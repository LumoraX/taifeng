"""ResponseItem —— 持久化的统一消息单元 + ThreadDirectory 相关 metadata 类型。

参照：codex codex-rs/codex-protocol/src/models.rs
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ItemKind = Literal[
    "user_message",
    "assistant_message",
    "function_call",
    "function_call_output",
    "reasoning",
    "compacted",
    "system_injection",
]


def _generate_id(prefix: str = "item") -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class ResponseItem(BaseModel):
    """对话单元 —— 落 JSONL 的最小单位。

    Payload schema 因 kind 而异：

    - user_message:           {"text": str, "attachments": list[dict]}
    - assistant_message:      {"text": str, "model": str}
    - function_call:          {"call_id": str, "name": str, "arguments": str}
    - function_call_output:   {"call_id": str, "output": str, "is_error": bool}
    - reasoning:              {"text": str, "summary": str}
    - compacted:              {"summary": str, "replaced_range": [int, int], "cache_invalidated": bool}
    - system_injection:       {"text": str, "source": str}
    """

    kind: ItemKind
    id: str = Field(default_factory=lambda: _generate_id())
    thread_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- 强类型构造器 ---


def user_message(text: str, *, thread_id: str, attachments: list[dict[str, Any]] | None = None) -> ResponseItem:
    return ResponseItem(
        kind="user_message",
        thread_id=thread_id,
        payload={"text": text, "attachments": attachments or []},
    )


def assistant_message(text: str, *, thread_id: str, model: str) -> ResponseItem:
    return ResponseItem(
        kind="assistant_message",
        thread_id=thread_id,
        payload={"text": text, "model": model},
    )


def function_call(call_id: str, name: str, arguments: str, *, thread_id: str) -> ResponseItem:
    return ResponseItem(
        kind="function_call",
        thread_id=thread_id,
        payload={"call_id": call_id, "name": name, "arguments": arguments},
    )


def function_call_output(
    call_id: str,
    output: str,
    *,
    thread_id: str,
    is_error: bool = False,
) -> ResponseItem:
    return ResponseItem(
        kind="function_call_output",
        thread_id=thread_id,
        payload={"call_id": call_id, "output": output, "is_error": is_error},
    )


def reasoning(text: str, *, thread_id: str, summary: str = "") -> ResponseItem:
    return ResponseItem(
        kind="reasoning",
        thread_id=thread_id,
        payload={"text": text, "summary": summary},
    )


def compacted(
    summary: str,
    *,
    thread_id: str,
    replaced_range: tuple[int, int],
    cache_invalidated: bool,
) -> ResponseItem:
    return ResponseItem(
        kind="compacted",
        thread_id=thread_id,
        payload={
            "summary": summary,
            "replaced_range": list(replaced_range),
            "cache_invalidated": cache_invalidated,
        },
    )


def system_injection(text: str, *, thread_id: str, source: str) -> ResponseItem:
    return ResponseItem(
        kind="system_injection",
        thread_id=thread_id,
        payload={"text": text, "source": source},
    )


# --- ThreadInfo —— 线程元数据 ---


class ThreadInfo(BaseModel):
    """Legacy ThreadInfo —— 兼容封装 ``JsonlMessageStore.list_threads`` 旧 API 时仍返回此类型。

    新代码请用 ``ThreadMetadata`` + ``ThreadDirectory`` 协议。
    """

    thread_id: str
    cwd: str | None = None
    created_at: datetime
    last_activity_at: datetime
    item_count: int = 0
    entry_skill_id: str | None = None
    source: str | None = None


# --- ThreadDirectory 协议相关 ---


@dataclass(frozen=True, slots=True)
class ThreadMetadata:
    """thread 元数据 —— ThreadDirectory 列表 / 查询的返回单元，也是 JSONL 首行 ``__meta__`` 落盘内容。

    字段语义：

    - ``thread_id``: 全局唯一标识
    - ``created_at`` / ``updated_at``: UTC epoch 秒
    - ``entry_skill_id``: 创建 thread 时声明的入口 skill
    - ``source``: 来源标识，约定值 ``"user"`` / ``"subskill:<parent_id>"`` / ``"system"``
    - ``tags``: 业务自定义标签元组（不可变）
    - ``extra``: 业务自定义 JSON-safe 字段（兼容场景下 ``cwd`` 落在此处）
    """

    thread_id: str
    created_at: float
    updated_at: float
    entry_skill_id: str
    source: str = "user"
    tags: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ThreadFilter:
    """``ThreadDirectory.list_threads`` 过滤条件。

    多字段非空 SHALL 应用 AND 语义；全空（None）表示不过滤。
    """

    entry_skill_id: str | None = None
    created_after: float | None = None
    created_before: float | None = None
    tag: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadPage:
    """``ThreadDirectory.list_threads`` 返回的分页结果。

    - ``items``: 按 ``updated_at`` 倒序（最新优先）
    - ``next_cursor``: None 表示已到末尾；非空可作为下次调用的 ``cursor`` 参数
    """

    items: list[ThreadMetadata]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """``rebuild_index`` 工具返回的统计报告。

    - ``scanned_count``: 扫描的 thread 文件总数
    - ``indexed_count``: 成功写入 directory 的数量（dry_run 模式下也累计）
    - ``orphan_count``: 跳过的 orphan 数量
    - ``error_count``: 因损坏 / IO 失败跳过的数量
    - ``elapsed_ms``: 总耗时毫秒
    """

    scanned_count: int
    indexed_count: int
    orphan_count: int
    error_count: int
    elapsed_ms: float
