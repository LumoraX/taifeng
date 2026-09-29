"""spawn 拒绝原因分类（ADR 0078）。

detached spawn 的准入拒绝此前以普通异常冒出：模型看到 ``tool_error: ...``、
``data.reason == "exception"``，日志里是一段 traceback，事件流里没有拒绝事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.loop.spawn import SpawnLimitError, SpawnRejectedError
from taifeng.tool.builtins.spawn_skill import make_spawn_skill_tool
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

_ORCH = """---
name: spawn-orch
description: 分离发起子任务的入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [worker]
tool_names: [spawn_skill]
max_call_depth: 3
---
# SPAWN_ORCH_MARK
按需分离发起子任务。
"""
_WORKER = """---
name: worker
description: 子任务
version: 1.0.0
type: composite
entry: false
model: mock-model
tool_names: [hold_slot]
max_call_depth: 2
---
# WORKER_MARK
完成子任务。
"""
_OUTSIDER = _WORKER.replace("name: worker", "name: outsider").replace(
    "WORKER_MARK", "OUTSIDER_MARK"
)


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    """入口 + 白名单内的 worker + 白名单外的 outsider。"""
    root = tmp_path / "skills"
    for name, body in (("spawn-orch", _ORCH), ("worker", _WORKER), ("outsider", _OUTSIDER)):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _spawn_call(call_id: str, skill_id: str) -> dict[str, str]:
    arguments = json.dumps({"skill_id": skill_id, "reason": "并发分析", "args": {}})
    return {"id": call_id, "name": "spawn_skill", "arguments": arguments}


def _hold_tool(entered: asyncio.Event, release: asyncio.Event) -> ToolSpec:
    """长占工具：把 spawn 的子 turn 钉在 running，稳定占住配额。"""

    async def hold(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        entered.set()
        await release.wait()
        return ToolResult.ok("released")

    return ToolSpec(
        name="hold_slot",
        description="测试用：长占一个 spawn slot",
        input_schema={"type": "object", "properties": {}},
        handler=hold,
        timeout_seconds=30.0,
    )


async def _run_turn(
    engine: taifeng.AgentEngine,
) -> tuple[list[taifeng.EventMsg], str]:
    """跑一轮并收集该 submission 的全部事件；返回 (事件, 终态 kind)。"""
    events: list[taifeng.EventMsg] = []
    sub_id = await engine.submit(taifeng.UserMessage(text="开始"))
    async for ev in engine.subscribe(sub_id):
        events.append(ev)
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            return events, ev.msg.kind
    raise AssertionError("turn did not reach a terminal event")


def _tool_result(events: list[taifeng.EventMsg], call_id: str) -> dict[str, Any]:
    """取某个调用的 tool_call_completed 事件 data。"""
    for ev in events:
        if ev.msg.kind == "tool_call_completed" and ev.msg.data.get("call_id") == call_id:
            return dict(ev.msg.data)
    raise AssertionError(f"no tool_call_completed for {call_id}")


def _rejections(events: list[taifeng.EventMsg]) -> list[dict[str, Any]]:
    return [dict(ev.msg.data) for ev in events if ev.msg.kind == "skill_spawn_rejected"]


# ------------------------------------------------------------------
# engine API：异常带分类，兼容既有捕获方式
# ------------------------------------------------------------------


async def test_unknown_skill_raises_classified_error(skills: Path, tmp_path: Path) -> None:
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp_path / "threads",
        model_client=RoutingSimClient(routes={}), compressors=[])
    engine = await pool.get_or_create(session_id="s", entry_skill_id="spawn-orch")

    with pytest.raises(SpawnRejectedError) as raised:
        await engine.spawn_skill(skill_id="ghost", args={}, reason="x")

    assert isinstance(raised.value, ValueError)
    assert str(raised.value) == "unknown_skill: ghost"
    assert raised.value.reject_reason == "unknown_skill"
    assert raised.value.skill_id == "ghost"
    await pool.close()


async def test_policy_rejection_raises_classified_error(skills: Path, tmp_path: Path) -> None:
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp_path / "threads",
        model_client=RoutingSimClient(routes={}), compressors=[])
    engine = await pool.get_or_create(session_id="s", entry_skill_id="spawn-orch")

    with pytest.raises(SpawnRejectedError) as raised:
        await engine.spawn_skill(skill_id="outsider", args={}, reason="x")

    assert str(raised.value) == "dispatch_rejected: not_in_whitelist"
    assert raised.value.reject_reason == "not_in_whitelist"
    assert raised.value.skill_id == "outsider"
    assert raised.value.path == ("spawn-orch", "outsider")
    await pool.close()


def test_limit_error_exposes_reject_reason() -> None:
    assert SpawnLimitError("concurrent", 4).reject_reason == "spawn_limit_concurrent"
    assert SpawnLimitError("total", 9).reject_reason == "spawn_limit_total"


def test_limit_error_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        SpawnLimitError("weird", 1)


# ------------------------------------------------------------------
# spawn_skill 工具：模型看到分类后的结果，事件流有拒绝事件
# ------------------------------------------------------------------


async def _pool_with_tools(
    skills: Path, tmp_path: Path, client: RoutingSimClient, *tools: ToolSpec, **kwargs: Any,
) -> taifeng.EnginePool:
    return await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp_path / "threads", model_client=client,
        compressors=[], extra_tools=[make_spawn_skill_tool(), *tools], **kwargs)


@pytest.mark.parametrize(
    ("skill_id", "reason", "message"),
    [
        ("ghost", "unknown_skill", "spawn_rejected: unknown_skill"),
        ("outsider", "not_in_whitelist", "spawn_rejected: not_in_whitelist"),
    ],
)
async def test_tool_reports_classified_rejection(
    skills: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    skill_id: str, reason: str, message: str,
) -> None:
    client = RoutingSimClient(routes={
        "SPAWN_ORCH_MARK": [
            SimTurn(text="发起", tool_calls=[_spawn_call("sp-1", skill_id)]),
            SimTurn(text="改用别的办法"),
        ],
    })
    pool = await _pool_with_tools(skills, tmp_path, client)
    engine = await pool.get_or_create(session_id="s", entry_skill_id="spawn-orch")

    with caplog.at_level(logging.ERROR):
        events, kind = await _run_turn(engine)

    assert kind == "turn_completed"
    assert _tool_result(events, "sp-1")["is_error"] is True
    output = client.ledger.function_call_output_text("sp-1")
    assert output is not None and output.startswith(message)
    assert skill_id in output
    assert "tool_error" not in output
    # 预期内的拒绝不是工具故障：不打异常日志
    assert not [r for r in caplog.records if r.exc_info]
    assert _rejections(events) == [{
        "skill_id": skill_id,
        "call_id": "sp-1",
        "reason": reason,
        "origin": "spawn_skill",
        "path": ["spawn-orch", "outsider"] if reason == "not_in_whitelist" else [],
    }]
    await pool.close()


async def test_tool_reports_quota_rejection(skills: Path, tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    client = RoutingSimClient(routes={
        "SPAWN_ORCH_MARK": [
            SimTurn(text="发起两个", tool_calls=[
                _spawn_call("sp-1", "worker"), _spawn_call("sp-2", "worker"),
            ]),
            SimTurn(text="第二个没起来"),
        ],
        "WORKER_MARK": [
            SimTurn(text="长占", tool_calls=[
                {"id": "hold-1", "name": "hold_slot", "arguments": "{}"},
            ]),
            SimTurn(text="完成"),
        ],
    })
    pool = await _pool_with_tools(
        skills, tmp_path, client, _hold_tool(entered, release), max_concurrent_spawns=1,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="spawn-orch")

    events, kind = await _run_turn(engine)

    assert kind == "turn_completed"
    assert _tool_result(events, "sp-2")["is_error"] is True
    output = client.ledger.function_call_output_text("sp-2")
    assert output is not None
    assert output.startswith("spawn_rejected: spawn_limit_concurrent")
    assert _rejections(events) == [{
        "skill_id": "worker",
        "call_id": "sp-2",
        "reason": "spawn_limit_concurrent",
        "origin": "spawn_skill",
        "path": [],
        "limit_kind": "concurrent",
        "limit": 1,
    }]
    assert _tool_result(events, "sp-1")["is_error"] is False
    release.set()
    await wait_for_condition(
        lambda: engine._spawn_registry.snapshot()["active"] == 0  # noqa: SLF001
    )
    await pool.close()


# ------------------------------------------------------------------
# spawn_skill handler：结构化结果
# ------------------------------------------------------------------


class _Rejecting:
    """准入阶段即抛出给定异常的协调器。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def spawn_skill(self, **kwargs: Any) -> dict[str, str]:
        raise self._error


def _ctx(coordinator: object) -> ToolContext:
    from taifeng.loop.cancellation import CancellationToken

    return ToolContext(
        call_id="sp-9", cancel=CancellationToken(), thread_id="thr",
        extras={"spawn_coordinator": coordinator},
    )


_ARGS = {"skill_id": "worker", "reason": "并发分析", "args": {}}


async def test_handler_returns_structured_policy_rejection() -> None:
    error = SpawnRejectedError("cycle_detected", skill_id="worker", path=("a", "worker"))

    result = await make_spawn_skill_tool().handler(_ARGS, _ctx(_Rejecting(error)))

    assert result.is_error
    assert result.output == "spawn_rejected: cycle_detected (skill 'worker')"
    assert result.data == {
        "reason": "cycle_detected",
        "skill_id": "worker",
        "origin": "spawn_skill",
        "path": ["a", "worker"],
    }


async def test_handler_returns_structured_quota_rejection() -> None:
    result = await make_spawn_skill_tool().handler(
        _ARGS, _ctx(_Rejecting(SpawnLimitError("total", 1000)))
    )

    assert result.is_error
    assert result.output == (
        "spawn_rejected: spawn_limit_total (skill 'worker', total spawn limit 1000 reached)"
    )
    assert result.data == {
        "reason": "spawn_limit_total",
        "skill_id": "worker",
        "origin": "spawn_skill",
        "path": [],
        "limit_kind": "total",
        "limit": 1000,
    }


async def test_handler_lets_unexpected_errors_propagate() -> None:
    """准入拒绝以外的异常仍是工具故障，不伪装成拒绝。"""
    with pytest.raises(RuntimeError, match="engine not running"):
        await make_spawn_skill_tool().handler(
            _ARGS, _ctx(_Rejecting(RuntimeError("engine not running")))
        )


def test_rejected_error_refuses_unknown_reason() -> None:
    with pytest.raises(ValueError, match="unknown spawn reject reason"):
        SpawnRejectedError("spawn_limit_total", skill_id="worker")
