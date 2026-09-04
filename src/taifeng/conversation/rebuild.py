"""rebuild_index —— 从 MessageWriter 主存全量重建 ThreadDirectory。

用途（spec：jsonl-transcript capability）：

- SqliteThreadDirectory schema 升级触发的自动 drop+rebuild（T3 内部用）
- 运维场景：业务侧 Redis/PG 索引丢失后，从 JSONL 主存恢复全部 thread 元数据
- 数据迁移：换 ThreadDirectory 实现（默认 SQLite → Redis）时的初次填充

行为契约：

- 扫描 ``<threads_dir>/*.jsonl``，对每个文件读首行 → 解析 ``ThreadMetadata`` → ``directory.upsert_metadata``
- ``dry_run=True`` 只统计，不写 directory
- 首行损坏 / 不可解析 → 计入 ``error_count`` + 发 ``rebuild_skipped_corrupt`` 事件
- IO 失败 → 同上
- 返回 ``RebuildReport(scanned, indexed, orphan, error, elapsed_ms)``
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from taifeng.conversation.models import RebuildReport, ThreadMetadata
from taifeng.conversation.protocols import ThreadDirectory
from taifeng.conversation.transcript import JsonlMessageWriter, iter_thread_files
from taifeng.loop.event import EventMsg, RebuildSkippedCorrupt

if TYPE_CHECKING:
    from taifeng.telemetry.sink import TelemetrySink


async def rebuild_index(
    writer: JsonlMessageWriter,
    directory: ThreadDirectory,
    *,
    dry_run: bool = False,
    sink: "TelemetrySink | None" = None,
) -> RebuildReport:
    """从 JSONL 主存全量重建 ThreadDirectory。

    参数：

    - ``writer``: ``JsonlMessageWriter`` 实例（需要其 ``threads_dir`` 访问文件系统）
    - ``directory``: 目标 ThreadDirectory；``dry_run=False`` 时被调用 ``upsert_metadata``
    - ``dry_run``: True 仅扫描计数，不修改 directory
    - ``sink``: 可选，发 ``rebuild_skipped_corrupt`` 事件
    """
    started = time.perf_counter()
    scanned = 0
    indexed = 0
    orphan = 0
    errors = 0

    for path in iter_thread_files(writer.threads_dir):
        scanned += 1
        try:
            with path.open("r", encoding="utf-8") as f:
                first_line = f.readline().strip()
        except OSError as e:
            errors += 1
            await _emit_corrupt(sink, str(path), e)
            continue

        if not first_line:
            errors += 1
            await _emit_corrupt(sink, str(path), ValueError("empty file"))
            continue

        try:
            data = json.loads(first_line)
        except json.JSONDecodeError as e:
            errors += 1
            await _emit_corrupt(sink, str(path), e)
            continue

        if not (isinstance(data, dict) and data.get("__meta__")):
            errors += 1
            await _emit_corrupt(sink, str(path), ValueError("first line is not __meta__"))
            continue

        try:
            meta = ThreadMetadata(
                thread_id=data["thread_id"],
                created_at=float(data["created_at"]),
                updated_at=float(data["updated_at"]),
                entry_skill_id=data["entry_skill_id"],
                source=data.get("source", "user"),
                tags=tuple(data.get("tags", [])),
                extra=dict(data.get("extra", {})),
            )
        except (KeyError, ValueError, TypeError) as e:
            errors += 1
            await _emit_corrupt(sink, str(path), e)
            continue

        if not dry_run:
            await directory.upsert_metadata(meta)
        indexed += 1

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return RebuildReport(
        scanned_count=scanned,
        indexed_count=indexed,
        orphan_count=orphan,
        error_count=errors,
        elapsed_ms=elapsed_ms,
    )


async def _emit_corrupt(sink: "TelemetrySink | None", path: str, cause: Exception) -> None:
    if sink is None:
        return
    try:
        await sink.handle(
            EventMsg(submission_id="*", msg=RebuildSkippedCorrupt(data={"path": path, "cause": repr(cause)}))
        )
    except Exception:
        # 事件发射失败不影响主路径
        pass


__all__ = ["rebuild_index"]
