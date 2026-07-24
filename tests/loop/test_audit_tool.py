"""audited Tool intent/outcome 收敛测试（helper 级）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.journal.records import ConversationItemV1
from taifeng.loop.audit_support import AuditHealth, SessionAuditFrozenError
from taifeng.loop.audit_tool import audited_tool_batch
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.tool_batch import ToolCallOutcome, ToolCallRequest
from taifeng.tool.spec import ToolResult, ToolSpec
from tests.loop.test_audit_llm import _state

if TYPE_CHECKING:
    from pathlib import Path


async def _noop_handler(args: dict, ctx: object) -> ToolResult:
    """占位 handler；helper 级测试直接注入 outcome，不真正执行。"""
    del args, ctx
    return ToolResult.ok("ok")


def _spec(name: str, *, effect_kind: str, reconciliation: str) -> ToolSpec:
    """构造带 ADR 0025 分类的最小 ToolSpec。"""
    return ToolSpec(
        name=name,
        description="d",
        input_schema={"type": "object"},
        handler=_noop_handler,
        effect_kind=effect_kind,
        reconciliation=reconciliation,
    )


class _Registry:
    """最小 registry 视图：name → ToolSpec。"""

    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self._specs = specs

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)


def _request(index: int, call_id: str, name: str) -> ToolCallRequest:
    """构造一条已解析的 tool call 请求。"""
    return ToolCallRequest(
        index=index,
        call_id=call_id,
        name=name,
        arguments={"x": 1},
        arguments_raw='{"x": 1}',
        parallel_safe=False,
    )


def _outcome(
    req: ToolCallRequest,
    result: ToolResult,
    *,
    suspend: object = None,
) -> ToolCallOutcome:
    """构造一条 tool call 结果。"""
    return ToolCallOutcome(
        index=req.index,
        call_id=req.call_id,
        name=req.name,
        result=result,
        duration_ms=5,
        suspend=suspend,
    )


async def _bootstrapped_state(tmp_path: Path):
    """建立带已 bootstrap projector 的最小 audited state。"""
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


async def _run(
    state,
    requests,
    registry,
    outcomes_for,
    *,
    cancel: CancellationToken | None = None,
    dispatch_probe=None,
):
    """驱动 audited_tool_batch，run_dispatch 返回注入的 outcomes。"""
    token = cancel or CancellationToken(name="turn")

    async def run_dispatch():
        if dispatch_probe is not None:
            await dispatch_probe()
        return outcomes_for()

    return await audited_tool_batch(
        state=state,
        submission_id="submission_1",
        turn_index=3,
        iteration=2,
        requests=requests,
        registry=registry,
        run_dispatch=run_dispatch,
        cancel=token,
        finalization_timeout=5.0,
    )


@pytest.mark.anyio
async def test_tool_success_commits_intent_then_outcome_and_fco(
    tmp_path: Path,
) -> None:
    """成功工具：intent 先于 outcome durable，落唯一 fco 会话项并推进 projection。"""
    state, core = await _bootstrapped_state(tmp_path)
    req = _request(0, "call_a", "file_write")
    registry = _Registry(
        {"file_write": _spec(
            "file_write",
            effect_kind="external_non_idempotent",
            reconciliation="manual",
        )}
    )
    fco_items = await _run(
        state,
        [req],
        registry,
        lambda: [_outcome(req, ToolResult.ok("done"))],
    )

    committed = [envelope async for envelope in core.load("session_1")]
    intent, outcome, conversation = committed[-3:]
    assert intent.record_type == "tool_intent_committed"
    assert intent.payload["call_id"] == "call_a"
    assert intent.payload["effect_kind"] == "external_non_idempotent"
    assert outcome.record_type == "tool_outcome_committed"
    assert outcome.causation_id == intent.record_id
    assert outcome.payload["status"] == "success"
    assert outcome.payload["intent_record_id"] == intent.record_id
    assert conversation.record_type == "conversation_item"
    item = ConversationItemV1.model_validate(conversation.payload)
    assert item.item_kind == "function_call_output"
    assert item.payload["call_id"] == "call_a"
    assert item.payload["is_error"] is False
    # projection 推进到 fco 会话项；hot history 追加逐字一致的 fco
    assert state.projector.state("thread_1").projected_seq == conversation.seq
    assert [i.kind for i in fco_items] == ["function_call_output"]
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_ordered_intents_durable_before_dispatch(tmp_path: Path) -> None:
    """两条意图作为一个原子 batch 先于任何 dispatch durable，且按 call-index 有序。"""
    state, core = await _bootstrapped_state(tmp_path)
    reqs = [_request(0, "call_a", "shell_exec"), _request(1, "call_b", "todo_write")]
    registry = _Registry({
        "shell_exec": _spec(
            "shell_exec",
            effect_kind="external_non_idempotent",
            reconciliation="manual",
        ),
        "todo_write": _spec(
            "todo_write", effect_kind="idempotent", reconciliation="retry"
        ),
    })
    seen: list[str] = []

    async def probe() -> None:
        # dispatch 运行时，两条 intent 必须已 durable
        committed = [envelope async for envelope in core.load("session_1")]
        seen.extend(
            e.payload["call_id"]
            for e in committed
            if e.record_type == "tool_intent_committed"
        )

    await _run(
        state,
        reqs,
        registry,
        lambda: [
            _outcome(reqs[0], ToolResult.ok("a")),
            _outcome(reqs[1], ToolResult.ok("b")),
        ],
        dispatch_probe=probe,
    )

    # 派发前两条意图已按 call-index 顺序 durable
    assert seen == ["call_a", "call_b"]


@pytest.mark.anyio
async def test_not_offered_tool_rejected_without_runtime(tmp_path: Path) -> None:
    """未提供的工具：commit intent + rejected outcome，不进入 runtime。"""
    state, core = await _bootstrapped_state(tmp_path)
    req = _request(0, "call_a", "ghost_tool")
    registry = _Registry({})  # 无 spec → 保守分类，dispatch 产出 not_offered

    await _run(
        state,
        [req],
        registry,
        lambda: [_outcome(
            req, ToolResult.error("tool_not_offered: ghost_tool", reason="not_offered")
        )],
    )

    committed = [envelope async for envelope in core.load("session_1")]
    outcome = committed[-2]
    assert outcome.record_type == "tool_outcome_committed"
    assert outcome.payload["status"] == "rejected"
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_cancel_before_dispatch_yields_cancelled_without_runtime(
    tmp_path: Path,
) -> None:
    """整批派发前取消：outcome=cancelled，run_dispatch 从不执行，不冻结。"""
    state, core = await _bootstrapped_state(tmp_path)
    req = _request(0, "call_a", "file_write")
    registry = _Registry({"file_write": _spec(
        "file_write",
        effect_kind="external_non_idempotent",
        reconciliation="manual",
    )})
    token = CancellationToken(name="turn")
    token.cancel()
    dispatched = False

    def outcomes_for():
        nonlocal dispatched
        dispatched = True
        return []

    await _run(state, [req], registry, outcomes_for, cancel=token)

    assert dispatched is False
    committed = [envelope async for envelope in core.load("session_1")]
    outcome = committed[-2]
    assert outcome.payload["status"] == "cancelled"
    # 取消不是失败：Session 不冻结
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_ambiguous_cancel_after_dispatch_is_unknown_and_freezes(
    tmp_path: Path,
) -> None:
    """外部工具在 dispatch 中途取消 → UNKNOWN，记录后冻结 Session。"""
    state, core = await _bootstrapped_state(tmp_path)
    req = _request(0, "call_a", "file_write")
    registry = _Registry({"file_write": _spec(
        "file_write",
        effect_kind="external_non_idempotent",
        reconciliation="manual",
    )})

    with pytest.raises(SessionAuditFrozenError):
        await _run(
            state,
            [req],
            registry,
            lambda: [_outcome(
                req, ToolResult.error("cancelled", reason="cancelled")
            )],
        )

    committed = [envelope async for envelope in core.load("session_1")]
    outcome = next(
        e for e in committed if e.record_type == "tool_outcome_committed"
    )
    assert outcome.payload["status"] == "unknown"
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_idempotent_cancel_is_cancelled_not_unknown(tmp_path: Path) -> None:
    """幂等工具 dispatch 中途取消无歧义 → cancelled，不冻结。"""
    state, core = await _bootstrapped_state(tmp_path)
    req = _request(0, "call_a", "todo_write")
    registry = _Registry({"todo_write": _spec(
        "todo_write", effect_kind="idempotent", reconciliation="retry"
    )})

    await _run(
        state,
        [req],
        registry,
        lambda: [_outcome(req, ToolResult.error("cancelled", reason="cancelled"))],
    )

    committed = [envelope async for envelope in core.load("session_1")]
    outcome = committed[-2]
    assert outcome.payload["status"] == "cancelled"
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_declared_non_suspending_tool_that_suspends_errors_and_freezes(
    tmp_path: Path,
) -> None:
    """声明 non-suspending 的工具运行时挂起 → error outcome + 冻结（不进 HITL）。"""
    state, core = await _bootstrapped_state(tmp_path)
    req = _request(0, "call_a", "shell_exec")
    registry = _Registry({"shell_exec": _spec(
        "shell_exec",
        effect_kind="external_non_idempotent",
        reconciliation="manual",
    )})
    pending = object()

    with pytest.raises(SessionAuditFrozenError):
        await _run(
            state,
            [req],
            registry,
            lambda: [_outcome(
                req, ToolResult(output="<suspended>"), suspend=pending
            )],
        )

    committed = [envelope async for envelope in core.load("session_1")]
    outcome = next(
        e for e in committed if e.record_type == "tool_outcome_committed"
    )
    # 挂起违约先记 error 终态，随后冻结
    assert outcome.payload["status"] == "error"
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_parallel_partial_completion_orders_outcomes_by_index(
    tmp_path: Path,
) -> None:
    """并发部分完成：全部意图按 call-index 顺序各得一个终态。"""
    state, core = await _bootstrapped_state(tmp_path)
    reqs = [_request(0, "call_a", "todo_write"), _request(1, "call_b", "todo_write")]
    registry = _Registry({"todo_write": _spec(
        "todo_write", effect_kind="idempotent", reconciliation="retry"
    )})

    # dispatch 乱序返回，收敛仍须按 call-index 有序
    await _run(
        state,
        reqs,
        registry,
        lambda: [
            _outcome(reqs[1], ToolResult.error("boom", reason="exception")),
            _outcome(reqs[0], ToolResult.ok("ok")),
        ],
    )

    committed = [envelope async for envelope in core.load("session_1")]
    outcomes = [
        e for e in committed if e.record_type == "tool_outcome_committed"
    ]
    assert [e.payload["call_id"] for e in outcomes] == ["call_a", "call_b"]
    assert [e.payload["status"] for e in outcomes] == ["success", "error"]
    assert state.coordinator.health is AuditHealth.HEALTHY
