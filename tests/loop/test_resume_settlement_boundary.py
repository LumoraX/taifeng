"""公开 Resume 在 root、child、spawn 上均拒绝已核销待办。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.suspend.record import SuspensionRecord
from tests.conftest import wait_for_condition
from tests.loop.test_spawn_direct_resume_boundaries import (
    _ask_policy,
    _gated_danger_tool,
    _skill,
    _write_skills,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("topology", ["root", "child", "spawn"])
async def test_settled_permission_cannot_execute_twice(tmp_path: Path, threads_dir: Path, topology: str) -> None:
    """部分批准后重发已核销项须先拒绝，不能再执行或回填工具。"""
    skills = _write_skills(tmp_path / "skills", {
        "host": _skill("host", entry=True, children=["worker"]),
        "worker": _skill("worker", entry=topology == "root", tools=["danger"]),
    })
    client = RoutingSimClient(routes={
        "HOST_MARK": [SimTurn(tool_calls=[{"id": "dispatch", "name": "call_skill",
            "arguments": '{"skill_id":"worker","reason":"test"}'}]), SimTurn(text="done")],
        "WORKER_MARK": [SimTurn(tool_calls=[
            {"id": "a", "name": "danger", "arguments": "{}"},
            {"id": "b", "name": "danger", "arguments": "{}"}]), SimTurn(text="done")],
    })
    pool = await taifeng.EnginePool.create(skills_dir=skills, threads_dir=threads_dir,
        model_client=client, compressors=[], extra_tools=[await _gated_danger_tool()],
        permission_policy=_ask_policy())
    engine = await pool.get_or_create(session_id="test", entry_skill_id="worker" if topology == "root" else "host")
    events: list[taifeng.EventMsg] = []
    async def collect() -> None:
        """提交前订阅，结论不能因读取晚而丢失。"""
        async for event in engine.subscribe_all():
            events.append(event)
    reader = asyncio.create_task(collect())
    await asyncio.sleep(0)
    try:
        if topology == "spawn":
            handle = await engine.spawn_skill(skill_id="worker", args={}, reason="test")
            thread = handle["child_thread_id"]
        else:
            await engine.submit(taifeng.UserMessage(text="go"))
            thread = engine.thread_id
        await wait_for_condition(lambda: any(e.msg.kind == "turn_suspended" for e in events))
        suspended = [e for e in events if e.msg.kind == "turn_suspended" and len(e.msg.data.get("pending", [])) == 2][-1]
        thread = str(suspended.msg.data["thread_id"])
        items = [item async for item in await pool.store.load_thread(thread)]
        record = SuspensionRecord.from_item([item for item in items if item.kind == "suspension"][-1])
        request = sorted(record.request_ids())[0]
        answer = {request: {"granted": True}}
        first = await engine.submit(taifeng.Resume(thread_id=thread, resolutions=answer))
        await wait_for_condition(lambda: any(e.submission_id == first and e.msg.kind == "suspension_partially_resolved" for e in events))
        repeat = await engine.submit(taifeng.Resume(thread_id=thread, resolutions=answer))
        await wait_for_condition(lambda: any(e.submission_id == repeat and e.msg.kind in {"suspension_partially_resolved", "suspension_resolve_rejected"} for e in events))
        result = [e for e in events if e.submission_id == repeat][-1]
        assert result.msg.kind == "suspension_resolve_rejected"
        assert result.msg.data["reason"] == "request_already_resolved"
        after = [item async for item in await pool.store.load_thread(thread)]
        outputs = [item for item in after if item.kind == "function_call_output"]
        assert len(outputs) == 1
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await pool.close()


async def test_root_system_abort_has_explicit_non_resuming_conclusion(tmp_path: Path, threads_dir: Path) -> None:
    """资源挂起中止必须明确不再续跑，调用方才能结束本次事件流。"""
    from taifeng.llm.providers.sim import SimClient
    from taifeng.loop.failure_policy import SuspendByDefaultPolicy
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
    async def tick(args: dict, ctx: ToolContext) -> ToolResult:
        """消耗一次工具迭代。"""
        return ToolResult.ok("ok")
    skills = _write_skills(tmp_path / "abort", {"worker": _skill("worker", entry=True, tools=["tick"])})
    client = SimClient(turns=[SimTurn(tool_calls=[{"id": "tick", "name": "tick", "arguments": "{}"}])])
    pool = await taifeng.EnginePool.create(skills_dir=skills, threads_dir=threads_dir,
        model_client=client, compressors=[], max_iterations=1, failure_policy=SuspendByDefaultPolicy(),
        extra_tools=[ToolSpec("tick", "一次操作", {"type": "object", "properties": {}}, tick)])
    engine = await pool.get_or_create(session_id="abort", entry_skill_id="worker")
    try:
        first = await engine.submit(taifeng.UserMessage(text="go"))
        async for event in engine.subscribe(first):
            if event.msg.kind == "turn_suspended":
                pending = event.msg.data["pending"]
                break
        assert pending[0]["reason"] == "resource_limit"
        resumed = await engine.submit(taifeng.Resume(thread_id=engine.thread_id,
            resolutions={pending[0]["request_id"]: {"action": "abort"}}))
        async for event in engine.subscribe(resumed):
            if event.msg.kind == "suspension_resolved":
                assert event.msg.data.get("aborted") is True
                break
    finally:
        await pool.close()
