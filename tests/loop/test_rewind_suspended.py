"""挂起状态下的 rewind（ADR 0080）。

turn 挂起等人时，人可以不回答而改为回到之前的某个节点重来：挂起随截断一并作废，
旧的待答请求不能再被 Resume。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.loop.submission import Resume, Rewind
from taifeng.loop.turn_helpers import _history_orphan_call_ids
from taifeng.permission.types import (
    PermissionPolicy,
    PermissionRequest,
    SuspendingPrompter,
)
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.suspend.record import SuspensionRecord

_ENTRY = """---
name: operator
description: 调用需审批工具的入口
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [danger, plain]
max_call_depth: 2
---
# 入口
按需调用工具。
"""
_TERMINAL = (
    "turn_completed", "turn_failed", "turn_suspended",
    "rewind_rejected", "suspension_resolve_rejected",
)


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "operator").mkdir(parents=True, exist_ok=True)
    (root / "operator" / "SKILL.md").write_text(_ENTRY, encoding="utf-8")
    return root


def _danger(executed: list[dict[str, Any]]) -> ToolSpec:
    """需审批的工具：放行后才记录执行。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        policy = ctx.extras["permission_policy"]
        await policy.check(PermissionRequest.for_tool_call(
            "danger", args,
            thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("root",),
            extra_metadata={"call_id": ctx.call_id},
        ))
        executed.append(dict(args))
        return ToolResult.ok(f"danger done {args.get('target', '')}".strip())

    return ToolSpec(
        name="danger",
        description="需审批的工具",
        input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        handler=handler,
        parallel_safe=True,
    )


def _plain(executed: list[str]) -> ToolSpec:
    """不需要审批的工具。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        executed.append(ctx.call_id)
        return ToolResult.ok("plain done")

    return ToolSpec(
        name="plain",
        description="普通工具",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        parallel_safe=True,
    )


class _Run:
    """一次测试用到的 pool / engine 与执行侧录。"""

    def __init__(self) -> None:
        self.danger: list[dict[str, Any]] = []
        self.plain: list[str] = []
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.client: SimClient

    async def start(self, tmp_path: Path, turns: list[SimTurn], **kwargs: Any) -> None:
        self.client = SimClient(turns=turns)
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
            model_client=self.client, compressors=[],
            extra_tools=[_danger(self.danger), _plain(self.plain)],
            permission_policy=PermissionPolicy(
                default_mode="ask", prompter=SuspendingPrompter()
            ),
            max_parallel_tool_calls=3,
        )
        self.engine = await self.pool.get_or_create(
            session_id="s", entry_skill_id="operator", **kwargs
        )

    async def drain(self, sub_id: str) -> list[taifeng.EventMsg]:
        """消费该 submission 的事件到终结（含挂起与拒绝）；超时即失败，不无限等待。"""

        async def collect() -> list[taifeng.EventMsg]:
            events: list[taifeng.EventMsg] = []
            async for ev in self.engine.subscribe_all():
                if ev.submission_id != sub_id:
                    continue
                events.append(ev)
                if ev.msg.kind in _TERMINAL:
                    return events
            raise AssertionError("stream ended before a terminal event")

        return await asyncio.wait_for(collect(), timeout=GUARD_TIMEOUT_SECONDS)

    async def submit(self, op: Any) -> list[taifeng.EventMsg]:
        return await self.drain(await self.engine.submit(op))

    async def suspend(self, text: str = "go") -> SuspensionRecord:
        """跑到挂起，返回活跃的挂起 record。"""
        events = await self.submit(taifeng.UserMessage(text=text))
        assert events[-1].msg.kind == "turn_suspended"
        await wait_for_condition(lambda: self.engine.rewind_nodes(), message="节点表未落地")
        record = self.engine._find_active_suspension()  # noqa: SLF001
        assert record is not None
        return record

    def node(self, call_id: str) -> Any:
        return next(n for n in self.engine.rewind_nodes() if n.call_id == call_id)


def _danger_call(call_id: str, target: str = "x") -> dict[str, str]:
    return {"id": call_id, "name": "danger", "arguments": f'{{"target": "{target}"}}'}


def _rewound(events: list[taifeng.EventMsg]) -> dict[str, Any]:
    return next(dict(ev.msg.data) for ev in events if ev.msg.kind == "turn_rewound")


async def test_re_reason_discards_the_suspension(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="要动手了", tool_calls=[_danger_call("d1")]),
        SimTurn(text="换个不需要审批的办法"),
    ])
    record = await run.suspend()

    events = await run.submit(Rewind(node_id="t1:it1", mode="re_reason"))

    assert events[-1].msg.kind == "turn_completed"
    rewound = _rewound(events)
    assert rewound["discarded_suspension"] == record.record_id
    assert run.engine._find_active_suspension() is None  # noqa: SLF001
    history = run.engine.history_snapshot()
    assert not [i for i in history if i.kind == "suspension"]
    assert _history_orphan_call_ids(list(history)) == set()
    assert run.danger == []
    # 旧的待答请求不能再被 Resume
    resumed = await run.submit(Resume(
        thread_id=run.engine.thread_id,
        resolutions={record.pending[0].request_id: {"granted": True}},
    ))
    assert resumed[-1].msg.kind == "suspension_resolve_rejected"
    assert run.danger == []
    await run.pool.close()


async def test_retry_tool_on_suspended_call_asks_again(tmp_path: Path) -> None:
    """对挂起的调用做 retry_tool：换参重跑，重新挂起等人；回答后完成。"""
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="要动手了", tool_calls=[_danger_call("d1", "prod")]),
        SimTurn(text="完成"),
    ])
    first = await run.suspend()

    events = await run.submit(Rewind(
        node_id=run.node("d1").node_id, mode="retry_tool", new_args={"target": "staging"},
    ))

    assert events[-1].msg.kind == "turn_suspended"
    assert _rewound(events)["discarded_suspension"] == first.record_id
    second = run.engine._find_active_suspension()  # noqa: SLF001
    assert second is not None
    assert second.record_id != first.record_id
    assert second.pending[0].related_call_id == "d1"
    assert run.danger == []

    resumed = await run.submit(Resume(
        thread_id=run.engine.thread_id,
        resolutions={second.pending[0].request_id: {"granted": True}},
    ))

    assert resumed[-1].msg.kind == "turn_completed"
    assert run.danger == [{"target": "staging"}]
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    await run.pool.close()


async def test_retry_tool_on_settled_sibling_of_suspended_call_is_rejected(
    tmp_path: Path,
) -> None:
    """同批里还有别的调用在等人：retry_tool 会让它们永远悬空，显式拒绝且不改动任何状态。"""
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="两件事", tool_calls=[
            {"id": "p1", "name": "plain", "arguments": "{}"}, _danger_call("d1"),
        ]),
        SimTurn(text="完成"),
    ])
    record = await run.suspend()
    before = [i.id for i in run.engine.history_snapshot()]

    events = await run.submit(Rewind(node_id=run.node("p1").node_id, mode="retry_tool"))

    assert events[-1].msg.kind == "rewind_rejected"
    assert events[-1].msg.data["reason"] == "sibling_calls_pending"
    assert [i.id for i in run.engine.history_snapshot()] == before
    assert run.plain == ["p1"]
    active = run.engine._find_active_suspension()  # noqa: SLF001
    assert active is not None and active.record_id == record.record_id
    # 挂起仍可正常回答
    resumed = await run.submit(Resume(
        thread_id=run.engine.thread_id,
        resolutions={record.pending[0].request_id: {"granted": True}},
    ))
    assert resumed[-1].msg.kind == "turn_completed"
    await run.pool.close()


async def test_re_reason_on_batch_with_pending_sibling_is_allowed(tmp_path: Path) -> None:
    """回到采样前重来不留任何调用：同批有几个在等人都可以。"""
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="两件事", tool_calls=[
            {"id": "p1", "name": "plain", "arguments": "{}"}, _danger_call("d1"),
        ]),
        SimTurn(text="都不做了"),
    ])
    await run.suspend()

    events = await run.submit(Rewind(node_id=run.node("p1").node_id, mode="re_reason"))

    assert events[-1].msg.kind == "turn_completed"
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    assert not [i for i in run.engine.history_snapshot() if i.kind == "function_call"]
    await run.pool.close()


async def test_rewind_to_earlier_turn_while_suspended(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="第一轮回答"),
        SimTurn(text="要动手了", tool_calls=[_danger_call("d1")]),
        SimTurn(text="第一轮重答"),
    ])
    done = await run.submit(taifeng.UserMessage(text="第一问"))
    assert done[-1].msg.kind == "turn_completed"
    record = await run.suspend("第二问")

    events = await run.submit(Rewind(node_id="t1:it1", mode="re_reason"))

    assert events[-1].msg.kind == "turn_completed"
    assert _rewound(events)["discarded_suspension"] == record.record_id
    users = [i.payload["text"] for i in run.engine.history_snapshot() if i.kind == "user_message"]
    assert users == ["第一问"]
    await run.pool.close()


async def test_cold_reload_after_suspended_rewind_has_no_suspension(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="要动手了", tool_calls=[_danger_call("d1")]),
        SimTurn(text="换个办法"),
    ])
    await run.suspend()
    events = await run.submit(Rewind(node_id="t1:it1", mode="re_reason"))
    assert events[-1].msg.kind == "turn_completed"
    thread_id = run.engine.thread_id
    hot = [(i.kind, i.id) for i in run.engine.history_snapshot()]
    await run.pool.close()

    cold = _Run()
    await cold.start(tmp_path, [], resume_thread_id=thread_id)

    assert [(i.kind, i.id) for i in cold.engine.history_snapshot()] == hot
    assert cold.engine._find_active_suspension() is None  # noqa: SLF001
    await cold.pool.close()


async def test_rewind_without_suspension_reports_no_discard(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [SimTurn(text="答"), SimTurn(text="重答")])
    done = await run.submit(taifeng.UserMessage(text="问"))
    assert done[-1].msg.kind == "turn_completed"
    await wait_for_condition(lambda: run.engine.rewind_nodes(), message="节点表未落地")

    events = await run.submit(Rewind(node_id="t1:it1", mode="re_reason"))

    assert _rewound(events)["discarded_suspension"] is None
    await run.pool.close()


# ------------------------------------------------------------------
# spawn 子 thread
# ------------------------------------------------------------------

_HOST = """---
name: host
description: 宿主入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [worker]
max_call_depth: 3
---
# 宿主 HOST_MARK
派发工作者。
"""
_WORKER = """---
name: worker
description: 调用需审批工具的工作者
version: 1.0.0
type: composite
model: mock-model
tool_names: [danger]
max_call_depth: 2
---
# 工作者 WORKER_MARK
按需调用 danger。
"""


@pytest.mark.asyncio
async def test_suspended_spawn_can_be_rewound(tmp_path: Path) -> None:
    skills = tmp_path / "spawn_skills"
    for name, body in (("host", _HOST), ("worker", _WORKER)):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(body, encoding="utf-8")
    executed: list[dict[str, Any]] = []
    client = RoutingSimClient(routes={
        "WORKER_MARK": [
            SimTurn(text="要动手了", tool_calls=[_danger_call("d1")]),
            SimTurn(text="换个办法完成"),
        ],
        "HOST_MARK": [SimTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp_path / "threads", model_client=client,
        compressors=[], extra_tools=[_danger(executed)],
        permission_policy=PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter()),
    )
    engine = await pool.get_or_create(session_id="sp", entry_skill_id="host")
    out = await engine.spawn_skill(skill_id="worker", args={}, reason="t")
    handle_id, child = out["handle_id"], out["child_thread_id"]
    await wait_for_condition(
        lambda: engine.spawn_status([handle_id])[handle_id]["status"] == "suspended",
        message="spawn 未挂起",
    )

    sub_id = await engine.submit(Rewind(node_id="t1:it1", thread_id=child))

    async def until_rewound() -> dict[str, Any]:
        async for ev in engine.subscribe_all():
            if ev.submission_id != sub_id:
                continue
            assert ev.msg.kind != "rewind_rejected", ev.msg.data
            if ev.msg.kind == "turn_rewound":
                return dict(ev.msg.data)
        raise AssertionError("stream ended before turn_rewound")

    rewound = await asyncio.wait_for(until_rewound(), timeout=GUARD_TIMEOUT_SECONDS)

    assert rewound["thread_id"] == child
    assert rewound["discarded_suspension"]
    await wait_for_condition(
        lambda: engine.spawn_status([handle_id])[handle_id]["status"] == "done",
        message="重推后 spawn 未完成",
    )
    assert engine.spawn_status([handle_id])[handle_id]["result"] == "换个办法完成"
    assert executed == []
    await pool.close()
