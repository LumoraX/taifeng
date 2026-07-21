"""SessionJournal Phase 1 的不可变核心 DTO。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # Pydantic 运行期解析字段类型
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

HashHex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class JournalModel(BaseModel):
    """核心 DTO 共同约束：冻结、禁止额外字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Durability(StrEnum):
    """Journal ack 的实际持久化等级。"""

    COMMITTED = "committed"


class JournalHealth(StrEnum):
    """物理 Journal 的可执行健康状态。"""

    HEALTHY = "healthy"
    RECOVERY_REQUIRED = "recovery_required"


class ActorRef(JournalModel):
    """触发 record 的版本化 actor 身份。"""

    version: int = 1
    kind: NonEmptyStr
    source: NonEmptyStr
    principal_id: str | None = None


class RootThreadDescriptor(JournalModel):
    """Session 初始化所需的 root thread 稳定描述符。"""

    thread_id: NonEmptyStr
    entry_skill_id: NonEmptyStr
    source: NonEmptyStr = "user"
    tags: tuple[str, ...] = ()
    extra: dict[str, JsonValue] = Field(default_factory=dict)


class SessionDescriptor(JournalModel):
    """原子创建 Session 所需的全部稳定输入。"""

    schema_version: int = 1
    session_id: NonEmptyStr
    creation_operation_id: NonEmptyStr
    writer_id: NonEmptyStr
    root_thread: RootThreadDescriptor
    config: dict[str, JsonValue]


class JournalRecord(JournalModel):
    """调用方提交的 canonical record；不含 writer 分配字段。"""

    schema_version: int = 1
    session_id: NonEmptyStr
    record_id: NonEmptyStr
    record_type: NonEmptyStr
    actor: ActorRef
    payload: dict[str, JsonValue]
    operation_id: str | None = None
    attempt_id: str | None = None
    occurred_at: datetime | None = None
    submission_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    parent_record_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


class JournalEnvelope(JournalModel):
    """writer 为已提交 record 分配的序号、时间和 hash 包装。"""

    schema_version: int = 1
    session_id: NonEmptyStr
    seq: Annotated[int, Field(ge=1)]
    writer_epoch: Annotated[int, Field(ge=1)]
    record_id: NonEmptyStr
    record_type: NonEmptyStr
    actor: ActorRef
    payload: dict[str, JsonValue]
    previous_hash: HashHex
    payload_hash: HashHex
    record_hash: HashHex
    recorded_at: datetime
    operation_id: str | None = None
    attempt_id: str | None = None
    occurred_at: datetime | None = None
    submission_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    parent_record_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


class SessionLease(JournalModel):
    """Phase 1 同进程 live writer 的 fencing capability。"""

    session_id: NonEmptyStr
    writer_id: NonEmptyStr
    writer_epoch: Annotated[int, Field(ge=1)]
    lease_id: NonEmptyStr


class JournalAck(JournalModel):
    """一个 durable 单条或 batch commit 的确认。"""

    session_id: NonEmptyStr
    first_seq: Annotated[int, Field(ge=1)]
    last_seq: Annotated[int, Field(ge=1)]
    record_ids: tuple[str, ...]
    tail_hash: HashHex
    writer_epoch: Annotated[int, Field(ge=1)]
    durability: Durability


class JournalVerification(JournalModel):
    """strict scan 得到的 committed tail 与物理尾状态。"""

    session_id: NonEmptyStr
    health: JournalHealth
    committed_tail_seq: Annotated[int, Field(ge=0)]
    committed_tail_hash: HashHex
    record_count: Annotated[int, Field(ge=0)]
    physical_tail_torn: bool = False
    pending_batch_id: str | None = None


class SessionCreateResult(JournalModel):
    """原子初始化成功后返回的 live lease 与 batch ack。"""

    lease: SessionLease
    ack: JournalAck


_SYSTEM_ACTOR = ActorRef(kind="system", source="taifeng")


def build_initialization_records(
    descriptor: SessionDescriptor,
) -> tuple[JournalRecord, JournalRecord, JournalRecord]:
    """从 descriptor 确定性构造 Session/Thread/Binding 三记录。"""
    operation_id = descriptor.creation_operation_id
    thread = descriptor.root_thread
    session_record = JournalRecord(
        session_id=descriptor.session_id,
        record_id=f"{operation_id}:session_started",
        operation_id=operation_id,
        record_type="session_started",
        thread_id=thread.thread_id,
        actor=_SYSTEM_ACTOR,
        payload={
            "config": descriptor.config,
            "creation_operation_id": operation_id,
            "writer_id": descriptor.writer_id,
        },
    )
    thread_record = JournalRecord(
        session_id=descriptor.session_id,
        record_id=f"{operation_id}:thread_created",
        operation_id=operation_id,
        record_type="thread_created",
        thread_id=thread.thread_id,
        causation_id=session_record.record_id,
        actor=_SYSTEM_ACTOR,
        payload={
            "entry_skill_id": thread.entry_skill_id,
            "source": thread.source,
            "tags": list(thread.tags),
            "extra": thread.extra,
        },
    )
    binding_record = JournalRecord(
        session_id=descriptor.session_id,
        record_id=f"{operation_id}:thread_bound",
        operation_id=operation_id,
        record_type="thread_bound",
        thread_id=thread.thread_id,
        causation_id=thread_record.record_id,
        actor=_SYSTEM_ACTOR,
        payload={"session_id": descriptor.session_id, "thread_id": thread.thread_id},
    )
    return session_record, thread_record, binding_record


__all__ = [
    "ActorRef",
    "Durability",
    "JournalAck",
    "JournalEnvelope",
    "JournalHealth",
    "JournalRecord",
    "JournalVerification",
    "JsonValue",
    "RootThreadDescriptor",
    "SessionCreateResult",
    "SessionDescriptor",
    "SessionLease",
    "build_initialization_records",
]
