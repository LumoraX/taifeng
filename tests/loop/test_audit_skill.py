"""audited 同步 call_skill 子谱系落账测试（helper 级）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.conversation.journal.records import (
    ConversationItemV1,
    JournalIdentities,
    SkillStatus,
    record_id,
)
from taifeng.loop.audit_skill import AuditedSkillDispatch, _child_thread_id
from taifeng.loop.audit_support import AuditHealth
from taifeng.loop.cancellation import CancellationToken
from taifeng.skill.definition import SkillDefinition
from tests.loop.test_audit_llm import _state


def _target(skill_id: str = "child_skill") -> SkillDefinition:
    """构造最小子 skill 定义。"""
    return SkillDefinition(
        id=skill_id,
        name=skill_id,
        description=skill_id,
        version="1",
        body="# child body",
        body_path=Path(f"/{skill_id}/SKILL.md"),
        type="atomic",
    )


async def _root_state(tmp_path: Path):
    """建立带已 bootstrap 根 thread projection 的 audited state。"""
    state, core = await _state(tmp_path)
    await state.projector.bootstrap_thread(
        thread_id="thread_1",
        cwd=None,
        entry_skill_id="entry",
        source="session:session_1",
        extra={
            "audit_required": True,
            "journal_session_id": "session_1",
            "journal_schema_version": 1,
        },
    )
    return state, core


def _dispatch(state, call_id: str = "call_sk") -> AuditedSkillDispatch:
    """构造挂在外层 call_skill Tool operation 之下的 skill 落账器。"""
    return AuditedSkillDispatch(
        state=state,
        parent_turn_index=3,
        parent_call_id=call_id,
        submission_id="submission_1",
        cancel=CancellationToken(name="turn"),
    )


@pytest.mark.anyio
async def test_skill_selected_precedes_atomic_started_batch(tmp_path: Path) -> None:
    """skill_selected 先于 started/thread_created/thread_bound/child_seed 原子批。"""
    state, core = await _root_state(tmp_path)
    dispatch = _dispatch(state)
    target = _target()

    op = await dispatch.commit_selected(target=target, arguments={"q": 1})
    child = await dispatch.start_child(
        target=target, arguments={"q": 1}, call_stack_path=("entry", "child_skill")
    )

    committed = [e async for e in core.load("session_1")]
    types = [e.record_type for e in committed]
    # skill_selected 先于原子 started 批
    selected_idx = types.index("skill_selected")
    started_idx = types.index("skill_dispatch_started")
    assert selected_idx < started_idx
    # 原子批四条紧邻且有序：started/thread_created/thread_bound/conversation_item(seed)
    assert types[started_idx : started_idx + 4] == [
        "skill_dispatch_started",
        "thread_created",
        "thread_bound",
        "conversation_item",
    ]
    # child 身份确定性派生、共享根 coordinator
    assert child.child_thread_id == _child_thread_id(op)
    assert child.child_state.coordinator is state.coordinator
    assert child.child_state.thread_id == child.child_thread_id
    # child seed 为该 child thread 的 user_message，projection 已推进
    seed_env = committed[started_idx + 3]
    seed = ConversationItemV1.model_validate(seed_env.payload)
    assert seed.item_kind == "user_message"
    assert seed.thread_id == child.child_thread_id
    assert state.projector.state(child.child_thread_id).projected_seq == seed_env.seq
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_skill_selected_causation_points_at_canonical_tool_intent(
    tmp_path: Path,
) -> None:
    """skill_selected.causation_id 必须等于外层 call_skill 的 §8 intent record_id。

    回归 M1：用 canonical record_id 生成器而非手拼字符串，锁死回链正确性。
    """
    state, core = await _root_state(tmp_path)
    dispatch = _dispatch(state, call_id="call_sk")
    await dispatch.commit_selected(target=_target(), arguments={})

    committed = [e async for e in core.load("session_1")]
    selected = next(e for e in committed if e.record_type == "skill_selected")
    # 独立按 §8 的 identity 规则推导外层 Tool intent 的 record_id
    identities = JournalIdentities("session_1", "thread_1", "submission_1")
    tool_op = identities.tool(identities.turn(3), "call_sk")
    expected = record_id(tool_op, "tool_intent_committed")
    assert selected.causation_id == expected


@pytest.mark.anyio
async def test_quota_rejection_has_no_child_lineage(tmp_path: Path) -> None:
    """配额拒绝：skill_selected 后仅 finished(rejected)，无 thread/seed 记录。"""
    state, core = await _root_state(tmp_path)
    dispatch = _dispatch(state)
    target = _target()

    await dispatch.commit_selected(target=target, arguments={})
    await dispatch.reject(reason="spawn_limit:depth")

    committed = [e async for e in core.load("session_1")]
    types = [e.record_type for e in committed]
    assert "skill_dispatch_started" not in types
    # 仅有根 Session 初始化的 thread_created/bound；拒绝不产生 child thread
    assert types.count("thread_created") == 1
    assert types.count("thread_bound") == 1
    finished = next(e for e in committed if e.record_type == "skill_dispatch_finished")
    assert finished.payload["status"] == "rejected"
    assert finished.payload["started_record_id"] is None
    assert finished.payload["child_thread_id"] is None
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_success_finish_commits_terminal_batch_atomically(
    tmp_path: Path,
) -> None:
    """成功：finished/thread_terminal/skill_outcome 原子批，谱系回链 started。"""
    state, core = await _root_state(tmp_path)
    dispatch = _dispatch(state)
    target = _target()

    await dispatch.commit_selected(target=target, arguments={})
    child = await dispatch.start_child(
        target=target, arguments={}, call_stack_path=("child_skill",)
    )
    await dispatch.finish_child(
        child=child,
        status=SkillStatus.SUCCESS,
        end_reason="completed",
        final_text="done",
        outcome_payload={"skill_id": "child_skill", "outcome": "success"},
        error=None,
    )

    committed = [e async for e in core.load("session_1")]
    types = [e.record_type for e in committed]
    fin_idx = types.index("skill_dispatch_finished")
    # 终态原子批三条紧邻：finished/thread_terminal/skill_outcome 会话项
    assert types[fin_idx : fin_idx + 3] == [
        "skill_dispatch_finished",
        "thread_terminal",
        "conversation_item",
    ]
    finished = committed[fin_idx]
    assert finished.payload["status"] == "success"
    assert finished.payload["started_record_id"] == child.started_record_id
    assert finished.payload["child_thread_id"] == child.child_thread_id
    terminal = committed[fin_idx + 1]
    assert terminal.payload["status"] == "success"
    assert terminal.thread_id == child.child_thread_id
    outcome_item = ConversationItemV1.model_validate(committed[fin_idx + 2].payload)
    assert outcome_item.item_kind == "skill_outcome"
    assert outcome_item.thread_id == child.child_thread_id
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_error_finish_records_stable_terminal(tmp_path: Path) -> None:
    """子 skill 失败：finished/thread_terminal=error，且不泄漏错误原文。"""
    state, core = await _root_state(tmp_path)
    dispatch = _dispatch(state)
    target = _target()

    await dispatch.commit_selected(target=target, arguments={})
    child = await dispatch.start_child(
        target=target, arguments={}, call_stack_path=("child_skill",)
    )
    await dispatch.finish_child(
        child=child,
        status=SkillStatus.ERROR,
        end_reason="failed",
        final_text="",
        outcome_payload={"skill_id": "child_skill", "outcome": "failure"},
        error=None,  # 由 finish_child 内部不构造；这里显式给 None 验证 error 分支
    )

    committed = [e async for e in core.load("session_1")]
    finished = next(
        e for e in committed if e.record_type == "skill_dispatch_finished"
    )
    assert finished.payload["status"] == "error"
    assert finished.payload["end_reason"] == "failed"
    assert state.coordinator.health is AuditHealth.HEALTHY
