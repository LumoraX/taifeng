"""审计 Session 里分离式派发的落账（ADR 0098）。

非审计模式下 spawn 的事实记在对话项里：父 thread 的 ``spawn`` 锚、子 thread 的
``spawn_settled`` 锚，冷恢复时扫这些条目重建句柄表。审计模式下这些事实记在 Journal 的
记录里，不往任何 thread 写锚点条目——

- 子 skill 在后台运行，它结束的时刻父 thread 上可能正有 turn 在写。往父 thread 追加条目
  会让两个写者交错，投影顺序就乱了；
- 句柄表是运行态，不是对话内容。记录本身已经是完整的事实。

写进 thread 的对话项只有一条：子 thread 的种子消息，与 ``spawn_started`` 同批提交。

接管时句柄表由记录重建（``spawn_handles_from_journal``），经 ``remember_spawns`` 交给新的
Engine。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from taifeng.conversation.journal.canonical import canonical_hash, validate_json_value
from taifeng.conversation.journal.errors import NonCanonicalValueError
from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    StableErrorV1,
    ThreadBoundV1,
    ThreadCreatedV1,
    ThreadTerminalV1,
    conversation_item_record,
    record_id,
)
from taifeng.conversation.journal.spawn_records import (
    SPAWN_SETTLED_RECORD_TYPE,
    SPAWN_STARTED_RECORD_TYPE,
    SpawnSettledV1,
    SpawnStartedV1,
)
from taifeng.conversation.models import user_message
from taifeng.loop.audit_bootstrap import AuditedSessionState
from taifeng.loop.audit_skill import _stable_definition
from taifeng.loop.spawn import SpawnRejectedError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord
    from taifeng.conversation.models import ResponseItem
    from taifeng.skill.definition import SkillDefinition

_ACTOR = ActorRef(kind="system", source="spawn")


@dataclass(frozen=True, slots=True)
class SpawnOpening:
    """一次已落账的发起：子 thread 的审计状态与种子消息。"""

    child_state: AuditedSessionState
    seed: ResponseItem
    started_record_id: str


@dataclass(frozen=True, slots=True)
class JournaledSpawn:
    """Journal 里一次派发的现状（接管时重建句柄表用）。

    ``status`` 为 None 表示记录里没有终态：进程死的时候它还在运行。
    """

    handle_id: str
    skill_id: str
    child_thread_id: str
    started: JournalEnvelope
    status: str | None
    result: str | None


def child_thread_id_of(session_id: str, handle_id: str) -> str:
    """由句柄确定性地派生子 thread id（不含分隔符，可复现）。"""
    return f"thrs{canonical_hash(f'{session_id}:{handle_id}')[:32]}"


def _factory(state: AuditedSessionState, handle_id: str) -> JournalRecordFactory:
    """派发记录的 factory：identity 取 root thread 与句柄。"""
    session_id = state.coordinator.session_id
    return JournalRecordFactory(
        session_id=session_id,
        actor=_ACTOR,
        identities=JournalIdentities(session_id, state.thread_id, handle_id),
    )


def _started_batch(
    state: AuditedSessionState,
    *,
    handle_id: str,
    child_thread_id: str,
    target: SkillDefinition,
    arguments: dict[str, Any],
    reason: str,
    deadline_seconds: float | None,
    seed: ResponseItem,
) -> tuple[JournalRecord, ...]:
    """``spawn_started`` + 子 thread 的创建、绑定与种子，一个原子批次。"""
    factory = _factory(state, handle_id)
    definition = _stable_definition(target)
    started = factory.build(
        operation_id=handle_id,
        record_type=SPAWN_STARTED_RECORD_TYPE,
        payload=SpawnStartedV1(
            handle_id=handle_id,
            skill_id=target.id,
            child_thread_id=child_thread_id,
            parent_thread_id=state.thread_id,
            reason=reason,
            arguments=arguments,
            definition_hash=canonical_hash(definition),
            body_hash=canonical_hash(target.body),
            deadline_seconds=deadline_seconds,
        ),
        submission_id=handle_id,
        thread_id=state.thread_id,
    )
    created = factory.build(
        operation_id=handle_id,
        record_type="thread_created",
        payload=ThreadCreatedV1(
            entry_skill_id=target.id,
            source=f"spawn:{handle_id}",
            tags=(),
            extra={"parent_thread_id": state.thread_id, "spawn_handle_id": handle_id},
            parent_thread_id=state.thread_id,
        ),
        submission_id=handle_id,
        thread_id=child_thread_id,
        causation_id=started.record_id,
    )
    bound = factory.build(
        operation_id=handle_id,
        record_type="thread_bound",
        payload=ThreadBoundV1(
            session_id=state.coordinator.session_id, thread_id=child_thread_id,
        ),
        submission_id=handle_id,
        thread_id=child_thread_id,
        causation_id=created.record_id,
    )
    seed_record = conversation_item_record(
        factory,
        operation_id=handle_id,
        item=seed,
        source_record_id=bound.record_id,
        ordinal=0,
        submission_id=handle_id,
    )
    return started, created, bound, seed_record


async def open_audited_spawn(
    state: AuditedSessionState,
    *,
    handle_id: str,
    target: SkillDefinition,
    arguments: dict[str, Any],
    reason: str,
    deadline_seconds: float | None,
) -> SpawnOpening:
    """发起落账：记录 ack 之后子 skill 才可以开始运行。

    Raises:
        SpawnRejectedError: 种子输入无法规范化（``arguments_not_canonical``，什么都没有写）。
        SessionAuditFrozenError: Session 已冻结，或写入结果不确定。
    """
    coordinator = state.coordinator
    await coordinator.ensure_effect_allowed()
    try:
        validate_json_value(arguments)
    except NonCanonicalValueError:
        raise SpawnRejectedError("arguments_not_canonical", skill_id=target.id) from None
    child_thread_id = child_thread_id_of(coordinator.session_id, handle_id)
    await state.projector.bootstrap_thread(
        thread_id=child_thread_id,
        cwd=None,
        entry_skill_id=target.id,
        source=f"spawn:{handle_id}",
        extra={
            "audit_required": True,
            "journal_session_id": coordinator.session_id,
            "journal_schema_version": 1,
            "parent_thread_id": state.thread_id,
            "spawn_handle_id": handle_id,
        },
    )
    seed = user_message(json.dumps(arguments, ensure_ascii=False), thread_id=child_thread_id)
    batch = _started_batch(
        state, handle_id=handle_id, child_thread_id=child_thread_id, target=target,
        arguments=arguments, reason=reason, deadline_seconds=deadline_seconds, seed=seed,
    )
    ack = await coordinator.append_batch(batch)
    envelopes = await coordinator.load_acknowledged(ack, batch)
    seeds = tuple(e for e in envelopes if e.record_type == "conversation_item")
    try:
        projection = await state.projector.project(seeds, ack)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        raise coordinator.freeze(error) from None
    coordinator.update_projection(projection)
    child_state = AuditedSessionState(
        thread_id=child_thread_id,
        coordinator=coordinator,
        projector=state.projector,
        max_attachment_bytes=state.max_attachment_bytes,
        max_total_attachment_bytes=state.max_total_attachment_bytes,
        root=False,
    )
    return SpawnOpening(child_state, seed, batch[0].record_id)


def settled_records(
    *,
    session_id: str,
    root_thread_id: str,
    handle_id: str,
    child_thread_id: str,
    status: str,
    end_reason: str,
    result: str | None,
    correlation_id: str | None = None,
) -> tuple[JournalRecord, JournalRecord]:
    """一次派发的终态：``spawn_settled`` 与子 thread 的 ``thread_terminal``。"""
    factory = JournalRecordFactory(
        session_id=session_id,
        actor=_ACTOR,
        identities=JournalIdentities(session_id, root_thread_id, handle_id),
    )
    started_record_id = record_id(handle_id, SPAWN_STARTED_RECORD_TYPE)
    settled = factory.build(
        operation_id=handle_id,
        record_type=SPAWN_SETTLED_RECORD_TYPE,
        payload=SpawnSettledV1(
            handle_id=handle_id,
            child_thread_id=child_thread_id,
            started_record_id=started_record_id,
            status=status,  # type: ignore[arg-type]
            end_reason=end_reason,
            result=result,
        ),
        submission_id=handle_id,
        thread_id=root_thread_id,
        causation_id=started_record_id,
        correlation_id=correlation_id,
    )
    terminal = factory.build(
        operation_id=handle_id,
        record_type="thread_terminal",
        payload=ThreadTerminalV1(
            status=status,
            end_reason=end_reason,
            stable_error=None if status == "done" else StableErrorV1(
                code=f"spawn_{status}",
                class_name="DetachedSpawn",
                failure_class="skill_error",
                retryable=status == "cancelled",
            ),
        ),
        submission_id=handle_id,
        thread_id=child_thread_id,
        causation_id=settled.record_id,
        correlation_id=correlation_id,
    )
    return settled, terminal


async def settle_audited_spawn(
    state: AuditedSessionState,
    *,
    handle_id: str,
    child_thread_id: str,
    status: str,
    end_reason: str,
    result: str | None,
) -> bool:
    """终态落账；Session 已不再接受写入时不写，返回 False。

    冻结或已封口的 Session 里，子 skill 被取消后照样会走到这里。此时终态写不进去也不该
    再尝试：接管时这次派发会被当作「进程死的时候还在运行」处置。
    """
    coordinator = state.coordinator
    if not coordinator.effect_gate_open:
        return False
    await coordinator.append_batch(settled_records(
        session_id=coordinator.session_id,
        root_thread_id=state.thread_id,
        handle_id=handle_id,
        child_thread_id=child_thread_id,
        status=status,
        end_reason=end_reason,
        result=result,
    ))
    return True


# ------------------------------------------------------------------
# 接管：由记录重建句柄表
# ------------------------------------------------------------------


def spawn_handles_from_journal(
    envelopes: Sequence[JournalEnvelope],
) -> tuple[JournaledSpawn, ...]:
    """Journal 里全部派发的现状，按发起顺序。

    Raises:
        pydantic.ValidationError: 派发记录形状违约（Journal 不可信）。
    """
    settled: dict[str, SpawnSettledV1] = {}
    started: list[tuple[JournalEnvelope, SpawnStartedV1]] = []
    for envelope in envelopes:
        if envelope.record_type == SPAWN_STARTED_RECORD_TYPE:
            started.append((envelope, SpawnStartedV1.model_validate(envelope.payload)))
        elif envelope.record_type == SPAWN_SETTLED_RECORD_TYPE:
            payload = SpawnSettledV1.model_validate(envelope.payload)
            settled[payload.handle_id] = payload
    spawns: list[JournaledSpawn] = []
    for envelope, begun in started:
        end = settled.get(begun.handle_id)
        spawns.append(JournaledSpawn(
            handle_id=begun.handle_id,
            skill_id=begun.skill_id,
            child_thread_id=begun.child_thread_id,
            started=envelope,
            status=end.status if end is not None else None,
            result=end.result if end is not None else None,
        ))
    return tuple(spawns)


# coordinator → 接管时从 Journal 读到的派发；Engine 启动后取走
_RESUMED: WeakKeyDictionary[object, tuple[JournaledSpawn, ...]] = WeakKeyDictionary()


def remember_spawns(state: AuditedSessionState, spawns: Sequence[JournaledSpawn]) -> None:
    """接管时记下 Journal 里的派发，留给新 Engine 重建句柄表。"""
    _RESUMED[state.coordinator] = tuple(spawns)


def take_spawns(state: AuditedSessionState) -> tuple[JournaledSpawn, ...]:
    """取走接管时记下的派发（只取一次）。"""
    return _RESUMED.pop(state.coordinator, ())


__all__ = [
    "JournaledSpawn",
    "SpawnOpening",
    "child_thread_id_of",
    "open_audited_spawn",
    "remember_spawns",
    "settle_audited_spawn",
    "settled_records",
    "spawn_handles_from_journal",
    "take_spawns",
]
