"""分离式派发与 join-barrier 的持久化：每件事实各自落在哪里。

两种模式写的地方不同，句柄表、取消、K1、事件这些运行态逻辑不关心这一点：

| | 非审计 | 审计（ADR 0098 / 0099） |
| --- | --- | --- |
| 发起 | 建子 thread、落种子、父 thread 落 ``spawn`` 锚 | ``spawn_started`` 批次（含子 thread 的创建与种子） |
| 终态 | 子 thread 落 ``spawn_settled`` 锚 | ``spawn_settled`` + ``thread_terminal`` 记录 |
| barrier 登记 | 父 thread 落 ``join_barrier`` 锚 | ``barrier_registered`` 记录 |
| barrier 点火 | 建聚合 thread、落种子、父 thread 落 ``join_barrier_fired`` 锚 | ``barrier_fired`` 批次 |
| 聚合 turn 结束 | 不记 | ``barrier_settled`` + ``thread_terminal`` 记录 |

审计模式不往任何 thread 写锚点条目，理由见 ``audit_spawn``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import (
    join_barrier_fired_item,
    join_barrier_item,
    spawn_item,
    spawn_settled_item,
    user_message,
)
from taifeng.loop.audit_spawn import (
    open_audited_barrier,
    open_audited_spawn,
    register_audited_barrier,
    run_audited_barrier,
    settle_audited_spawn,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence

    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.spawn_handle import JoinBarrier
    from taifeng.skill.definition import SkillDefinition


@dataclass(frozen=True, slots=True)
class OpenedSpawn:
    """一次已持久化的发起。``audit_state`` 仅审计模式有值，交给子 runner。"""

    child_thread_id: str
    seed: ResponseItem
    audit_state: AuditedSessionState | None = None


async def open_spawn(
    engine: AgentEngine,
    *,
    handle_id: str,
    target: SkillDefinition,
    args: dict[str, Any],
    reason: str,
    deadline_seconds: float | None,
) -> OpenedSpawn:
    """建子 thread 并落种子；返回后子 skill 才可以开始运行。

    种子只创建一次：runner 的 history 首项与已持久化的是同一个对象（同一个 id），
    冷恢复重建出的消息图谱才与运行时一致。
    """
    state = engine._audit_state  # noqa: SLF001
    if state is not None:
        opening = await open_audited_spawn(
            state, handle_id=handle_id, target=target, arguments=args, reason=reason,
            deadline_seconds=deadline_seconds,
        )
        return OpenedSpawn(
            opening.child_state.thread_id, opening.seed, opening.child_state,
        )
    child_thread_id = await engine._store.create_thread(  # noqa: SLF001
        cwd=None,
        entry_skill_id=target.id,
        source=f"spawn:{engine._entry_skill.id}",  # noqa: SLF001
        extra={
            "parent_thread_id": engine._thread_id,  # noqa: SLF001
            "spawn_handle_id": handle_id,
            "reason": reason,
        },
    )
    seed = user_message(json.dumps(args, ensure_ascii=False), thread_id=child_thread_id)
    await engine._store.append(seed)  # noqa: SLF001
    return OpenedSpawn(child_thread_id, seed)


async def anchor_spawn(
    engine: AgentEngine, *, handle_id: str, skill_id: str, child_thread_id: str,
) -> None:
    """父 thread 落 ``spawn`` 锚（冷恢复据此重建句柄表）；审计模式不写。"""
    if engine._audit_state is not None:  # noqa: SLF001
        return
    await _anchor(engine, spawn_item(
        handle_id=handle_id, skill_id=skill_id, child_thread_id=child_thread_id,
        thread_id=engine._thread_id,  # noqa: SLF001
    ))


async def settle_spawn(
    engine: AgentEngine,
    *,
    handle_id: str,
    child_thread_id: str,
    status: str,
    result: str | None,
    end_reason: str,
) -> None:
    """终态持久化，先于终态事件。"""
    state = engine._audit_state  # noqa: SLF001
    if state is not None:
        await settle_audited_spawn(
            state, handle_id=handle_id, child_thread_id=child_thread_id,
            status=status, end_reason=end_reason, result=result,
        )
        return
    await engine._store.append(spawn_settled_item(  # noqa: SLF001
        handle_id=handle_id, status=status, result=result, thread_id=child_thread_id,
    ))


async def _anchor(engine: AgentEngine, item: ResponseItem) -> None:
    """父 thread 落一条锚（hot history + store）。"""
    async with engine._lock:  # noqa: SLF001
        engine._history.append(item)  # noqa: SLF001
    await engine._store.append(item)  # noqa: SLF001


async def register_barrier(engine: AgentEngine, barrier: JoinBarrier) -> None:
    """登记持久化，先于登记事件。"""
    state = engine._audit_state  # noqa: SLF001
    if state is not None:
        await register_audited_barrier(state, barrier)
        return
    await _anchor(engine, join_barrier_item(
        barrier_id=barrier.barrier_id,
        handle_ids=list(barrier.handle_ids),
        then_skill_id=barrier.then_skill_id,
        then_args_template=barrier.then_args_template,
        thread_id=engine._thread_id,  # noqa: SLF001
    ))


async def open_barrier(
    engine: AgentEngine,
    *,
    barrier: JoinBarrier,
    target: SkillDefinition,
    args: dict[str, Any],
    members: Sequence[tuple[str, str]],
) -> OpenedSpawn:
    """建聚合 thread 并落种子；返回后聚合 turn 才可以开始运行。"""
    state = engine._audit_state  # noqa: SLF001
    if state is not None:
        opening = await open_audited_barrier(
            state, barrier=barrier, target=target, arguments=args, members=members,
        )
        return OpenedSpawn(
            opening.child_state.thread_id, opening.seed, opening.child_state,
        )
    then_thread_id = await engine._store.create_thread(  # noqa: SLF001
        cwd=None,
        entry_skill_id=barrier.then_skill_id,
        source=f"join_barrier:{barrier.barrier_id}",
        extra={
            "parent_thread_id": engine._thread_id,  # noqa: SLF001
            "barrier_id": barrier.barrier_id,
        },
    )
    seed = user_message(json.dumps(args, ensure_ascii=False), thread_id=then_thread_id)
    await engine._store.append(seed)  # noqa: SLF001
    return OpenedSpawn(then_thread_id, seed)


def barrier_run(
    engine: AgentEngine, *, barrier_id: str, opened: OpenedSpawn, runner: Any,
) -> Coroutine[Any, Any, Any]:
    """聚合 turn 的协程；审计模式下跑完要落它的终态。"""
    state = engine._audit_state  # noqa: SLF001
    if state is None:
        return runner.run()  # type: ignore[no-any-return]
    return run_audited_barrier(
        state, barrier_id=barrier_id, then_thread_id=opened.child_thread_id, runner=runner,
    )


async def anchor_barrier_fired(
    engine: AgentEngine, *, barrier_id: str, then_thread_id: str,
) -> None:
    """父 thread 落 ``join_barrier_fired`` 锚（冷恢复的幂等依据）；审计模式不写。"""
    if engine._audit_state is not None:  # noqa: SLF001
        return
    await _anchor(engine, join_barrier_fired_item(
        barrier_id=barrier_id, then_thread_id=then_thread_id,
        thread_id=engine._thread_id,  # noqa: SLF001
    ))


__all__ = [
    "OpenedSpawn",
    "anchor_barrier_fired",
    "anchor_spawn",
    "barrier_run",
    "open_barrier",
    "open_spawn",
    "register_barrier",
    "settle_spawn",
]
