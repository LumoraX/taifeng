"""call_skill 自身因审批挂起后的恢复（ADR 0089）。

派发类工具依赖 TurnRunner 提供的调用栈与调度器。获批后它在续跑的 turn 内重跑，
而不是在 engine 层用最小上下文执行。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import Resume
from taifeng.loop.turn_helpers import _history_orphan_call_ids
from taifeng.permission.types import PermissionPolicy, SuspendingPrompter
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path

_USAGE = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
_TERMINAL = ("turn_completed", "turn_failed", "turn_suspended", "suspension_resolve_rejected")

_ENTRY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [alpha, beta]
tool_names: []
max_call_depth: 6
---
# entry
"""

_CHILD = """---
name: {name}
description: {name} 处理具体子任务
version: 1.0.0
type: atomic
---
# {name}
"""


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "entry").mkdir(parents=True)
    (root / "entry" / "SKILL.md").write_text(_ENTRY, encoding="utf-8")
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(_CHILD.format(name=name), encoding="utf-8")
    return root


def _call(skill_id: str, call_id: str) -> dict[str, str]:
    arguments = json.dumps({"reason": "需要它", "skill_id": skill_id, "args": {}})
    return {"id": call_id, "name": "call_skill", "arguments": arguments}


class _Run:
    """一次测试用到的 pool / engine / client。"""

    def __init__(self) -> None:
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.client: SimClient

    async def start(self, tmp_path: Path, turns: list[SimTurn]) -> None:
        self.client = SimClient(turns=turns)
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
            model_client=self.client, compressors=[],
            permission_policy=PermissionPolicy(
                default_mode="ask", prompter=SuspendingPrompter()
            ),
        )
        self.engine = await self.pool.get_or_create(session_id="s", entry_skill_id="entry")

    async def submit(self, op: Any) -> list[taifeng.EventMsg]:
        """提交并收集事件，直到最外层 turn 终结、挂起或裁决被拒。"""
        holder: list[str] = []
        events: list[taifeng.EventMsg] = []

        async def collect() -> None:
            depth = 0
            async for event in self.engine.subscribe_all():
                if not holder or event.submission_id != holder[0]:
                    continue
                events.append(event)
                if event.msg.kind == "skill_dispatched":
                    depth += 1
                elif event.msg.kind == "skill_returned":
                    depth -= 1
                if event.msg.kind in _TERMINAL and depth <= 0:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        holder.append(await self.engine.submit(op))
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
        return events

    def pending(self) -> list[Any]:
        record = self.engine._find_active_suspension()  # noqa: SLF001
        assert record is not None
        return list(record.pending)

    async def resume(self, **resolutions: Any) -> list[taifeng.EventMsg]:
        return await self.submit(Resume(
            thread_id=self.engine.thread_id, resolutions=resolutions,
        ))


def _dispatched(events: list[taifeng.EventMsg]) -> list[str]:
    return [e.msg.data["skill_id"] for e in events if e.msg.kind == "skill_dispatched"]


async def test_approved_call_skill_is_dispatched_after_resume(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="派发", tool_calls=[_call("alpha", "c1")], usage=_USAGE),
        SimTurn(text="alpha 完成", usage=_USAGE),
        SimTurn(text="总结", usage=_USAGE),
    ])

    suspended = await run.submit(taifeng.UserMessage(text="开始"))
    assert suspended[-1].msg.kind == "turn_suspended"
    (pending,) = run.pending()
    assert pending.detail["scope"] == "skill_dispatch"
    assert pending.related_call_id == "c1"

    resumed = await run.resume(**{pending.request_id: {"granted": True}})

    assert resumed[-1].msg.kind == "turn_completed", [e.msg.kind for e in resumed]
    assert _dispatched(resumed) == ["alpha"]
    assert "alpha 完成" in (run.client.ledger.function_call_output_text("c1") or "")
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    await run.pool.close()


async def test_denied_call_skill_is_not_dispatched(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(text="派发", tool_calls=[_call("alpha", "c1")], usage=_USAGE),
        SimTurn(text="换个办法", usage=_USAGE),
    ])

    await run.submit(taifeng.UserMessage(text="开始"))
    (pending,) = run.pending()
    resumed = await run.resume(**{pending.request_id: {"granted": False, "reason": "不行"}})

    assert resumed[-1].msg.kind == "turn_completed"
    assert _dispatched(resumed) == []
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    await run.pool.close()


async def test_two_approved_dispatches_both_run(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(
            text="派发两个", tool_calls=[_call("alpha", "c1"), _call("beta", "c2")],
            usage=_USAGE,
        ),
        SimTurn(text="alpha 完成", usage=_USAGE),
        SimTurn(text="beta 完成", usage=_USAGE),
        SimTurn(text="总结", usage=_USAGE),
    ])

    suspended = await run.submit(taifeng.UserMessage(text="开始"))
    assert suspended[-1].msg.kind == "turn_suspended"
    pending = run.pending()
    assert sorted(p.related_call_id for p in pending) == ["c1", "c2"]

    resumed = await run.resume(**{p.request_id: {"granted": True} for p in pending})

    assert resumed[-1].msg.kind == "turn_completed", [e.msg.kind for e in resumed]
    assert _dispatched(resumed) == ["alpha", "beta"]
    assert "alpha 完成" in (run.client.ledger.function_call_output_text("c1") or "")
    assert "beta 完成" in (run.client.ledger.function_call_output_text("c2") or "")
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    await run.pool.close()


async def test_partial_approval_of_dispatch_is_rejected(tmp_path: Path) -> None:
    """只批其中一个派发：续跑要等 record 结清，批准无处落地，显式拒绝且不改动状态。"""
    run = _Run()
    await run.start(tmp_path, [
        SimTurn(
            text="派发两个", tool_calls=[_call("alpha", "c1"), _call("beta", "c2")],
            usage=_USAGE,
        ),
        SimTurn(text="alpha 完成", usage=_USAGE),
        SimTurn(text="总结", usage=_USAGE),
    ])

    await run.submit(taifeng.UserMessage(text="开始"))
    pending = {p.related_call_id: p for p in run.pending()}
    before = len(run.engine.history_snapshot())

    rejected = await run.resume(**{pending["c1"].request_id: {"granted": True}})

    assert rejected[-1].msg.kind == "suspension_resolve_rejected"
    assert rejected[-1].msg.data["reason"] == "dispatch_approval_requires_full_resolution"
    assert rejected[-1].msg.data["detail"] == {"call_ids": ["c1"]}
    assert len(run.engine.history_snapshot()) == before
    assert len(run.pending()) == 2

    # 拒绝一个、批准一个：一次结清，批准的那个照常派发
    resumed = await run.resume(**{
        pending["c1"].request_id: {"granted": True},
        pending["c2"].request_id: {"granted": False, "reason": "不需要"},
    })

    assert resumed[-1].msg.kind == "turn_completed", [e.msg.kind for e in resumed]
    assert _dispatched(resumed) == ["alpha"]
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    await run.pool.close()
