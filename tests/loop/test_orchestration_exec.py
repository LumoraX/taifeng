"""声明式编排端到端（走 EnginePool + RoutingSimClient）：

- parallel 段：cap≥2 真并发（wall-clock < 串行和）；cap=1 退化串行（回归对照）
- serial 段：即便 cap 高也强制 Semaphore(1) 串行
- 同种子 + 前序输出注入：summarizer 的子线程种子含上一步 child 输出
- when：condition flag 选 then/else；flag 缺失 → 硬失败 turn + condition_missing 事件
- orchestration_plan_resolved 事件 emit
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers.sim import SimTurn, RoutingSimClient

if TYPE_CHECKING:
    from pathlib import Path


def _collect_user_message_seeds(threads_dir: Path) -> list[str]:
    """同步扫描 threads_dir 下所有 JSONL，收集 user_message 的文本（供 async 测试调用）。

    放同步 helper 而非在 async 测试体里直接做 pathlib IO —— 规避 ASYNC240。
    """
    seeds: list[str] = []
    for jf in threads_dir.rglob("*.jsonl"):
        for line in jf.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("kind") == "user_message":
                seeds.append(obj.get("payload", {}).get("text", ""))
    return seeds


def _entry(orch_yaml: str, children: list[str]) -> str:
    return (
        "---\n"
        "name: trip\ndescription: 行程编排\nversion: 1.0.0\n"
        "type: composite\nentry: true\nmodel: mock-model\n"
        f"child_skills: [{', '.join(children)}]\ntool_names: []\nmax_call_depth: 3\n"
        f"{orch_yaml}"
        "---\n# 行程编排 ENTRY_MARK\n"
    )


def _child(sid: str, mark: str) -> str:
    return (
        "---\n"
        f"name: {sid}\ndescription: {sid}\nversion: 1.0.0\ntype: atomic\n"
        f"---\n# {sid} {mark}\n推算 {sid}。\n"
    )


def _write(skills: Path, name: str, body: str) -> None:
    d = skills / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


async def _run(
    skills: Path, threads_dir: Path, client: RoutingSimClient, *, cap: int
) -> tuple[float, list, set[str]]:
    """跑 trip 编排，返回 (根 turn wall-clock, 全事件列表, returned 子 skill 集合)。

    用 subscribe_all + is_root=True 收根 turn（子 turn 也 emit turn_completed）。
    """
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir,
        model_client=client, compressors=[], max_parallel_tool_calls=cap,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="trip")

    events: list = []
    returned: set[str] = set()
    root_done = asyncio.Event()
    holder: dict[str, float] = {}

    async def consume() -> None:
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "skill_returned":
                returned.add(ev.msg.data.get("skill_id", ""))
            if ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root"):
                holder["t"] = time.monotonic() - start
                root_done.set()
            if ev.msg.kind == "turn_failed" and ev.msg.data.get("is_root"):
                holder["t"] = time.monotonic() - start
                root_done.set()
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(consume())
    start = time.monotonic()
    await engine.submit(taifeng.UserMessage(text="规划行程"))
    await asyncio.wait_for(root_done.wait(), timeout=5.0)
    await pool.close()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
    return holder["t"], events, returned


@pytest.mark.asyncio
async def test_parallel_step_concurrent(tmp_path: Path, threads_dir: Path) -> None:
    """parallel: [route-a, route-b]，各 sleep 0.3s，cap=2 → 根 turn < 0.5s（并发）。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n    - parallel: [route-a, route-b]\n",
        ["route-a", "route-b"],
    ))
    _write(skills, "route-a", _child("route-a", "A_MARK"))
    _write(skills, "route-b", _child("route-b", "B_MARK"))
    client = RoutingSimClient(routes={
        "A_MARK": [SimTurn(text="线路甲完成", delay_seconds=0.3)],
        "B_MARK": [SimTurn(text="线路乙完成", delay_seconds=0.3)],
    })
    elapsed, _events, returned = await _run(skills, threads_dir, client, cap=2)
    assert returned == {"route-a", "route-b"}
    assert elapsed < 0.5, f"期望并发(<0.5s)，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_parallel_step_serial_when_cap_one(tmp_path: Path, threads_dir: Path) -> None:
    """同上 cap=1 → 退化串行 → 根 turn >= 0.35s（回归对照）。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n    - parallel: [route-a, route-b]\n",
        ["route-a", "route-b"],
    ))
    _write(skills, "route-a", _child("route-a", "A_MARK"))
    _write(skills, "route-b", _child("route-b", "B_MARK"))
    client = RoutingSimClient(routes={
        "A_MARK": [SimTurn(text="线路甲完成", delay_seconds=0.2)],
        "B_MARK": [SimTurn(text="线路乙完成", delay_seconds=0.2)],
    })
    elapsed, _events, returned = await _run(skills, threads_dir, client, cap=1)
    assert returned == {"route-a", "route-b"}
    assert elapsed >= 0.35, f"期望串行(>=0.35s)，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_serial_step_forces_sequential(tmp_path: Path, threads_dir: Path) -> None:
    """serial: [route-a, route-b]，各 0.2s，即便 cap=4 也强制串行 → >= 0.35s。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n    - serial: [route-a, route-b]\n",
        ["route-a", "route-b"],
    ))
    _write(skills, "route-a", _child("route-a", "A_MARK"))
    _write(skills, "route-b", _child("route-b", "B_MARK"))
    client = RoutingSimClient(routes={
        "A_MARK": [SimTurn(text="甲", delay_seconds=0.2)],
        "B_MARK": [SimTurn(text="乙", delay_seconds=0.2)],
    })
    elapsed, _events, returned = await _run(skills, threads_dir, client, cap=4)
    assert returned == {"route-a", "route-b"}
    assert elapsed >= 0.35, f"serial 段应串行(>=0.35s)，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_upstream_injected_into_serial_step(tmp_path: Path, threads_dir: Path) -> None:
    """parallel → serial[summarizer]：summarizer 子线程种子含上一步 child 输出。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n"
        "    - parallel: [route-a, route-b]\n"
        "    - serial: [summarizer]\n",
        ["route-a", "route-b", "summarizer"],
    ))
    _write(skills, "route-a", _child("route-a", "A_MARK"))
    _write(skills, "route-b", _child("route-b", "B_MARK"))
    _write(skills, "summarizer", _child("summarizer", "SUM_MARK"))
    client = RoutingSimClient(routes={
        "A_MARK": [SimTurn(text="线路甲完成")],
        "B_MARK": [SimTurn(text="线路乙完成")],
        "SUM_MARK": [SimTurn(text="汇总完毕")],
    })
    _elapsed, _events, returned = await _run(skills, threads_dir, client, cap=2)
    assert returned == {"route-a", "route-b", "summarizer"}

    seeds = _collect_user_message_seeds(threads_dir)
    upstream_seed = [s for s in seeds if "upstream" in s]
    assert upstream_seed, f"未找到含 upstream 的子线程种子；seeds={seeds}"
    assert "线路甲完成" in upstream_seed[0] and "线路乙完成" in upstream_seed[0]


@pytest.mark.asyncio
async def test_when_true_branch(tmp_path: Path, threads_dir: Path) -> None:
    """上一步输出 needs_weather=true → 执行 then 分支（weather）。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n"
        "    - serial: [probe]\n"
        "    - when:\n"
        "        condition: needs_weather\n"
        "        then: [weather]\n",
        ["probe", "weather"],
    ))
    _write(skills, "probe", _child("probe", "PROBE_MARK"))
    _write(skills, "weather", _child("weather", "WEATHER_MARK"))
    client = RoutingSimClient(routes={
        "PROBE_MARK": [SimTurn(text='{"needs_weather": true}')],
        "WEATHER_MARK": [SimTurn(text="天气已查")],
    })
    _elapsed, _events, returned = await _run(skills, threads_dir, client, cap=2)
    assert "weather" in returned, f"needs_weather=true 应执行 weather；returned={returned}"


@pytest.mark.asyncio
async def test_when_false_skips_when_no_else(tmp_path: Path, threads_dir: Path) -> None:
    """needs_weather=false 且无 else → 跳过该段（weather 不执行）。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n"
        "    - serial: [probe]\n"
        "    - when:\n"
        "        condition: needs_weather\n"
        "        then: [weather]\n",
        ["probe", "weather"],
    ))
    _write(skills, "probe", _child("probe", "PROBE_MARK"))
    _write(skills, "weather", _child("weather", "WEATHER_MARK"))
    client = RoutingSimClient(routes={
        "PROBE_MARK": [SimTurn(text='{"needs_weather": false}')],
        "WEATHER_MARK": [SimTurn(text="天气已查")],
    })
    _elapsed, _events, returned = await _run(skills, threads_dir, client, cap=2)
    assert "weather" not in returned, f"needs_weather=false 应跳过 weather；returned={returned}"


@pytest.mark.asyncio
async def test_when_missing_flag_hard_fails(tmp_path: Path, threads_dir: Path) -> None:
    """上一步输出不含 flag → emit condition_missing + 根 turn 失败（硬失败，禁 fallback）。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n"
        "    - serial: [probe]\n"
        "    - when:\n"
        "        condition: needs_weather\n"
        "        then: [weather]\n",
        ["probe", "weather"],
    ))
    _write(skills, "probe", _child("probe", "PROBE_MARK"))
    _write(skills, "weather", _child("weather", "WEATHER_MARK"))
    client = RoutingSimClient(routes={
        "PROBE_MARK": [SimTurn(text='{"other": 1}')],
        "WEATHER_MARK": [SimTurn(text="天气已查")],
    })
    _elapsed, events, _returned = await _run(skills, threads_dir, client, cap=2)
    kinds = [e.msg.kind for e in events]
    assert "orchestration_condition_missing" in kinds
    root_failed = [
        e for e in events
        if e.msg.kind == "turn_failed" and e.msg.data.get("is_root")
    ]
    assert root_failed, f"期望根 turn failed；kinds={kinds}"


@pytest.mark.asyncio
async def test_when_then_parallel_injects_upstream(tmp_path: Path, threads_dir: Path) -> None:
    """when 段（即便其 then 叶子是 parallel）作为条件后续段，仍注入上一步输出。

    锁定决策：「serial/when 段额外注入上一步输出」（段=顶层 step），故 when→parallel
    分支的各 child 也应在种子里看到触发步 probe 的输出。
    """
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n"
        "    - serial: [probe]\n"
        "    - when:\n"
        "        condition: needs_weather\n"
        "        then: [weather, traffic]\n",  # then 叶子是 parallel（裸列表）
        ["probe", "weather", "traffic"],
    ))
    _write(skills, "probe", _child("probe", "PROBE_MARK"))
    _write(skills, "weather", _child("weather", "WEATHER_MARK"))
    _write(skills, "traffic", _child("traffic", "TRAFFIC_MARK"))
    client = RoutingSimClient(routes={
        "PROBE_MARK": [SimTurn(text='{"needs_weather": true}')],
        "WEATHER_MARK": [SimTurn(text="天气已查")],
        "TRAFFIC_MARK": [SimTurn(text="路况已查")],
    })
    _elapsed, _events, returned = await _run(skills, threads_dir, client, cap=4)
    assert {"weather", "traffic"} <= returned
    # weather / traffic 的子线程种子应含上一步 probe 的输出（upstream 注入）。
    # probe 输出 `{"needs_weather": true}` 嵌进 upstream 列表后会被 json.dumps 二次转义，
    # 故按转义无关的键名 `needs_weather` 判定（该键名只可能来自被注入的 probe 输出）。
    upstream_seed = [s for s in _collect_user_message_seeds(threads_dir) if "upstream" in s]
    assert any("needs_weather" in s for s in upstream_seed), (
        f"when→parallel 叶子应注入触发步输出；seeds={upstream_seed}"
    )


@pytest.mark.asyncio
async def test_orchestrated_turn_respects_cancel() -> None:
    """段间取消边界：已取消的 cancel token → run_orchestrated_turn 在首个段边界抛 CancelledError。

    复用 CancellationToken（已在 test_cancellation.py 充分单测）；用极简 duck-typed runner
    隔离验证编排循环确实在 raise_if_cancelled 处中断（无时序、不脆）。
    """
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.orchestration_exec import run_orchestrated_turn
    from taifeng.skill.orchestration import OrchestrationSpec, ParallelStep

    cancel = CancellationToken()
    cancel.cancel()

    class _FakeEntry:
        id = "trip"
        orchestration = OrchestrationSpec(steps=(ParallelStep(skill_ids=("a",)),))

    emitted: list = []

    class _FakeRunner:
        entry_skill = _FakeEntry()
        history_buffer: list = []

        def __init__(self, c: CancellationToken) -> None:
            self.cancel = c

        async def _emit(self, msg: object) -> None:
            emitted.append(msg)

        def _build_tool_context(self, call_id: str, iteration: int) -> object:
            raise AssertionError("取消应在派发前中断，不应构造 ToolContext")

    runner = _FakeRunner(cancel)
    with pytest.raises(asyncio.CancelledError):
        await run_orchestrated_turn(runner)  # type: ignore[arg-type]
    # plan_resolved 在取消检查之前已 emit；dispatch 未发生
    assert any(getattr(m, "kind", "") == "orchestration_plan_resolved" for m in emitted)


@pytest.mark.asyncio
async def test_plan_resolved_emitted(tmp_path: Path, threads_dir: Path) -> None:
    """声明了 orchestration 的 entry 执行时 emit 一次 orchestration_plan_resolved。"""
    skills = tmp_path / "s"
    _write(skills, "trip", _entry(
        "orchestration:\n  steps:\n    - parallel: [route-a]\n",
        ["route-a"],
    ))
    _write(skills, "route-a", _child("route-a", "A_MARK"))
    client = RoutingSimClient(routes={"A_MARK": [SimTurn(text="甲")]})
    _elapsed, events, _returned = await _run(skills, threads_dir, client, cap=2)
    resolved = [e for e in events if e.msg.kind == "orchestration_plan_resolved"]
    assert len(resolved) == 1
    assert resolved[0].msg.data["skill_id"] == "trip"
    assert resolved[0].msg.data["groups"] == [{"type": "parallel", "skills": ["route-a"]}]
