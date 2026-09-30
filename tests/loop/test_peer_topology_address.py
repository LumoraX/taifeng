"""peer 拓扑路径寻址（ADR 0091）：按对方跑的 skill 指代它，不需要先拿到运行时 id。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.loop.peer_address import (
    TopologyAddress,
    is_topology_address,
    parse_topology_address,
    resolve_topology_address,
)
from taifeng.loop.spawn_handle import SpawnHandle
from taifeng.loop.submission import SendToPeer
from taifeng.tool.builtins.send_message import make_send_message_tool
from taifeng.tool.builtins.spawn_skill import make_spawn_skill_tool
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

_ROOT = "thr_root"


def _handle(handle_id: str, skill_id: str, status: str = "done") -> SpawnHandle:
    return SpawnHandle(
        handle_id=handle_id, skill_id=skill_id, child_thread_id=f"thr_{handle_id}",
        status=status,  # type: ignore[arg-type]
    )


def _resolve(target: str, sender: str, *handles: SpawnHandle) -> str:
    return resolve_topology_address(
        target, sender_thread_id=sender, root_thread_id=_ROOT, handles=handles
    )


# ------------------------------------------------------------------
# 解析
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("sibling:reviewer", TopologyAddress("sibling", "reviewer")),
        ("child:reviewer", TopologyAddress("child", "reviewer")),
        ("sibling:code-reviewer#2", TopologyAddress("sibling", "code-reviewer", 2)),
    ],
)
def test_parse(target: str, expected: TopologyAddress) -> None:
    assert parse_topology_address(target) == expected
    assert is_topology_address(target)


@pytest.mark.parametrize(
    "target",
    ["sibling:", "child:#1", "sibling:a b", "sibling:reviewer#0", "sibling:reviewer#x",
     "sibling:reviewer#"],
)
def test_malformed_address_is_rejected(target: str) -> None:
    assert is_topology_address(target)
    with pytest.raises(ValueError, match="invalid_peer_address"):
        parse_topology_address(target)


@pytest.mark.parametrize("target", ["parent", "thr_abc", "sp_1234", "cousin:reviewer"])
def test_other_targets_are_not_topology_addresses(target: str) -> None:
    assert not is_topology_address(target)


# ------------------------------------------------------------------
# 解析到 thread
# ------------------------------------------------------------------


def test_sibling_resolves_to_the_other_child() -> None:
    writer, reviewer = _handle("w", "writer"), _handle("r", "reviewer")

    assert _resolve("sibling:reviewer", "thr_w", writer, reviewer) == "thr_r"


def test_child_resolves_from_the_root() -> None:
    assert _resolve("child:reviewer", _ROOT, _handle("r", "reviewer")) == "thr_r"


def test_sender_is_never_its_own_sibling() -> None:
    first, second = _handle("a", "worker"), _handle("b", "worker")

    assert _resolve("sibling:worker", "thr_a", first, second) == "thr_b"
    with pytest.raises(ValueError, match="unknown_peer_target"):
        _resolve("sibling:worker", "thr_a", first)


def test_relation_must_fit_the_sender() -> None:
    reviewer = _handle("r", "reviewer")

    with pytest.raises(ValueError, match="peer_address_not_applicable.*child:reviewer"):
        _resolve("sibling:reviewer", _ROOT, reviewer)
    with pytest.raises(ValueError, match="peer_address_not_applicable.*sibling:reviewer"):
        _resolve("child:reviewer", "thr_w", _handle("w", "writer"), reviewer)


def test_unknown_skill() -> None:
    with pytest.raises(ValueError, match="unknown_peer_target"):
        _resolve("child:ghost", _ROOT, _handle("r", "reviewer"))


def test_several_instances_need_an_index() -> None:
    handles = [_handle("a", "worker"), _handle("b", "worker"), _handle("c", "worker")]

    with pytest.raises(ValueError, match="ambiguous_peer_target") as raised:
        _resolve("child:worker", _ROOT, *handles)

    assert "handles: a, b, c" in str(raised.value)
    assert _resolve("child:worker#1", _ROOT, *handles) == "thr_a"
    assert _resolve("child:worker#3", _ROOT, *handles) == "thr_c"
    with pytest.raises(ValueError, match="unknown_peer_target.*only 3 instance"):
        _resolve("child:worker#4", _ROOT, *handles)


@pytest.mark.parametrize("status", ["cancelled", "error"])
def test_failed_instances_are_not_addressable(status: str) -> None:
    gone, alive = _handle("a", "worker", status), _handle("b", "worker", "running")

    assert _resolve("child:worker", _ROOT, gone, alive) == "thr_b"
    assert _resolve("child:worker#1", _ROOT, gone, alive) == "thr_b"
    with pytest.raises(ValueError, match="unknown_peer_target"):
        _resolve("child:worker", _ROOT, gone)


@pytest.mark.parametrize("status", ["running", "suspended", "done"])
def test_live_and_finished_instances_are_addressable(status: str) -> None:
    assert _resolve("child:worker", _ROOT, _handle("a", "worker", status)) == "thr_a"


# ------------------------------------------------------------------
# 端到端
# ------------------------------------------------------------------

_COORD = """---
name: coordinator
description: 协调者
version: 1.0.0
type: composite
entry: true
child_skills: [writer, reviewer]
tool_names: [spawn_skill, send_message]
max_call_depth: 3
---
# COORD_MARK 协调者
"""

_EXPERT = """---
name: {name}
description: {name} 专家
version: 1.0.0
type: composite
tool_names: [send_message]
max_call_depth: 2
---
# {mark} 专家
"""


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "coordinator").mkdir(parents=True)
    (root / "coordinator" / "SKILL.md").write_text(_COORD, encoding="utf-8")
    for name in ("writer", "reviewer"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(
            _EXPERT.format(name=name, mark=f"{name.upper()}_MARK"), encoding="utf-8"
        )
    return root


async def _engine(
    tmp_path: Path, routes: dict[str, list[SimTurn]],
) -> tuple[taifeng.EnginePool, taifeng.AgentEngine, RoutingSimClient]:
    client = RoutingSimClient(routes=routes)
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
        model_client=client, compressors=[],
        extra_tools=[make_spawn_skill_tool(), make_send_message_tool()],
    )
    engine = await pool.get_or_create(session_id="peer", entry_skill_id="coordinator")
    return pool, engine, client


async def _spawn_done(engine: taifeng.AgentEngine, skill_id: str) -> dict[str, str]:
    handle = await engine.spawn_skill(skill_id=skill_id, args={}, reason="x")
    await wait_for_condition(
        lambda: engine.spawn_status([handle["handle_id"]])[handle["handle_id"]]["status"]
        == "done"
    )
    return handle


async def _peer_items(pool: taifeng.EnginePool, thread_id: str) -> list[Any]:
    items = [item async for item in await pool.store.load_thread(thread_id)]
    return [
        item for item in items
        if item.kind == "user_message" and item.payload.get("source") == "peer"
    ]


async def test_root_addresses_a_child_by_skill(tmp_path: Path) -> None:
    pool, engine, _ = await _engine(tmp_path, {
        "WRITER_MARK": [SimTurn(text="稿子")],
        "REVIEWER_MARK": [SimTurn(text="意见")],
    })
    await _spawn_done(engine, "writer")
    reviewer = await _spawn_done(engine, "reviewer")
    events: list[Any] = []

    async def watch() -> None:
        async for event in engine.subscribe_all():
            events.append(event.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    out = await engine.deliver_peer_message(
        target="child:reviewer", text="请复核第三节", mode="queue_only",
        from_thread_id=engine.thread_id,
    )

    assert out["target_thread_id"] == reviewer["child_thread_id"]
    assert out["address"] == "child:reviewer"
    delivered = await _peer_items(pool, reviewer["child_thread_id"])
    assert [item.payload["text"] for item in delivered] == ["请复核第三节"]
    await wait_for_condition(
        lambda: any(message.kind == "peer_message_sent" for message in events)
    )
    sent = next(message for message in events if message.kind == "peer_message_sent")
    assert sent.data["to"] == reviewer["child_thread_id"]
    assert sent.data["address"] == "child:reviewer"
    task.cancel()
    await pool.close()


async def test_direct_addressing_leaves_no_address_field(tmp_path: Path) -> None:
    pool, engine, _ = await _engine(tmp_path, {"REVIEWER_MARK": [SimTurn(text="意见")]})
    reviewer = await _spawn_done(engine, "reviewer")

    by_handle = await engine.deliver_peer_message(
        target=reviewer["handle_id"], text="x", mode="queue_only",
        from_thread_id=engine.thread_id,
    )
    to_root = await engine.deliver_peer_message(
        target="root", text="y", mode="queue_only",
        from_thread_id=reviewer["child_thread_id"],
    )

    assert "address" not in by_handle
    assert to_root["target_thread_id"] == engine.thread_id
    assert "address" not in to_root
    await pool.close()


async def test_sibling_wakes_the_other_expert(tmp_path: Path) -> None:
    """writer 按 skill 名给 reviewer 发消息并唤醒它，全程不需要对方的句柄。"""
    arguments = json.dumps(
        {"target": "sibling:reviewer", "text": "初稿好了", "mode": "trigger_turn"},
        ensure_ascii=False,
    )
    pool, engine, client = await _engine(tmp_path, {
        "REVIEWER_MARK": [SimTurn(text="等稿子"), SimTurn(text="已复核初稿")],
        "WRITER_MARK": [
            SimTurn(text="通知复核", tool_calls=[
                {"id": "tc_send", "name": "send_message", "arguments": arguments},
            ]),
            SimTurn(text="稿子写完"),
        ],
    })
    reviewer = await _spawn_done(engine, "reviewer")

    await _spawn_done(engine, "writer")

    sent = json.loads(client.ledger.function_call_output_text("tc_send") or "{}")
    assert sent["target_thread_id"] == reviewer["child_thread_id"]
    assert sent["address"] == "sibling:reviewer"
    assert sent["woken"] is True
    hid = reviewer["handle_id"]
    await wait_for_condition(
        lambda: engine.spawn_status([hid])[hid]["result"] == "已复核初稿"
    )
    await pool.close()


async def test_tool_reports_addressing_errors(tmp_path: Path) -> None:
    arguments = json.dumps({"target": "sibling:ghost", "text": "在吗"}, ensure_ascii=False)
    pool, engine, client = await _engine(tmp_path, {
        "WRITER_MARK": [
            SimTurn(text="找人", tool_calls=[
                {"id": "tc_send", "name": "send_message", "arguments": arguments},
            ]),
            SimTurn(text="没找到"),
        ],
    })

    await _spawn_done(engine, "writer")

    output = client.ledger.function_call_output_text("tc_send") or ""
    assert output.startswith("unknown_peer_target: sibling:ghost")
    await pool.close()


async def test_send_to_peer_op_accepts_topology_address(tmp_path: Path) -> None:
    pool, engine, _ = await _engine(tmp_path, {"REVIEWER_MARK": [SimTurn(text="意见")]})
    reviewer = await _spawn_done(engine, "reviewer")

    await engine.submit(SendToPeer(target_thread_id="child:reviewer", text="程序化投递"))

    async def delivered() -> bool:
        return bool(await _peer_items(pool, reviewer["child_thread_id"]))

    async def poll() -> None:
        while not await delivered():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=GUARD_TIMEOUT_SECONDS)
    await pool.close()
