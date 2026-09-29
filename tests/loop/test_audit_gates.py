"""审计模式下的 hook 与权限裁决（ADR 0096）：裁决先落账，再生效。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.gate_records import HookEvaluatedV1, PermissionDecidedV1
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.hooks import HookDecision, HookRegistry, HookRunner
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_config import (
    AuditCapabilityError,
    AuditConfig,
    AuditStaticInputs,
    _validate_unsupported_fields,
)
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionGrant,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    SuspendingPrompter,
)
from taifeng.skill import DispatchPolicy
from taifeng.tool.spec import ToolResult, ToolSpec
from tests.conftest import run_until_root_done, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope

_SESSION = "ses-gates"

_ENTRY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [leaf]
tool_names: [remote_write, guarded]
max_call_depth: 3
---
# ENTRY_BODY_MARK
"""

_LEAF = """---
name: leaf
description: 叶子
version: 1.0.0
type: composite
model: mock-model
tool_names: [remote_write]
max_call_depth: 2
---
# LEAF_BODY_MARK
"""


def _skills(tmp_path: Path, leaf_tool: str = "remote_write") -> Path:
    root = tmp_path / "skills"
    leaf = _LEAF.replace("tool_names: [remote_write]", f"tool_names: [{leaf_tool}]")
    for name, body in (("entry", _ENTRY), ("leaf", leaf)):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _remote_write(executed: list[dict[str, Any]]) -> ToolSpec:
    async def handler(args: dict[str, Any], ctx: object) -> ToolResult:
        executed.append(dict(args))
        return ToolResult.ok(f"written {args.get('key')}")

    return ToolSpec(
        name="remote_write", description="写外部系统",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        handler=handler, effect_kind="external_non_idempotent", reconciliation="manual",
    )


def _guarded(extra_metadata: dict[str, Any] | None = None) -> ToolSpec:
    """执行前向权限策略申请 ``tool_use`` 的工具。"""

    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        policy = ctx.extras["permission_policy"]
        decision = await policy.check(PermissionRequest.for_tool_call(
            "guarded", args,
            thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("entry",),
            extra_metadata={"call_id": ctx.call_id, **(extra_metadata or {})},
            reason="需要写入",
        ))
        if not decision.granted:
            return ToolResult.error(f"permission_denied: {decision.reason}",
                                    reason="permission_denied")
        return ToolResult.ok("guarded done")

    return ToolSpec(
        name="guarded", description="需审批的工具",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        handler=handler, effect_kind="pure", reconciliation="none",
    )


def _call(name: str, call_id: str, **arguments: Any) -> dict[str, str]:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


class _Run:
    """一个审计 Session。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.executed: list[dict[str, Any]] = []
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.sim: RoutingSimClient
        self.core: JsonlSessionJournalCore

    async def start(
        self,
        entry: list[SimTurn],
        *,
        leaf: list[SimTurn] | None = None,
        resume_thread_id: str | None = None,
        guarded: ToolSpec | None = None,
        leaf_tool: str = "remote_write",
        **kwargs: Any,
    ) -> None:
        self.sim = RoutingSimClient(routes={
            "ENTRY_BODY_MARK": entry, "LEAF_BODY_MARK": leaf or [],
        })
        self.core = JsonlSessionJournalCore(self.tmp_path / "journal")
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(self.tmp_path, leaf_tool),
            threads_dir=self.tmp_path / "threads",
            model_client=AttemptObservableClientAdapter(
                self.sim, provider="sim", default_model="sim-model"
            ),
            compressors=[],
            extra_tools=[_remote_write(self.executed), guarded or _guarded()],
            audit=AuditConfig(
                journal_core=self.core, writer_id="writer-gates",
                max_attachment_bytes=65536, max_total_attachment_bytes=1048576,
            ),
            **kwargs,
        )
        self.engine = await self.pool.get_or_create(
            session_id=_SESSION, entry_skill_id="entry", resume_thread_id=resume_thread_id,
        )

    async def ask(self, text: str = "开始") -> list[Any]:
        return await run_until_root_done(self.engine, taifeng.UserMessage(text=text))

    async def journal(self) -> list[JournalEnvelope]:
        core = JsonlSessionJournalCore(self.tmp_path / "journal")
        return [envelope async for envelope in core.load(_SESSION)]

    async def hooks(self) -> list[tuple[JournalEnvelope, HookEvaluatedV1]]:
        return [
            (e, HookEvaluatedV1.model_validate(e.payload))
            for e in await self.journal() if e.record_type == "hook_evaluated"
        ]

    async def permissions(self) -> list[tuple[JournalEnvelope, PermissionDecidedV1]]:
        return [
            (e, PermissionDecidedV1.model_validate(e.payload))
            for e in await self.journal() if e.record_type == "permission_decided"
        ]


def _hooks(**handlers: Any) -> HookRunner:
    registry = HookRegistry()
    for kind, handler in handlers.items():
        for one in handler if isinstance(handler, list) else [handler]:
            registry.register(kind, one)
    return HookRunner(registry)


def _write_then_done(call_id: str = "c1", key: str = "k1") -> list[SimTurn]:
    return [
        SimTurn(text="写入", tool_calls=[_call("remote_write", call_id, key=key)]),
        SimTurn(text="完成"),
    ]


def _seq(envelopes: list[JournalEnvelope], record_type: str) -> int:
    return next(e.seq for e in envelopes if e.record_type == record_type)


# ====================================================================
# 静态门
# ====================================================================


def _inputs(**overrides: Any) -> AuditStaticInputs:
    return AuditStaticInputs(
        model_client=object(),  # type: ignore[arg-type]
        skill_snapshot=object(),  # type: ignore[arg-type]
        failure_suspension_enabled=False, skill_suspension_enabled=False, **overrides,
    )


def test_hook_runner_and_plain_policy_are_admitted() -> None:
    _validate_unsupported_fields(_inputs(hooks=HookRunner(HookRegistry())))
    _validate_unsupported_fields(_inputs(permission_policy=PermissionPolicy(default_mode="deny")))

    async def approve(request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.allow()

    _validate_unsupported_fields(_inputs(
        permission_policy=PermissionPolicy(prompter=CallbackPrompter(approve)),
    ))


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"hooks": object()}, "audit_hooks_unsupported"),
        ({"permission_policy": object()}, "audit_permission_unsupported"),
        (
            {"permission_policy": PermissionPolicy(prompter=SuspendingPrompter())},
            "audit_permission_unsupported",
        ),
    ],
)
def test_unknown_or_suspending_gates_are_rejected(
    overrides: dict[str, Any], code: str,
) -> None:
    with pytest.raises(AuditCapabilityError) as raised:
        _validate_unsupported_fields(_inputs(**overrides))

    assert raised.value.code == code


# ====================================================================
# hook
# ====================================================================


async def test_args_override_is_journaled_before_the_tool_runs(tmp_path: Path) -> None:
    async def rewrite(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.ok(args_override={"key": "redirected"}, ticket="T-1")

    run = _Run(tmp_path)
    await run.start(_write_then_done(), hooks=_hooks(pre_tool_use=rewrite))

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    assert run.executed == [{"key": "redirected"}]
    ((envelope, record),) = await run.hooks()
    assert (record.hook_kind, record.handler_index, record.allow) == ("pre_tool_use", 0, True)
    assert record.subject == {"call_id": "c1", "tool_name": "remote_write"}
    assert record.overrides == {"args_override": {"key": "redirected"}}
    assert record.metadata_keys == ("ticket",)
    assert envelope.operation_id.endswith(":turn:0:hook:0")
    envelopes = await run.journal()
    assert _seq(envelopes, "tool_intent_committed") < envelope.seq
    assert envelope.seq < _seq(envelopes, "tool_outcome_committed")
    await run.pool.close()


async def test_hook_denial_is_journaled_and_the_tool_does_not_run(tmp_path: Path) -> None:
    async def deny(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.deny("生产环境只读")

    run = _Run(tmp_path)
    await run.start(_write_then_done(), hooks=_hooks(pre_tool_use=deny))

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    assert run.executed == []
    ((_, record),) = await run.hooks()
    assert (record.allow, record.reason) == (False, "生产环境只读")
    (outcome,) = [e for e in await run.journal() if e.record_type == "tool_outcome_committed"]
    assert "hook_denied" in outcome.payload["output"]
    await run.pool.close()


async def test_every_handler_is_journaled_in_order(tmp_path: Path) -> None:
    async def first(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.ok()

    async def second(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.deny("第二个拒绝")

    async def third(hook: Any, ctx: Any) -> HookDecision:
        raise AssertionError("拒绝之后不应再运行")

    run = _Run(tmp_path)
    await run.start(_write_then_done(), hooks=_hooks(pre_tool_use=[first, second, third]))

    await run.ask()

    records = await run.hooks()
    assert [(r.handler_index, r.allow) for _, r in records] == [(0, True), (1, False)]
    assert [e.operation_id[-7:] for e, _ in records] == [":hook:0", ":hook:1"]
    await run.pool.close()


async def test_output_override_is_journaled_and_reaches_the_model(tmp_path: Path) -> None:
    async def redact(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.ok(output_override="[已脱敏]")

    run = _Run(tmp_path)
    await run.start(_write_then_done(), hooks=_hooks(post_tool_use=redact))

    await run.ask()

    ((envelope, record),) = await run.hooks()
    assert record.hook_kind == "post_tool_use"
    assert record.overrides == {"output_override": "[已脱敏]"}
    envelopes = await run.journal()
    (outcome,) = [e for e in envelopes if e.record_type == "tool_outcome_committed"]
    assert outcome.payload["output"] == "[已脱敏]"
    assert envelope.seq < outcome.seq
    assert run.sim.ledger.function_call_output_text("c1") == "[已脱敏]"
    await run.pool.close()


async def test_failing_handler_is_journaled(tmp_path: Path) -> None:
    async def broken(hook: Any, ctx: Any) -> HookDecision:
        raise RuntimeError("policy service down")

    run = _Run(tmp_path)
    await run.start(_write_then_done(), hooks=_hooks(pre_tool_use=broken))

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    assert run.executed == []
    ((_, record),) = await run.hooks()
    assert (record.allow, record.reason, record.error_class) == (
        False, "hook_error", "RuntimeError",
    )
    # 异常原文不进 Journal
    assert "policy service down" not in repr(record)
    await run.pool.close()


async def test_pre_turn_denial_is_journaled(tmp_path: Path) -> None:
    async def deny(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.deny("超出配额")

    run = _Run(tmp_path)
    await run.start([SimTurn(text="不会走到")], hooks=_hooks(pre_turn=deny))

    events = await run.ask()

    assert events[-1].msg.kind == "turn_failed"
    ((envelope, record),) = await run.hooks()
    assert (record.hook_kind, record.allow, record.reason) == ("pre_turn", False, "超出配额")
    assert envelope.operation_id.endswith(":turn:0:hook:0")
    assert not [e for e in await run.journal() if e.record_type == "llm_request_committed"]
    await run.pool.close()


async def test_turn_level_hooks_share_one_numbering(tmp_path: Path) -> None:
    """engine 层的 pre_turn / post_turn 与 runner 层的 hook 在同一 turn 内连续编号。"""
    seen: list[str] = []

    def record(name: str) -> Any:
        async def handler(hook: Any, ctx: Any) -> HookDecision:
            seen.append(name)
            return HookDecision.ok(text_override="归一后的回答") if name == "outbound" else (
                HookDecision.ok()
            )
        return handler

    run = _Run(tmp_path)
    await run.start(_write_then_done(), hooks=_hooks(
        pre_turn=record("pre_turn"), pre_tool_use=record("pre_tool"),
        post_tool_use=record("post_tool"), outbound_message=record("outbound"),
        post_turn=record("post_turn"),
    ))

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    outbound = next(e.msg.data for e in events if e.msg.kind == "outbound_message")
    assert outbound["text"] == "归一后的回答"
    # post_turn 在 turn_completed 事件之后触发：等它的裁决落账再关
    await wait_for_condition(lambda: "post_turn" in seen)
    records = await run.hooks()
    for _ in range(200):
        if len(records) == 5:
            break
        await asyncio.sleep(0.01)
        records = await run.hooks()
    await run.pool.close()
    assert [r.hook_kind for _, r in records] == [
        "pre_turn", "pre_tool_use", "post_tool_use", "outbound_message", "post_turn",
    ]
    assert [int(e.operation_id.rsplit(":", 1)[1]) for e, _ in records] == [0, 1, 2, 3, 4]
    assert records[3][1].overrides == {"text_override": "归一后的回答"}
    assert seen == ["pre_turn", "pre_tool", "post_tool", "outbound", "post_turn"]


async def test_second_turn_restarts_the_numbering(tmp_path: Path) -> None:
    async def allow(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.ok()

    run = _Run(tmp_path)
    await run.start(
        [*_write_then_done("c1"), *_write_then_done("c2")],
        hooks=_hooks(pre_tool_use=allow),
    )

    await run.ask("一")
    await run.ask("二")

    operations = [e.operation_id for e, _ in await run.hooks()]
    assert [op.split(":turn:")[1] for op in operations] == ["0:hook:0", "1:hook:0"]
    assert len(set(operations)) == 2
    await run.pool.close()


async def test_child_skill_hooks_are_journaled_on_the_child_thread(tmp_path: Path) -> None:
    async def allow(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.ok()

    run = _Run(tmp_path)
    await run.start(
        [
            SimTurn(text="派发", tool_calls=[
                _call("call_skill", "tc_leaf", reason="需要", skill_id="leaf", args={}),
            ]),
            SimTurn(text="总结"),
        ],
        leaf=_write_then_done("c_child"),
        hooks=_hooks(pre_tool_use=allow, pre_skill_dispatch=allow),
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    records = await run.hooks()
    by_subject = {
        (r.hook_kind, r.subject.get("call_id") or r.subject.get("target_skill_id")): e
        for e, r in records
    }
    root = run.engine.thread_id
    assert by_subject[("pre_tool_use", "tc_leaf")].thread_id == root
    assert by_subject[("pre_skill_dispatch", "leaf")].thread_id == root
    child = by_subject[("pre_tool_use", "c_child")]
    assert child.thread_id != root
    assert child.operation_id.startswith(f"{child.thread_id}:")
    assert len({e.record_id for e, _ in records}) == len(records)
    await run.pool.close()


async def test_journal_verifies_and_resume_ignores_gate_records(tmp_path: Path) -> None:
    async def allow(hook: Any, ctx: Any) -> HookDecision:
        return HookDecision.ok()

    run = _Run(tmp_path)
    await run.start(_write_then_done(), hooks=_hooks(pre_tool_use=allow))
    await run.ask()
    before = list(run.engine.history_snapshot())
    thread_id = run.engine.thread_id
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start(
        _write_then_done("c2"), hooks=_hooks(pre_tool_use=allow), resume_thread_id=thread_id,
    )

    assert list(resumed.engine.history_snapshot()) == before
    events = await resumed.ask("继续")
    assert events[-1].msg.kind == "turn_completed"
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
    assert len({e.record_id for e, _ in await resumed.hooks()}) == 2


# ====================================================================
# 权限
# ====================================================================


def _guarded_then_done(call_id: str = "g1") -> list[SimTurn]:
    return [
        SimTurn(text="申请", tool_calls=[_call("guarded", call_id, key="k")]),
        SimTurn(text="完成"),
    ]


async def test_rule_decision_is_journaled(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(_guarded_then_done(), permission_policy=PermissionPolicy(
        rules=[PermissionRule(
            scope="tool_use", target_pattern="guarded", mode="deny", reason="冻结期",
        )],
        default_mode="allow",
    ))

    await run.ask()

    ((envelope, record),) = await run.permissions()
    assert (record.scope, record.target, record.call_id) == ("tool_use", "guarded", "g1")
    assert (record.granted, record.mode, record.decision_reason) == (False, "deny", "冻结期")
    assert record.request_reason == "需要写入"
    assert record.call_chain == ("entry",)
    assert record.request_metadata == {"args": {"key": "k"}, "call_id": "g1"}
    assert envelope.operation_id.endswith(":turn:0:permission:0")
    envelopes = await run.journal()
    assert _seq(envelopes, "tool_intent_committed") < envelope.seq
    assert envelope.seq < _seq(envelopes, "tool_outcome_committed")
    assert "permission_denied" in (run.sim.ledger.function_call_output_text("g1") or "")
    await run.pool.close()


async def test_human_approval_and_minted_grant_are_journaled(tmp_path: Path) -> None:
    asked: list[PermissionRequest] = []

    async def approve(request: PermissionRequest) -> PermissionDecision:
        asked.append(request)
        return PermissionDecision.allow(
            reason="值班同意", remember="session",
            grant=PermissionGrant(scope="tool_use", target_pattern="guarded", max_uses=3),
        )

    run = _Run(tmp_path)
    await run.start(
        [*_guarded_then_done("g1"), *_guarded_then_done("g2")],
        permission_policy=PermissionPolicy(
            default_mode="ask", prompter=CallbackPrompter(approve),
        ),
    )

    await run.ask("一")
    await run.ask("二")

    first, second = (record for _, record in await run.permissions())
    assert (first.granted, first.decision_reason, first.remember_until) == (
        True, "值班同意", "session",
    )
    assert first.minted_grant is not None
    assert first.minted_grant["target_pattern"] == "guarded"
    assert first.minted_grant["max_uses"] == 3
    # 第二次命中已签发的授权：没有再问人，裁决照样落账
    assert len(asked) == 1
    assert second.granted is True
    assert second.decision_reason.startswith("grant:")
    assert second.minted_grant is None
    await run.pool.close()


async def test_skill_dispatch_permission_is_journaled(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        [
            SimTurn(text="派发", tool_calls=[
                _call("call_skill", "tc_leaf", reason="需要它", skill_id="leaf", args={}),
            ]),
            SimTurn(text="换个办法"),
        ],
        permission_policy=PermissionPolicy(
            rules=[PermissionRule(scope="skill_dispatch", target_pattern="leaf", mode="deny")],
            default_mode="allow",
        ),
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    ((_, record),) = await run.permissions()
    assert (record.scope, record.target, record.granted) == ("skill_dispatch", "leaf", False)
    assert record.request_reason == "需要它"
    assert record.call_id == "tc_leaf"
    assert not [e for e in events if e.msg.kind == "skill_dispatched"]
    await run.pool.close()


async def test_child_skill_permission_follows_the_subagent_mode(tmp_path: Path) -> None:
    """子 skill 按 auto_deny 裁决：记在子 thread 名下，且没有去问审批人。"""
    asked: list[PermissionRequest] = []

    async def approve(request: PermissionRequest) -> PermissionDecision:
        asked.append(request)
        return PermissionDecision.allow()

    run = _Run(tmp_path)
    await run.start(
        [
            SimTurn(text="派发", tool_calls=[
                _call("call_skill", "tc_leaf", reason="需要", skill_id="leaf", args={}),
            ]),
            SimTurn(text="总结"),
        ],
        leaf=_guarded_then_done("g_child"),
        leaf_tool="guarded",
        permission_policy=PermissionPolicy(
            rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
            default_mode="ask", prompter=CallbackPrompter(approve),
        ),
        dispatch_policy=DispatchPolicy(subagent_approval_mode="auto_deny"),
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    assert asked == []
    records = await run.permissions()
    dispatch, child = records
    assert dispatch[1].scope == "skill_dispatch"
    assert dispatch[0].thread_id == run.engine.thread_id
    assert (child[1].scope, child[1].granted, child[1].decision_reason) == (
        "tool_use", False, "subagent_auto_deny",
    )
    assert child[0].thread_id != run.engine.thread_id
    await run.pool.close()


async def test_request_that_cannot_be_journaled_is_denied(tmp_path: Path) -> None:
    asked: list[PermissionRequest] = []

    async def approve(request: PermissionRequest) -> PermissionDecision:
        asked.append(request)
        return PermissionDecision.allow()

    run = _Run(tmp_path)
    await run.start(
        _guarded_then_done(),
        guarded=_guarded({"handle": object()}),
        permission_policy=PermissionPolicy(
            default_mode="ask", prompter=CallbackPrompter(approve),
        ),
    )

    await run.ask()

    assert asked == []
    ((_, record),) = await run.permissions()
    assert (record.granted, record.decision_reason) == (
        False, "audit_permission_request_not_canonical",
    )
    assert record.request_metadata == {}
    assert "permission_denied" in (run.sim.ledger.function_call_output_text("g1") or "")
    await run.pool.close()


async def test_policy_attributes_stay_reachable(tmp_path: Path) -> None:
    """绑定只接管 check：签发授权等其余入口仍作用在业务自己的策略上。"""
    policy = PermissionPolicy(default_mode="ask")
    run = _Run(tmp_path)
    await run.start(_guarded_then_done(), permission_policy=policy)
    policy.issue_grant(PermissionGrant(scope="tool_use", target_pattern="guarded"))

    await run.ask()

    ((_, record),) = await run.permissions()
    assert record.granted is True
    assert record.decision_reason.startswith("grant:")
    await run.pool.close()
