"""执行投影的事实状态、恢复与有界性测试。"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from taifeng.loop.event import EventMsg
from taifeng.loop.execution_identity import ExecutionIdentity
from taifeng.telemetry.execution_projection import (
    ExecutionProjection,
    OrchestrationPlanLimits,
)


def event(kind: str, seq: int = 0, *, node: str = 'root', attempt: str = 'a',
          parent: str | None = None, **data: Any) -> EventMsg:
    """构造完整的执行事实，不依赖模型正文。"""
    return EventMsg.model_validate({
        'submission_id': 'shared', 'seq': seq, 'msg': {'kind': kind, 'data': data},
        'execution': ExecutionIdentity(execution_id=node, attempt_id=attempt,
            thread_id=node, skill_id='same-skill', model='sim',
            parent_execution_id=parent, parent_thread_id=parent),
    })


def test_empty_capacity_and_frozen_snapshot() -> None:
    """空投影与不可变快照拥有明确边界。"""
    assert ExecutionProjection().snapshot() == ()
    with pytest.raises(ValueError):
        ExecutionProjection(max_nodes=0)
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    with pytest.raises(FrozenInstanceError):
        p.snapshot()[0].state = 'failed'  # type: ignore[misc]


def test_parallel_tools_are_tracked_by_call_id_and_errors_do_not_fail_turn() -> None:
    """完成其中一个同名工具不能清掉另一个调用。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('tool_call_started', 1, call_id='1', name='read_skill'))
    p.handle(event('tool_call_started', 2, call_id='2', name='call_skill'))
    assert p.snapshot()[0].state == 'waiting_children'
    p.handle(event('tool_call_completed', 3, call_id='2', name='call_skill', is_error=True))
    assert p.snapshot()[0].state == 'tool'
    assert p.snapshot()[0].active_tools == ('read_skill',)
    p.handle(event('tool_call_completed', 4, call_id='1', name='read_skill'))
    assert p.snapshot()[0].state == 'running'


def test_usage_absolute_self_only_resume_and_late_attempt() -> None:
    """重复快照不翻倍，恢复累加自身用量并拒绝旧 attempt 复活。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    usage = {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}
    p.handle(event('execution_updated', 1, phase='model', usage=usage,
                   usage_complete=True, iteration=2, context_tokens=10,
                   context_window=100, context_measured=True))
    p.handle(event('execution_updated', 2, phase='running', usage=usage,
                   usage_complete=True))
    p.handle(event('turn_completed', 3, usage=usage,
                   subtree_usage={'total_tokens': 9999}, iterations=2))
    p.handle(event('execution_updated', 4, phase='model', usage=usage))
    assert p.snapshot()[0].state == 'completed'
    p.handle(event('turn_started', 5, attempt='b'))
    assert p.snapshot()[0].state == 'running'
    assert p.snapshot()[0].iterations == 0
    assert not p.snapshot()[0].context_measured
    p.handle(event('execution_updated', 6, attempt='b', phase='model',
                   usage={'input_tokens': 3, 'output_tokens': 1, 'total_tokens': 4},
                   usage_complete=True))
    assert p.snapshot()[0].total_tokens == 16
    p.handle(event('turn_completed', 7, attempt='a', usage=usage))
    assert p.snapshot()[0].state == 'model'
    assert p.incomplete


def test_missing_usage_is_visible_and_preserves_known_tokens() -> None:
    """无计量事实不能把已知用量清零或伪装为完整。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('execution_updated', 1, phase='running',
                   usage={'input_tokens': 7, 'total_tokens': 7},
                   usage_complete=True, context_tokens=20, context_measured=False))
    p.handle(event('execution_updated', 2, phase='failed', usage_complete=False))
    snap = p.snapshot()[0]
    assert snap.input_tokens == 7
    assert not snap.usage_complete
    assert not snap.context_measured
    assert snap.state == 'failed'
    assert snap.finished_at is not None


def test_usage_attempt_diagnostics_survive_projection() -> None:
    """投影与终端消费 provider 缺 usage 的尝试计数。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('execution_updated', 1, phase='model', iteration=1,
                   usage={'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6},
                   usage_complete=False, usage_attempts_started=3,
                   usage_attempts_metered=2, usage_attempts_missing=1))
    snap = p.snapshot()[0]
    assert snap.usage_attempts_started == 3
    assert snap.usage_attempts_metered == 2
    assert snap.usage_attempts_missing == 1


def test_spawn_handle_targets_child_even_after_parent_attempt_changes() -> None:
    """queued 失败与挂起后取消均收敛子节点。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('spawn_started', 1, handle_id='h', child_thread_id='child', skill_id='same'))
    assert p.snapshot()[1].state == 'queued'
    p.handle(event('turn_started', 2, attempt='b'))
    p.handle(event('spawn_suspended', 3, attempt='b', handle_id='h', thread_id='child'))
    assert p.snapshot()[1].state == 'waiting_input'
    p.handle(event('spawn_cancelled', 4, attempt='b', handle_id='h'))
    assert p.snapshot()[1].state == 'cancelled'
    assert p.snapshot()[0].state == 'running'
    p.handle(event('spawn_started', 5, attempt='b', handle_id='h2', child_thread_id='queued', skill_id='same'))
    p.handle(event('spawn_failed', 6, attempt='b', handle_id='h2'))
    assert p.snapshot()[2].state == 'failed'


def test_child_identity_resume_preserves_parent_and_terminal_is_sticky() -> None:
    """同 Skill 兄弟分开，续跑不凭空丢掉已知父级。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('turn_started', 1, node='c1', parent='root'))
    p.handle(event('turn_started', 2, node='c2', parent='root'))
    p.handle(event('turn_suspended', 3, node='c1'))
    assert p.snapshot()[1].state == 'waiting_input'
    p.handle(event('turn_started', 4, node='c1', attempt='resume'))
    assert p.snapshot()[1].parent_execution_id == 'root'
    p.handle(event('turn_completed', 5, node='c1', attempt='resume', end_reason='cancelled'))
    p.handle(event('tool_call_started', 6, node='c1', attempt='resume', call_id='x', name='read_skill'))
    assert p.snapshot()[1].state == 'cancelled'
    assert p.snapshot()[2].state == 'running'


def test_seq_duplicates_gaps_late_subscription_and_limits() -> None:
    """重复事件幂等，跳号、迟到与容量截断显式暴露。"""
    p = ExecutionProjection(max_nodes=2)
    p.handle(event('turn_started', 3))
    assert p.incomplete
    p.handle(event('tool_call_started', 4, call_id='x', name='read_skill'))
    p.handle(event('tool_call_completed', 4, call_id='x', name='read_skill'))
    assert p.snapshot()[0].state == 'tool'
    p.handle(event('turn_started', 6, node='child'))
    p.handle(event('turn_started', 5, node='late'))
    p.handle(event('turn_started', 7, node='excess'))
    assert len(p.snapshot()) == 2
    assert p.incomplete


@pytest.mark.parametrize('name', ['call_skill', 'await_skills', 'wait_peer', 'wait_any'])
def test_wait_tools_keep_parallel_same_name_calls(name: str) -> None:
    """同名等待调用以 call_id 计数，先完成一个仍在等待。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('tool_call_started', 1, call_id='first', name=name))
    p.handle(event('tool_call_started', 2, call_id='second', name=name))
    p.handle(event('tool_call_completed', 3, call_id='first', name=name))
    assert p.snapshot()[0].active_tools == (name,)
    assert p.snapshot()[0].state == 'waiting_children'


def test_observation_without_start_and_legacy_events() -> None:
    """旧事件不虚构执行；中途身份只建立不完整观察。"""
    p = ExecutionProjection()
    legacy = EventMsg.model_validate({'submission_id': 'old', 'seq': 0,
        'msg': {'kind': 'assistant_text', 'data': {'delta': '开始工具或完成'}}})
    p.handle(legacy)
    assert p.snapshot() == ()
    p.handle(event('assistant_text', 1, delta='已完成，不需要工具'))
    assert p.snapshot()[0].state == 'running'
    assert p.incomplete


def test_dynamic_orchestration_plan_history_is_append_only() -> None:
    """计划事件按执行节点追加，重规划历史不被覆盖。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event(
        'orchestration_plan_resolved', 1,
        skill_id='coordinator',
        groups=[{'type': 'parallel', 'skills': ['worker-a', 'worker-b']}],
    ))
    first = p.plan_snapshot()
    assert len(first) == 1
    assert first[0].groups[0].skills == ('worker-a', 'worker-b')

    p.handle(event(
        'orchestration_plan_resolved', 2,
        skill_id='coordinator',
        groups=[{'type': 'serial', 'skills': ['summarizer']}],
    ))
    history = p.plan_snapshot()
    assert len(history) == 2
    assert history[0].groups[0].skills == ('worker-a', 'worker-b')
    assert history[1].groups[0].kind == 'serial'
    assert history[1].groups[0].skills == ('summarizer',)


def test_malformed_orchestration_plan_is_incomplete() -> None:
    """计划结构损坏时不猜测关系，只公开不完整状态。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event(
        'orchestration_plan_resolved', 1,
        skill_id='coordinator',
        groups=[{'type': 'parallel', 'skills': ['ok', 3]}],
    ))
    assert p.plan_snapshot() == ()
    assert p.incomplete


@pytest.mark.parametrize(
    ('limits', 'groups'),
    [
        (OrchestrationPlanLimits(max_groups=1), [
            {'type': 'parallel', 'skills': ['a']},
            {'type': 'serial', 'skills': ['b']},
        ]),
        (OrchestrationPlanLimits(max_skills_per_group=1), [
            {'type': 'parallel', 'skills': ['a', 'b']},
        ]),
        (OrchestrationPlanLimits(max_skill_id_length=3), [
            {'type': 'parallel', 'skills': ['long']},
        ]),
        (OrchestrationPlanLimits(max_condition_length=3), [
            {'type': 'when', 'condition': 'long'},
        ]),
        (OrchestrationPlanLimits(max_plan_bytes=32), [
            {'type': 'parallel', 'skills': ['long-skill-name']},
        ]),
    ],
)
def test_plan_shape_limits_mark_projection_incomplete(
    limits: OrchestrationPlanLimits,
    groups: list[dict[str, object]],
) -> None:
    """计划各层超限时拒绝摘要，不能把超大输入保留在内存。"""
    p = ExecutionProjection(plan_limits=limits)
    p.handle(event('turn_started'))
    p.handle(event('orchestration_plan_resolved', 1, skill_id='coordinator', groups=groups))
    assert p.plan_snapshot() == ()
    assert p.incomplete


def test_plan_limits_validate_positive_integer_values() -> None:
    """边界配置拒绝布尔、零和负数。"""
    with pytest.raises(ValueError):
        OrchestrationPlanLimits(max_groups=0)
    with pytest.raises(ValueError):
        OrchestrationPlanLimits(max_skills_per_group=True)  # type: ignore[arg-type]


def test_plan_history_capacity_is_bounded_and_visible() -> None:
    """计划轨迹达到容量上限时不静默丢失，投影显式标记不完整。"""
    p = ExecutionProjection(max_nodes=1)
    p.handle(event('turn_started'))
    p.handle(event(
        'orchestration_plan_resolved', 1,
        skill_id='coordinator',
        groups=[{'type': 'parallel', 'skills': ['first']}],
    ))
    p.handle(event(
        'orchestration_plan_resolved', 2,
        skill_id='coordinator',
        groups=[{'type': 'parallel', 'skills': ['second']}],
    ))
    assert len(p.plan_snapshot()) == 1
    assert p.plan_snapshot()[0].groups[0].skills == ('first',)
    assert p.incomplete


def test_timeline_after_returns_safe_facts_and_cursor() -> None:
    """调用方可按 seq 增量绘制调用历史，事实不含正文。"""
    p = ExecutionProjection(timeline_capacity=8)
    p.handle(event('turn_started'))
    p.handle(event('tool_call_started', 1, call_id='c1', name='read', arguments='secret'))
    p.handle(event('tool_call_completed', 2, call_id='c1', name='read', output='secret'))

    page = p.timeline_after(0)
    assert [item.seq for item in page.items] == [1, 2]
    assert page.next_seq == 2
    assert not page.cursor_expired
    assert page.items[0].call_id == 'c1'
    assert page.items[0].name == 'read'
    assert not hasattr(page.items[0], 'data')

    next_page = p.timeline_after(page.next_seq)
    assert next_page.items == ()
    assert next_page.next_seq == 2


def test_timeline_capacity_exposes_expired_cursor_and_gaps() -> None:
    """历史被截断或 seq 有缺口时，增量消费者得到显式状态。"""
    p = ExecutionProjection(timeline_capacity=2)
    p.handle(event('turn_started'))
    p.handle(event('execution_updated', 2, phase='model'))
    p.handle(event('turn_completed', 3))
    page = p.timeline_after(0)
    assert [item.seq for item in page.items] == [2, 3]
    assert page.cursor_expired
    assert page.incomplete
    assert p.incomplete


def test_timeline_capacity_must_be_positive() -> None:
    """时间线容量是资源边界，不能关闭或使用负数。"""
    with pytest.raises(ValueError):
        ExecutionProjection(timeline_capacity=0)


def test_tool_and_handle_capacity_do_not_grow_without_bound() -> None:
    """异常流中的活动调用与句柄同样受容量约束。"""
    p = ExecutionProjection(max_nodes=2)
    p.handle(event('turn_started'))
    for i in range(3):
        p.handle(event('tool_call_started', i + 1, call_id=str(i), name='read_skill'))
    assert len(p.snapshot()[0].active_tools) == 2
    assert p.incomplete
    for i in range(3):
        p.handle(event('spawn_started', i + 4, handle_id=str(i), child_thread_id='child', skill_id='same'))
    assert len(p._handles) == 2


def test_zero_usage_and_context_estimate_are_distinct() -> None:
    """明确的零用量可以完整，估算上下文仍不冒充实测。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('execution_updated', 1, phase='running', iteration=1,
        usage={'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
        usage_complete=True, context_tokens=80, context_window=1000,
        context_measured=False))
    assert p.snapshot()[0].usage_complete
    assert p.snapshot()[0].total_tokens == 0
    assert not p.snapshot()[0].context_measured


def test_detached_failure_overrides_provisional_turn_completion() -> None:
    """子runner迭代耗尽后，协调器的明确失败必须显示为失败。"""
    p = ExecutionProjection()
    p.handle(event('spawn_started', handle_id='h', child_thread_id='child', skill_id='same'))
    p.handle(event('turn_started', 1, node='child', parent='root'))
    p.handle(event('turn_completed', 2, node='child', end_reason='max_iterations'))
    p.handle(event('spawn_failed', 3, handle_id='h'))
    assert p.snapshot()[0].state == 'failed'


def test_suspended_attempt_ignores_late_progress_until_new_attempt() -> None:
    """挂起后旧attempt的采样和工具事件不能复活节点。"""
    p = ExecutionProjection()
    p.handle(event('turn_started'))
    p.handle(event('turn_suspended', 1))
    p.handle(event('execution_updated', 2, phase='model'))
    p.handle(event('tool_call_started', 3, call_id='late', name='read_skill'))
    assert p.snapshot()[0].state == 'waiting_input'
    assert p.snapshot()[0].active_tools == ()
    p.handle(event('turn_started', 4, attempt='resumed'))
    assert p.snapshot()[0].state == 'running'
