"""执行身份作用域与采样事实元数据（ADR 0132）。

参照 codex 的父子调用归因；差异是仅传播同 session 的观测上下文，
不扩展调度工具参数，不改变 Journal、LLM 请求或订阅过滤语义。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING, Any

from taifeng.loop.event import EventMsg, ExecutionUpdated
from taifeng.loop.execution_identity import ExecutionIdentity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from taifeng.loop.turn import TurnOutcome, TurnRunner

logger = logging.getLogger(__name__)
_ACTIVE: ContextVar[tuple[str, ExecutionIdentity] | None] = ContextVar('taifeng_execution', default=None)


def current_execution(session_id: str | None) -> ExecutionIdentity | None:
    """只读取指定 session 的当前身份，跨 session 不形成父子关系。"""
    active = _ACTIVE.get()
    return active[1] if active is not None and active[0] == session_id else None


@contextmanager
def execution_scope(
    *, session_id: str, thread_id: str, skill_id: str, model: str = 'auto',
    parent_thread_id: str | None = None, parent_known: bool = False,
) -> Iterator[ExecutionIdentity]:
    """建立任务本地作用域，异常与取消都在 finally 恢复原上下文。"""
    parent = current_execution(session_id)
    parent_id = parent_thread_id if parent_known else (parent.thread_id if parent else None)
    identity = ExecutionIdentity(
        execution_id=thread_id, attempt_id=f'run_{secrets.token_hex(12)}',
        thread_id=thread_id, skill_id=skill_id, model=model,
        parent_execution_id=parent_id, parent_thread_id=parent_id,
    )
    token = _ACTIVE.set((session_id, identity))
    try:
        yield identity
    finally:
        _ACTIVE.reset(token)


def remember_execution_parent(engine: object, child: str, parent: str) -> None:
    """只在创建子 thread 时记住逻辑父级，唤醒与续跑不重写关系。"""
    parents = getattr(engine, '_execution_parents', None)
    if isinstance(parents, dict) and child != parent:
        parents.setdefault(child, parent)


def observe_run(
    method: Callable[[TurnRunner], Awaitable[TurnOutcome]],
) -> Callable[[TurnRunner], Awaitable[TurnOutcome]]:
    """包围一次 run；不移动原有异常处理或改变返回值。"""
    @wraps(method)
    async def observed(owner: TurnRunner) -> TurnOutcome:
        """把父身份传播到子任务，并在逃逸异常时补充观察终态。"""
        parents = getattr(owner.spawn_coordinator, '_execution_parents', None)
        with execution_scope(
            parent_known=isinstance(parents, dict),
            parent_thread_id=parents.get(owner.thread_id) if isinstance(parents, dict) else None,
            session_id=owner.session_id or owner.thread_id, thread_id=owner.thread_id,
            skill_id=owner.entry_skill.id, model=owner.entry_skill.model or 'auto',
        ) as identity:
            owner._execution_identity = identity
            owner._execution_usage_complete = True
            try:
                return await method(owner)
            except asyncio.CancelledError:
                await emit_sample_observation(owner, owner._current_iteration, phase='cancelled')
                raise
            except Exception:
                await emit_sample_observation(owner, owner._current_iteration, phase='failed')
                raise
    return observed


async def emit_turn_event(owner: TurnRunner, msg: Any) -> None:
    """保留终态计量语义，并在统一出口显式附带所属 runner 身份。"""
    if msg.kind in {'turn_completed', 'turn_failed', 'turn_suspended'}:
        msg.data['usage_complete'] = (
            owner._sample.samples_started == owner._sample.samples_metered
            and owner._execution_usage_complete
        )
    try:
        await owner.emit(EventMsg(
            submission_id=owner.submission_id, msg=msg, execution=owner._execution_identity,
        ))
    except Exception:
        logger.exception('emit failed')


def _valid_count(value: Any) -> bool:
    """只有 provider 明确提供的非负整数才是有效 token 计数。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def emit_sample_observation(
    owner: TurnRunner, iteration: int, *, usage: dict[str, Any] | None = None,
    phase: str = 'model',
) -> None:
    """发出小型元数据快照；没有 provider 输入计数时保留估算标记。"""
    if owner._execution_identity is None:
        return  # 预热/裸采样未进入执行作用域，不伪造身份。
    try:
        measured = usage is not None and _valid_count(usage.get('input_tokens'))
        if usage is not None and not all(_valid_count(usage.get(k)) for k in ('input_tokens', 'output_tokens')):
            owner._execution_usage_complete = False
        tokens = int(usage['input_tokens']) if measured and usage is not None else owner._history_token_estimate()
        counts = owner.total_usage.model_dump(mode='json')
        counts['total_tokens'] = counts.get('total_tokens', 0) or counts['input_tokens'] + counts['output_tokens']
        await emit_turn_event(owner, ExecutionUpdated(data={
            'phase': phase, 'iteration': iteration, 'usage': counts,
            'usage_complete': owner._execution_usage_complete and owner._sample.samples_started == owner._sample.samples_metered,
            'context_tokens': tokens, 'context_window': owner.effective_budget.context_window,
            'context_measured': measured,
        }))
    except Exception:
        logger.exception('execution observation failed')
