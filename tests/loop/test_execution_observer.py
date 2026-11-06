"""执行观察作用域：父子关系、跨会话隔离、取消和事件元数据。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from typing import TYPE_CHECKING

import anyio
import pytest

import taifeng
from tests.conftest import GUARD_TIMEOUT_SECONDS
from tests.loop.test_usage_tree_accounting import _delegating_client

if TYPE_CHECKING:
    from pathlib import Path


def _observer():
    """观察模块尚未实现时在测试中明确失败。"""
    name = 'taifeng.loop.execution_observer'
    assert importlib.util.find_spec(name) is not None, '缺少执行作用域传播'
    return importlib.import_module(name)


def test_nested_scope_and_session_isolation() -> None:
    """父级只在同session继承，退出后恢复上层作用域。"""
    observer = _observer()
    with observer.execution_scope(session_id='s', thread_id='root', skill_id='planner') as parent:
        with observer.execution_scope(session_id='s', thread_id='child', skill_id='worker') as child:
            assert child.parent_execution_id == parent.execution_id
            assert child.parent_thread_id == 'root'
            assert observer.current_execution('s') == child
        assert observer.current_execution('s') == parent
        with observer.execution_scope(session_id='other', thread_id='foreign', skill_id='worker') as other:
            assert other.parent_execution_id is None
        assert observer.current_execution('other') is None
    assert observer.current_execution('s') is None


def test_new_attempt_reuses_logical_node() -> None:
    """同一thread续跑不会伪造第二个逻辑agent，但有独立attempt。"""
    observer = _observer()
    with observer.execution_scope(session_id='s', thread_id='t', skill_id='worker') as first:
        pass
    with observer.execution_scope(session_id='s', thread_id='t', skill_id='worker') as second:
        pass
    assert first.execution_id == second.execution_id == 't'
    assert first.attempt_id != second.attempt_id


async def test_concurrent_siblings_keep_distinct_contexts() -> None:
    """两个异步子任务交错时不覆盖彼此身份。"""
    observer = _observer()
    values = []

    async def child(tid: str) -> None:
        """让出一次调度后核验作用域仍为自己。"""
        with observer.execution_scope(session_id='s', thread_id=tid, skill_id='worker') as identity:
            await anyio.sleep(0)
            assert observer.current_execution('s') == identity
            values.append(identity)

    with observer.execution_scope(session_id='s', thread_id='root', skill_id='planner'):
        async with anyio.create_task_group() as group:
            group.start_soon(child, 'left')
            group.start_soon(child, 'right')
    assert {value.execution_id for value in values} == {'left', 'right'}
    assert {value.parent_execution_id for value in values} == {'root'}


async def test_scope_is_reset_on_exception_and_cancellation() -> None:
    """异常和取消都经finally恢复，不污染后续独立执行。"""
    observer = _observer()
    with pytest.raises(ValueError):
        with observer.execution_scope(session_id='s', thread_id='t', skill_id='worker'):
            raise ValueError('expected')
    assert observer.current_execution('s') is None
    with anyio.CancelScope() as scope:
        with observer.execution_scope(session_id='s', thread_id='t', skill_id='worker'):
            scope.cancel()
            await anyio.lowlevel.checkpoint()
    assert observer.current_execution('s') is None


async def test_real_runner_events_have_child_identity(skills_dir: Path, threads_dir: Path) -> None:
    """同步call_skill共享submission时，文本/工具仍有准确的thread归属。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=_delegating_client(300), compressors=[],
    )
    engine = await pool.get_or_create(session_id='obs', entry_skill_id='code-reviewer')
    events = []
    done = asyncio.Event()

    async def collect() -> None:
        """捕获完整事件信封直到根执行结束。"""
        async for event in engine.subscribe_all():
            events.append(event)
            if (event.msg.kind == 'turn_completed'
                    and event.msg.data.get('thread_id') == engine.thread_id):
                done.set()
                return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    try:
        await engine.submit(taifeng.UserMessage(text='审查代码'))
        await asyncio.wait_for(done.wait(), GUARD_TIMEOUT_SECONDS)
        starts = [event for event in events if event.msg.kind == 'turn_started']
        assert len(starts) == 2
        root = next(event.execution for event in starts if event.msg.data['thread_id'] == engine.thread_id)
        child = next(event.execution for event in starts if event.msg.data['thread_id'] != engine.thread_id)
        assert root is not None and child is not None
        assert child.parent_execution_id == root.execution_id
        assert starts[0].submission_id == starts[1].submission_id
        child_text = [event for event in events if event.msg.kind == 'assistant_text'
                      and event.execution.execution_id == child.execution_id]
        assert child_text
        updates = [event for event in events if event.msg.kind == 'execution_updated']
        assert updates and all(event.execution is not None for event in updates)
        child_updates = [event for event in updates if event.execution.execution_id == child.execution_id]
        assert child_updates[-1].msg.data['usage']['total_tokens'] == 300
        assert any(event.msg.data['context_measured'] for event in child_updates)
    finally:
        task.cancel()
        await pool.close()


async def test_peer_wake_keeps_original_parent(skills_dir: Path, threads_dir: Path) -> None:
    """兄弟唤醒已存在节点，只产生新attempt，不能改变其创建父级。"""
    from taifeng.llm.providers import RoutingSimClient, SimTurn
    from tests.conftest import wait_for_condition

    observer = _observer()
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, compressors=[],
        model_client=RoutingSimClient(routes={'style-checker': [
            SimTurn(text='first'), SimTurn(text='awake'),
        ]}),
    )
    engine = await pool.get_or_create(session_id='peer-obs', entry_skill_id='code-reviewer')
    events = []

    async def collect() -> None:
        """捕获目标执行的两次启动，保留身份信封。"""
        async for event in engine.subscribe_all():
            events.append(event)

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    try:
        with observer.execution_scope(session_id='peer-obs', thread_id=engine.thread_id, skill_id='root'):
            target = await engine.spawn_skill(skill_id='style-checker', args={}, reason='first')
        handle = target['handle_id']
        await wait_for_condition(lambda: engine.spawn_status([handle])[handle]['status'] == 'done')
        with observer.execution_scope(session_id='peer-obs', thread_id='sibling', skill_id='worker'):
            await engine.deliver_peer_message(
                target=target['child_thread_id'], text='continue', mode='trigger_turn',
                from_thread_id=engine.thread_id,
            )
        await wait_for_condition(lambda: len([event for event in events
            if event.msg.kind == 'turn_completed'
            and event.msg.data.get('thread_id') == target['child_thread_id']]) == 2)
        starts = [event.execution for event in events if event.msg.kind == 'turn_started'
                  and event.msg.data['thread_id'] == target['child_thread_id']]
        assert len(starts) == 2 and starts[0].attempt_id != starts[1].attempt_id
        assert starts[0].parent_execution_id == engine.thread_id
        assert starts[1].parent_execution_id == starts[0].parent_execution_id
    finally:
        collector.cancel()
        await pool.close()


def test_sampling_function_stays_within_named_limit() -> None:
    """ADR0121为最热采样函数规定259行上限，观测不能突破。"""
    import inspect

    from taifeng.loop.turn_sample import TurnSample

    assert len(inspect.getsource(TurnSample.sample_once).splitlines()) <= 259
