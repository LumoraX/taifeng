"""旧 transcript 导入 Journal（session-journal，ADR 0104）。

审计模式之前的 thread 只有一份 ``ResponseItem`` JSONL。它不能直接被审计 Session 接管：没有
Journal、没有 marker。导入把它变成一个可以接管的审计 Session：

```text
初始化批次（root thread = 旧 thread id）
legacy_import                         旧文件的摘要、行数、逐条来源
conversation_item × N                 旧对话项，原样、保序，metadata 记来源行号
```

导入后的历史标为 ``legacy_unverified``：Journal 保证的是「从导入这一刻起」的完整性，导入之前
发生过什么只能相信旧文件。旧文件原样搬到 ``<threads_dir>/legacy/`` 留档，投影按 Journal 重建。

旧文件里 Journal 不认识的条目（spawn 锚等运行态锚点）不进 Journal，记在 ``skipped`` 里。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.conversation.journal.materialization import safe_thread_path
from taifeng.conversation.journal.models import (
    ActorRef,
    NonEmptyStr,
    RootThreadDescriptor,
    SessionDescriptor,
)
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.journal.records import (
    ConversationItemV1 as _ConversationItemV1,
)
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    PayloadModel,
    UnsupportedConversationItemError,
    conversation_item_record,
    deserialize_response_item,
)
from taifeng.conversation.jsonl_atomic import read_state
from taifeng.conversation.models import ResponseItem  # noqa: TC001  # 运行期需要构造

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
    from taifeng.conversation.journal.models import JournalRecord
    from taifeng.conversation.transcript import JsonlMessageStore

LEGACY_IMPORT_RECORD_TYPE = "legacy_import"
LEGACY_IMPORT_OPERATION_ID = "legacy_import"
LEGACY_SOURCE_LINE_KEY = "legacy_source_line"
"""导入的对话项 metadata 里的键：它在旧文件里的行号（从 1 起）。"""
LEGACY_ARCHIVE_DIR = "legacy"


class LegacyImportV1(PayloadModel):
    """一次旧 transcript 导入的事实。

    Attributes:
        thread_id: 被导入的 thread。
        source_file: 旧文件名（相对 threads 目录）。
        file_sha256: 旧文件完整内容的摘要。
        line_count: 旧文件的行数（含首行元数据）。
        imported_count: 进入 Journal 的对话项数。
        skipped: Journal 不认识、没有导入的条目：``[行号, kind]``。
        history_status: 恒为 ``legacy_unverified``。
    """

    thread_id: NonEmptyStr
    source_file: NonEmptyStr
    file_sha256: NonEmptyStr
    line_count: int
    imported_count: int
    skipped: tuple[tuple[int, str], ...] = ()
    history_status: str = "legacy_unverified"


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    """导入结果。"""

    session_id: str
    thread_id: str
    imported_count: int
    skipped: tuple[tuple[int, str], ...]
    archived_to: Path


class LegacyImportError(RuntimeError):
    """旧 transcript 无法导入；``code`` 稳定可断言。"""

    def __init__(self, code: str, thread_id: str) -> None:
        """记录稳定 code 与 thread。"""
        super().__init__(f"{code}: thread={thread_id}")
        self.code = code
        self.thread_id = thread_id


def _read_lines(path: Path) -> tuple[bytes, list[str]]:
    """读旧文件的全部字节与各行。"""
    raw = path.read_bytes()
    return raw, raw.decode("utf-8").splitlines()


def _parse(
    path: Path, lines: list[str], thread_id: str,
) -> tuple[dict[str, Any], list[tuple[int, ResponseItem]]]:
    """首行元数据 + 各条对话项（带行号）。

    对话项集合与 ``load_history`` 一致（只含 bare 条目与完整提交的原子 batch）；损坏的行让
    导入失败而不是静默跳过——旧文件是这段历史唯一的依据。
    """
    if not lines:
        raise LegacyImportError("legacy_transcript_empty", thread_id)
    meta = json.loads(lines[0])
    if not isinstance(meta, dict) or meta.get("__meta__") is not True:
        raise LegacyImportError("legacy_transcript_meta_missing", thread_id)
    if meta.get("thread_id") != thread_id:
        raise LegacyImportError("legacy_transcript_thread_mismatch", thread_id)
    extra = meta.get("extra")
    if isinstance(extra, dict) and extra.get("audit_required") is True:
        raise LegacyImportError("legacy_transcript_already_audited", thread_id)
    state = read_state(path)
    if state.corrupt:
        raise LegacyImportError("legacy_transcript_line_invalid", thread_id)
    line_of: dict[str, int] = {}
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        data = json.loads(line)
        if isinstance(data, dict) and isinstance(data.get("id"), str) and "kind" in data:
            line_of.setdefault(data["id"], number)
    return meta, [(line_of.get(item.id, 0), item) for item in state.items]


def _records(
    *, session_id: str, thread_id: str, meta: dict[str, Any], raw: bytes, lines: list[str],
    items: list[tuple[int, ResponseItem]], source_file: str,
) -> tuple[tuple[JournalRecord, ...], list[ResponseItem], tuple[tuple[int, str], ...]]:
    """导入批次：``legacy_import`` 领头，其后是能进 Journal 的对话项。"""
    factory = JournalRecordFactory(
        session_id=session_id,
        actor=ActorRef(kind="system", source="legacy_import"),
        identities=JournalIdentities(session_id, thread_id, LEGACY_IMPORT_OPERATION_ID),
    )
    imported: list[JournalRecord] = []
    kept: list[ResponseItem] = []
    skipped: list[tuple[int, str]] = []
    for number, item in items:
        tagged = item.model_copy(update={
            "thread_id": thread_id,
            "metadata": {**item.metadata, LEGACY_SOURCE_LINE_KEY: number},
        })
        try:
            record = conversation_item_record(
                factory,
                operation_id=LEGACY_IMPORT_OPERATION_ID,
                item=tagged,
                source_record_id=f"{LEGACY_IMPORT_OPERATION_ID}:{LEGACY_IMPORT_RECORD_TYPE}:none:0",
                ordinal=len(imported) + 1,
            )
        except (UnsupportedConversationItemError, ValueError):
            skipped.append((number, str(item.kind)))
            continue
        imported.append(record)
        kept.append(deserialize_response_item(_ConversationItemV1.model_validate(record.payload)))
    lead = factory.build(
        operation_id=LEGACY_IMPORT_OPERATION_ID,
        record_type=LEGACY_IMPORT_RECORD_TYPE,
        payload=LegacyImportV1(
            thread_id=thread_id,
            source_file=source_file,
            file_sha256=hashlib.sha256(raw).hexdigest(),
            line_count=len(lines),
            imported_count=len(imported),
            skipped=tuple(skipped),
        ),
        thread_id=thread_id,
    )
    return (lead, *imported), kept, tuple(skipped)


async def import_legacy_transcript(
    *,
    journal_core: JsonlSessionJournalCore,
    store: JsonlMessageStore,
    session_id: str,
    thread_id: str,
    writer_id: str,
    max_attachment_bytes: int,
    max_total_attachment_bytes: int,
) -> LegacyImportResult:
    """把一个旧 thread 导入为可接管的审计 Session。

    完成后 ``EnginePool.get_or_create(session_id, entry_skill_id, resume_thread_id=thread_id)``
    在审计模式下可以接管它。

    Raises:
        LegacyImportError: 旧文件缺失、损坏、已是审计投影，或 thread id 不符。
        JournalAlreadyExistsError: 该 Session id 已有 Journal。
    """
    root = store.threads_dir
    path = safe_thread_path(root, thread_id)
    if not path.is_file():
        raise LegacyImportError("legacy_transcript_missing", thread_id)
    raw, lines = await anyio.to_thread.run_sync(_read_lines, path)
    meta, items = _parse(path, lines, thread_id)
    descriptor = SessionDescriptor(
        session_id=session_id,
        creation_operation_id=f"{session_id}:create",
        writer_id=writer_id,
        root_thread=RootThreadDescriptor(
            thread_id=thread_id,
            entry_skill_id=str(meta.get("entry_skill_id") or "general"),
            source=f"legacy_import:{path.name}",
            extra={},
        ),
        config={
            "audit_required": True,
            "journal_schema_version": 1,
            "strict_mode": "session_journal_business_v1",
            "max_attachment_bytes": max_attachment_bytes,
            "max_total_attachment_bytes": max_total_attachment_bytes,
            "history_status": "legacy_unverified",
        },
    )
    created = await journal_core.create_session(descriptor)
    try:
        records, kept, skipped = _records(
            session_id=session_id, thread_id=thread_id, meta=meta, raw=raw, lines=lines,
            items=items, source_file=path.name,
        )
        ack = await journal_core.append_batch(
            records, lease=created.lease, expected_seq=created.ack.last_seq,
        )
        # 旧文件留档；投影按 Journal 重建（带 audited marker）
        archive_dir = root / LEGACY_ARCHIVE_DIR
        archive_dir.mkdir(exist_ok=True)
        archived = archive_dir / path.name
        await anyio.to_thread.run_sync(shutil.move, str(path), str(archived))
        projector = JournalConversationProjector(store)
        await projector.bootstrap_thread(
            thread_id=thread_id,
            cwd=(meta.get("extra") or {}).get("cwd") if isinstance(meta.get("extra"), dict) else None,
            entry_skill_id=descriptor.root_thread.entry_skill_id,
            source=descriptor.root_thread.source,
            extra={
                "audit_required": True,
                "journal_session_id": session_id,
                "journal_schema_version": 1,
                "history_status": "legacy_unverified",
            },
        )
        await projector.reconcile_resumed_thread(
            thread_id=thread_id,
            session_id=session_id,
            items=kept,
            first_seq=ack.first_seq + 1 if kept else None,
            last_seq=ack.last_seq,
        )
    finally:
        await journal_core.close_session(created.lease)
    return LegacyImportResult(session_id, thread_id, len(kept), skipped, archived)


__all__ = [
    "LEGACY_IMPORT_RECORD_TYPE",
    "LEGACY_SOURCE_LINE_KEY",
    "LegacyImportError",
    "LegacyImportResult",
    "LegacyImportV1",
    "import_legacy_transcript",
]
