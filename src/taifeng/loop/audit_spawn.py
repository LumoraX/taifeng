"""审计 Session 里分离式派发与 join-barrier 的落账（ADR 0098 / 0099）。

非审计模式下这些事实记在对话项里：父 thread 的 ``spawn`` / ``join_barrier`` /
``join_barrier_fired`` 锚、子 thread 的 ``spawn_settled`` 锚，冷恢复时扫这些条目重建运行态。
审计模式下这些事实记在 Journal 的记录里，不往任何 thread 写锚点条目——

- 子 skill 在后台运行，它结束的时刻父 thread 上可能正有 turn 在写。往父 thread 追加条目
  会让两个写者交错，投影顺序就乱了；
- 句柄表与 barrier 表是运行态，不是对话内容。记录本身已经是完整的事实。

写进 thread 的对话项只有新 thread 的种子消息，与发起 / 点火记录同批提交。

接管时运行态由记录重建（``detached_from_journal``），经 ``remember_detached`` 交给新的
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
    PayloadModel,
    StableErrorV1,
    ThreadBoundV1,
    ThreadCreatedV1,
    ThreadTerminalV1,
    conversation_item_record,
    record_id,
)
from taifeng.conversation.journal.spawn_records import (
    BARRIER_FIRED_RECORD_TYPE,
    BARRIER_REGISTERED_RECORD_TYPE,
    BARRIER_SETTLED_RECORD_TYPE,
    SPAWN_SETTLED_RECORD_TYPE,
    SPAWN_STARTED_RECORD_TYPE,
    BarrierFiredV1,
    BarrierMemberV1,
    BarrierRegisteredV1,
    BarrierSettledV1,
    SpawnSettledV1,
    SpawnStartedV1,
)
from taifeng.conversation.models import user_message
from taifeng.loop.audit_bootstrap import AuditedSessionState
from taifeng.loop.audit_skill import _stable_definition
from taifeng.loop.spawn import SpawnRejectedError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.spawn_handle import JoinBarrier
    from taifeng.skill.definition import SkillDefinition

_ACTOR = ActorRef(kind="system", source="spawn")


@dataclass(frozen=True, slots=True)
class SpawnOpening:
    """一次已落账的发起 / 点火：新 thread 的审计状态与种子消息。"""

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


@dataclass(frozen=True, slots=True)
class JournaledBarrier:
    """Journal 里一个 barrier 的现状（接管时重建 barrier 表用）。"""

    barrier_id: str
    handle_ids: tuple[str, ...]
    then_skill_id: str
    then_args_template: dict[str, Any] | None
    fired: bool


@dataclass(frozen=True, slots=True)
class JournaledDetached:
    """接管时从 Journal 读到的分离式运行态。"""

    spawns: tuple[JournaledSpawn, ...] = ()
    barriers: tuple[JournaledBarrier, ...] = ()


def child_thread_id_of(session_id: str, handle_id: str) -> str:
    """由句柄确定性地派生子 thread id（不含分隔符，可复现）。"""
    return f"thrs{canonical_hash(f'{session_id}:{handle_id}')[:32]}"


def barrier_thread_id_of(session_id: str, barrier_id: str) -> str:
    """由 barrier id 确定性地派生聚合 thread id。"""
    return f"thrb{canonical_hash(f'{session_id}:{barrier_id}')[:32]}"


def _factory(session_id: str, root_thread_id: str, operation_id: str) -> JournalRecordFactory:
    """派发 / barrier 记录的 factory：identity 取 root thread 与该 operation。"""
    return JournalRecordFactory(
        session_id=session_id,
        actor=_ACTOR,
        identities=JournalIdentities(session_id, root_thread_id, operation_id),
    )


def _canonical(value: object) -> bool:
    """能不能原样进 Journal。"""
    try:
        validate_json_value(value)
    except NonCanonicalValueError:
        return False
    return True


async def _open_thread(
    state: AuditedSessionState,
    *,
    operation_id: str,
    thread_id: str,
    source: str,
    target: SkillDefinition,
    arguments: dict[str, Any],
    extra: dict[str, str],
    lead: Callable[[JournalRecordFactory], JournalRecord],
) -> SpawnOpening:
    """起一个新 thread：领头记录 + 创建 + 绑定 + 种子，一个原子批次；ack 后推进投影。

    Raises:
        SessionAuditFrozenError: Session 已冻结，或写入结果不确定。
    """
    coordinator = state.coordinator
    lineage: dict[str, Any] = {"parent_thread_id": state.thread_id, **extra}
    await state.projector.bootstrap_thread(
        thread_id=thread_id, cwd=None, entry_skill_id=target.id, source=source,
        extra={
            "audit_required": True,
            "journal_session_id": coordinator.session_id,
            "journal_schema_version": 1,
            **lineage,
        },
    )
    factory = _factory(coordinator.session_id, state.thread_id, operation_id)
    first = lead(factory)
    created = factory.build(
        operation_id=operation_id,
        record_type="thread_created",
        payload=ThreadCreatedV1(
            entry_skill_id=target.id, source=source, tags=(), extra=lineage,
            parent_thread_id=state.thread_id,
        ),
        submission_id=operation_id,
        thread_id=thread_id,
        causation_id=first.record_id,
    )
    bound = factory.build(
        operation_id=operation_id,
        record_type="thread_bound",
        payload=ThreadBoundV1(session_id=coordinator.session_id, thread_id=thread_id),
        submission_id=operation_id,
        thread_id=thread_id,
        causation_id=created.record_id,
    )
    seed = user_message(json.dumps(arguments, ensure_ascii=False), thread_id=thread_id)
    seed_record = conversation_item_record(
        factory,
        operation_id=operation_id,
        item=seed,
        source_record_id=bound.record_id,
        ordinal=0,
        submission_id=operation_id,
    )
    batch = (first, created, bound, seed_record)
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
        thread_id=thread_id,
        coordinator=coordinator,
        projector=state.projector,
        max_attachment_bytes=state.max_attachment_bytes,
        max_total_attachment_bytes=state.max_total_attachment_bytes,
        root=False,
    )
    return SpawnOpening(child_state, seed, first.record_id)


def _terminal(
    factory: JournalRecordFactory,
    *,
    operation_id: str,
    thread_id: str,
    status: str,
    end_reason: str,
    cause: JournalRecord,
    correlation_id: str | None,
) -> JournalRecord:
    """分离运行的 thread 的终态。"""
    return factory.build(
        operation_id=operation_id,
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
        submission_id=operation_id,
        thread_id=thread_id,
        causation_id=cause.record_id,
        correlation_id=correlation_id,
    )


# ------------------------------------------------------------------
# 分离式派发
# ------------------------------------------------------------------


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
    await state.coordinator.ensure_effect_allowed()
    if not _canonical(arguments):
        raise SpawnRejectedError("arguments_not_canonical", skill_id=target.id)
    child_thread_id = child_thread_id_of(state.coordinator.session_id, handle_id)
    definition = _stable_definition(target)

    def lead(factory: JournalRecordFactory) -> JournalRecord:
        return factory.build(
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

    return await _open_thread(
        state, operation_id=handle_id, thread_id=child_thread_id,
        source=f"spawn:{handle_id}", target=target, arguments=arguments,
        extra={"spawn_handle_id": handle_id}, lead=lead,
    )


def _settled_pair(
    *,
    session_id: str,
    root_thread_id: str,
    operation_id: str,
    thread_id: str,
    record_type: str,
    payload: PayloadModel,
    status: str,
    end_reason: str,
    started_record_id: str,
    correlation_id: str | None,
) -> tuple[JournalRecord, JournalRecord]:
    """终态记录与该 thread 的 ``thread_terminal``。"""
    factory = _factory(session_id, root_thread_id, operation_id)
    settled = factory.build(
        operation_id=operation_id,
        record_type=record_type,
        payload=payload,
        submission_id=operation_id,
        thread_id=root_thread_id,
        causation_id=started_record_id,
        correlation_id=correlation_id,
    )
    terminal = _terminal(
        factory, operation_id=operation_id, thread_id=thread_id, status=status,
        end_reason=end_reason, cause=settled, correlation_id=correlation_id,
    )
    return settled, terminal


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
    started_record_id = record_id(handle_id, SPAWN_STARTED_RECORD_TYPE)
    return _settled_pair(
        session_id=session_id, root_thread_id=root_thread_id, operation_id=handle_id,
        thread_id=child_thread_id, record_type=SPAWN_SETTLED_RECORD_TYPE,
        payload=SpawnSettledV1(
            handle_id=handle_id,
            child_thread_id=child_thread_id,
            started_record_id=started_record_id,
            status=status,  # type: ignore[arg-type]
            end_reason=end_reason,
            result=result,
        ),
        status=status, end_reason=end_reason, started_record_id=started_record_id,
        correlation_id=correlation_id,
    )


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
# join-barrier
# ------------------------------------------------------------------


async def register_audited_barrier(state: AuditedSessionState, barrier: JoinBarrier) -> None:
    """登记落账。

    Raises:
        ValueError: 自定义输入无法规范化（``then_args_not_canonical``，什么都没有写）。
    """
    await state.coordinator.ensure_effect_allowed()
    if barrier.then_args_template is not None and not _canonical(barrier.then_args_template):
        raise ValueError(f"then_args_not_canonical: {barrier.barrier_id}")
    factory = _factory(state.coordinator.session_id, state.thread_id, barrier.barrier_id)
    await state.coordinator.append(factory.build(
        operation_id=barrier.barrier_id,
        record_type=BARRIER_REGISTERED_RECORD_TYPE,
        payload=BarrierRegisteredV1(
            barrier_id=barrier.barrier_id,
            handle_ids=barrier.handle_ids,
            then_skill_id=barrier.then_skill_id,
            then_args_template=barrier.then_args_template,
        ),
        submission_id=barrier.barrier_id,
        thread_id=state.thread_id,
    ))


async def open_audited_barrier(
    state: AuditedSessionState,
    *,
    barrier: JoinBarrier,
    target: SkillDefinition,
    arguments: dict[str, Any],
    members: Sequence[tuple[str, str]],
) -> SpawnOpening:
    """点火落账：记录 ack 之后聚合 turn 才可以开始运行。

    ``members`` 是点火时各成员的 (句柄 id, 终态)。
    """
    await state.coordinator.ensure_effect_allowed()
    barrier_id = barrier.barrier_id
    then_thread_id = barrier_thread_id_of(state.coordinator.session_id, barrier_id)
    definition = _stable_definition(target)

    def lead(factory: JournalRecordFactory) -> JournalRecord:
        return factory.build(
            operation_id=barrier_id,
            record_type=BARRIER_FIRED_RECORD_TYPE,
            payload=BarrierFiredV1(
                barrier_id=barrier_id,
                registered_record_id=record_id(barrier_id, BARRIER_REGISTERED_RECORD_TYPE),
                then_skill_id=target.id,
                then_thread_id=then_thread_id,
                members=tuple(
                    BarrierMemberV1(handle_id=handle_id, status=status)  # type: ignore[arg-type]
                    for handle_id, status in members
                ),
                arguments=arguments,
                definition_hash=canonical_hash(definition),
                body_hash=canonical_hash(target.body),
            ),
            submission_id=barrier_id,
            thread_id=state.thread_id,
            causation_id=record_id(barrier_id, BARRIER_REGISTERED_RECORD_TYPE),
        )

    return await _open_thread(
        state, operation_id=barrier_id, thread_id=then_thread_id,
        source=f"join_barrier:{barrier_id}", target=target, arguments=arguments,
        extra={"barrier_id": barrier_id}, lead=lead,
    )


def barrier_settled_records(
    *,
    session_id: str,
    root_thread_id: str,
    barrier_id: str,
    then_thread_id: str,
    status: str,
    end_reason: str,
    result: str | None,
    correlation_id: str | None = None,
) -> tuple[JournalRecord, JournalRecord]:
    """聚合 turn 的终态：``barrier_settled`` 与聚合 thread 的 ``thread_terminal``。"""
    fired_record_id = record_id(barrier_id, BARRIER_FIRED_RECORD_TYPE)
    return _settled_pair(
        session_id=session_id, root_thread_id=root_thread_id, operation_id=barrier_id,
        thread_id=then_thread_id, record_type=BARRIER_SETTLED_RECORD_TYPE,
        payload=BarrierSettledV1(
            barrier_id=barrier_id,
            then_thread_id=then_thread_id,
            fired_record_id=fired_record_id,
            status=status,  # type: ignore[arg-type]
            end_reason=end_reason,
            result=result,
        ),
        status=status, end_reason=end_reason, started_record_id=fired_record_id,
        correlation_id=correlation_id,
    )


async def run_audited_barrier(
    state: AuditedSessionState, *, barrier_id: str, then_thread_id: str, runner: Any,
) -> None:
    """跑聚合 turn 并落它的终态（聚合 turn 不登记为句柄，终态只在记录里）。"""
    status, end_reason, result = "error", "error", None
    try:
        outcome = await runner.run()
        end_reason = str(outcome.end_reason)
        if end_reason == "completed":
            status, result = "done", outcome.final_text
        elif end_reason == "cancelled":
            status = "cancelled"
        else:
            result = outcome.error or end_reason
    finally:
        coordinator = state.coordinator
        if coordinator.effect_gate_open:
            await coordinator.append_batch(barrier_settled_records(
                session_id=coordinator.session_id,
                root_thread_id=state.thread_id,
                barrier_id=barrier_id,
                then_thread_id=then_thread_id,
                status=status,
                end_reason=end_reason,
                result=result,
            ))


# ------------------------------------------------------------------
# 接管：由记录重建运行态
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


def barriers_from_journal(
    envelopes: Sequence[JournalEnvelope],
) -> tuple[JournaledBarrier, ...]:
    """Journal 里全部 barrier 的现状，按登记顺序。

    Raises:
        pydantic.ValidationError: barrier 记录形状违约（Journal 不可信）。
    """
    fired = {
        BarrierFiredV1.model_validate(envelope.payload).barrier_id
        for envelope in envelopes
        if envelope.record_type == BARRIER_FIRED_RECORD_TYPE
    }
    barriers: list[JournaledBarrier] = []
    for envelope in envelopes:
        if envelope.record_type != BARRIER_REGISTERED_RECORD_TYPE:
            continue
        payload = BarrierRegisteredV1.model_validate(envelope.payload)
        barriers.append(JournaledBarrier(
            barrier_id=payload.barrier_id,
            handle_ids=payload.handle_ids,
            then_skill_id=payload.then_skill_id,
            then_args_template=(
                dict(payload.then_args_template)
                if payload.then_args_template is not None else None
            ),
            fired=payload.barrier_id in fired,
        ))
    return tuple(barriers)


def detached_from_journal(envelopes: Sequence[JournalEnvelope]) -> JournaledDetached:
    """Journal 里的分离式运行态：派发与 barrier。"""
    return JournaledDetached(
        spawn_handles_from_journal(envelopes), barriers_from_journal(envelopes),
    )


# coordinator → 接管时从 Journal 读到的运行态；Engine 启动后取走
_RESUMED: WeakKeyDictionary[object, JournaledDetached] = WeakKeyDictionary()


def remember_detached(state: AuditedSessionState, detached: JournaledDetached) -> None:
    """接管时记下 Journal 里的运行态，留给新 Engine 重建句柄表与 barrier 表。"""
    _RESUMED[state.coordinator] = detached


def take_detached(state: AuditedSessionState) -> JournaledDetached:
    """取走接管时记下的运行态（只取一次）。"""
    return _RESUMED.pop(state.coordinator, JournaledDetached())


__all__ = [
    "JournaledBarrier",
    "JournaledDetached",
    "JournaledSpawn",
    "SpawnOpening",
    "barrier_settled_records",
    "barrier_thread_id_of",
    "barriers_from_journal",
    "child_thread_id_of",
    "detached_from_journal",
    "open_audited_barrier",
    "open_audited_spawn",
    "register_audited_barrier",
    "remember_detached",
    "run_audited_barrier",
    "settle_audited_spawn",
    "settled_records",
    "spawn_handles_from_journal",
    "take_detached",
]
