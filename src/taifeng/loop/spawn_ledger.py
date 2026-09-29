"""分离式派发的持久化：发起与终态各自落在哪里。

两种模式写的地方不同，句柄表、取消、K1、事件这些运行态逻辑不关心这一点：

| | 非审计 | 审计（ADR 0098） |
| --- | --- | --- |
| 发起 | 建子 thread、落种子、父 thread 落 ``spawn`` 锚 | ``spawn_started`` 批次（含子 thread 的创建与种子） |
| 终态 | 子 thread 落 ``spawn_settled`` 锚 | ``spawn_settled`` + ``thread_terminal`` 记录 |

审计模式不往任何 thread 写锚点条目，理由见 ``audit_spawn``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import spawn_item, spawn_settled_item, user_message
from taifeng.loop.audit_spawn import open_audited_spawn, settle_audited_spawn

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.engine import AgentEngine
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
    anchor = spawn_item(
        handle_id=handle_id, skill_id=skill_id, child_thread_id=child_thread_id,
        thread_id=engine._thread_id,  # noqa: SLF001
    )
    async with engine._lock:  # noqa: SLF001
        engine._history.append(anchor)  # noqa: SLF001
    await engine._store.append(anchor)  # noqa: SLF001


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


__all__ = ["OpenedSpawn", "anchor_spawn", "open_spawn", "settle_spawn"]
