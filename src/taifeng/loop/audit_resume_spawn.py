"""审计 Session resume 时收敛被中断的分离式派发（ADR 0098）。

进程死的时候还在运行的派发，Journal 里有 ``spawn_started`` 而没有 ``spawn_settled``。
子 skill 不会被续跑：接管的进程没有它的运行态，重跑等于把已经做过的事再做一遍。处置是

1. 子 thread 上的工具调用按既有规则结算（与被中断的同步派发相同，ADR 0070 / 0075 / 0076）；
2. 这次派发落终态 ``spawn_settled(cancelled, process_recovery)`` 与子 thread 的
   ``thread_terminal``。

发起方此后查询这个句柄得到 ``cancelled``，由它决定要不要重新派发。

本模块只做纯计算；记录的追加与全有或全无的语义由 ``audit_resume`` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.conversation.journal.spawn_records import (
    SPAWN_RECOVERY_END_REASON,
    SPAWN_STARTED_RECORD_TYPE,
    SpawnStartedV1,
)
from taifeng.loop.audit_spawn import settled_records

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord


@dataclass(frozen=True, slots=True)
class InterruptedSpawn:
    """一次没有终态的派发。"""

    started: JournalEnvelope
    payload: SpawnStartedV1

    @property
    def child_thread_id(self) -> str:
        """子 skill 运行所在的 thread。"""
        return self.payload.child_thread_id


def interrupted_spawns(
    envelopes: Sequence[JournalEnvelope], pending: Collection[str],
) -> tuple[InterruptedSpawn, ...]:
    """未结算 record 里的派发，按发起顺序。

    Raises:
        pydantic.ValidationError: 派发记录形状违约（Journal 不可信）。
    """
    return tuple(
        InterruptedSpawn(envelope, SpawnStartedV1.model_validate(envelope.payload))
        for envelope in envelopes
        if envelope.record_type == SPAWN_STARTED_RECORD_TYPE and envelope.record_id in pending
    )


def interrupted_spawn_records(
    spawn: InterruptedSpawn, *, session_id: str, recovery_operation_id: str,
) -> tuple[JournalRecord, JournalRecord]:
    """被中断的派发的终态记录。"""
    assert spawn.started.thread_id is not None
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
