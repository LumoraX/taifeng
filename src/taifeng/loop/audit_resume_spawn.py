"""审计 Session resume 时收敛被中断的分离式运行（ADR 0098 / 0099）。

进程死的时候还在运行的派发，Journal 里有 ``spawn_started`` 而没有 ``spawn_settled``；
还在运行的聚合 turn，有 ``barrier_fired`` 而没有 ``barrier_settled``。它们不会被续跑：
接管的进程没有它们的运行态，重跑等于把已经做过的事再做一遍。处置是

1. 该 thread 上的工具调用按既有规则结算（与被中断的同步派发相同，ADR 0070 / 0075 / 0076）；
2. 落终态 ``spawn_settled`` / ``barrier_settled``（``cancelled``，``process_recovery``）与该 thread
   的 ``thread_terminal``。

发起方此后查询这个句柄得到 ``cancelled``，由它决定要不要重新派发。已经点火的 barrier 不会再点火。

本模块只做纯计算；记录的追加与全有或全无的语义由 ``audit_resume`` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.conversation.journal.spawn_records import (
    BARRIER_FIRED_RECORD_TYPE,
    SPAWN_RECOVERY_END_REASON,
    SPAWN_STARTED_RECORD_TYPE,
    BarrierFiredV1,
    SpawnStartedV1,
)
from taifeng.loop.audit_spawn import barrier_settled_records, settled_records

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord


@dataclass(frozen=True, slots=True)
class InterruptedSpawn:
    """一次没有终态的分离式运行：派发，或 barrier 的聚合 turn。"""

    started: JournalEnvelope
    payload: SpawnStartedV1 | BarrierFiredV1

    @property
    def child_thread_id(self) -> str:
        """它运行所在的 thread。"""
        if isinstance(self.payload, SpawnStartedV1):
            return self.payload.child_thread_id
        return self.payload.then_thread_id


def interrupted_spawns(
    envelopes: Sequence[JournalEnvelope], pending: Collection[str],
) -> tuple[InterruptedSpawn, ...]:
    """未结算 record 里的分离式运行，按发起顺序。

    Raises:
        pydantic.ValidationError: 记录形状违约（Journal 不可信）。
    """
    found: list[InterruptedSpawn] = []
    for envelope in envelopes:
        if envelope.record_id not in pending:
            continue
        if envelope.record_type == SPAWN_STARTED_RECORD_TYPE:
            found.append(InterruptedSpawn(
                envelope, SpawnStartedV1.model_validate(envelope.payload)))
        elif envelope.record_type == BARRIER_FIRED_RECORD_TYPE:
            found.append(InterruptedSpawn(
                envelope, BarrierFiredV1.model_validate(envelope.payload)))
    return tuple(found)


def interrupted_spawn_records(
    spawn: InterruptedSpawn, *, session_id: str, recovery_operation_id: str,
) -> tuple[JournalRecord, JournalRecord]:
    """被中断的分离式运行的终态记录。"""
    assert spawn.started.thread_id is not None
    if isinstance(spawn.payload, BarrierFiredV1):
        return barrier_settled_records(
            session_id=session_id,
            root_thread_id=spawn.started.thread_id,
            barrier_id=spawn.payload.barrier_id,
            then_thread_id=spawn.child_thread_id,
            status="cancelled",
            end_reason=SPAWN_RECOVERY_END_REASON,
            result=None,
            correlation_id=recovery_operation_id,
        )
    return settled_records(
        session_id=session_id,
        root_thread_id=spawn.started.thread_id,
        handle_id=spawn.payload.handle_id,
        child_thread_id=spawn.child_thread_id,
        status="cancelled",
        end_reason=SPAWN_RECOVERY_END_REASON,
        result=None,
        correlation_id=recovery_operation_id,
    )


__all__ = ["InterruptedSpawn", "interrupted_spawn_records", "interrupted_spawns"]
