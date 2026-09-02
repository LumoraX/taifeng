"""peer-mailbox-messaging 测试:谱系内点对点投递 + 双模式 + wait_peer + SendToPeer Op。

覆盖 spec 的全部 Requirement:
  - 寻址三态(thread_id / handle_id / "parent",未知显式 error);
  - QueueOnly(运行中投 pending_input drain 并入 / 空闲落史 R5);
  - TriggerTurn(空闲唤醒 + K1 / 运行中降级 / root 拒绝 / suspended 不唤醒);
  - wait_peer(终态 / 超时 / 取消);
  - SendToPeer Op 与工具同路径;join-barrier 并存。
"""

from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers.sim import SimTurn, RoutingSimClient
from taifeng.loop.submission import SendToPeer
from taifeng.tool.builtins.send_message import make_send_message_tool
from taifeng.tool.builtins.spawn_skill import make_spawn_skill_tool
from taifeng.tool.builtins.wait_peer import (
    make_wait_any_tool,
    make_wait_peer_tool,
)
from taifeng.tool.spec import ToolResult, ToolSpec


async def _wait(cond, tries: int = 300) -> bool:
    """轮询等待条件成立(每次 10ms,最多 3s),等后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


_COORD = """---
name: coordinator
description: 协调者
version: 1.0.0
type: composite
entry: true
child_skills: [expert]
tool_names: [spawn_skill, send_message, wait_peer]
max_call_depth: 3
---
# COORD_MARK 协调者
"""

_EXPERT = """---
name: expert
description: 专家
version: 1.0.0
type: composite
tool_names: [gate_wait, send_message, request_user_input]
max_call_depth: 2
---
# EXPERT_MARK 专家
"""


@pytest.fixture
def peer_skills(tmp_path):
    """coordinator(entry)+ expert 双 skill 目录。"""
    skills = tmp_path / "peer_skills"
    (skills / "coordinator").mkdir(parents=True)
    (skills / "coordinator" / "SKILL.md").write_text(_COORD, encoding="utf-8")
    (skills / "expert").mkdir(parents=True)
    (skills / "expert" / "SKILL.md").write_text(_EXPERT, encoding="utf-8")
    return skills


def _gate_tool(gate: asyncio.Event) -> ToolSpec:
    """门控工具:handler 阻塞到 gate set —— 用于把子 turn 钉在「运行中」。"""

    async def h(args: dict, ctx: object) -> ToolResult:
        await gate.wait()
        return ToolResult.ok("opened")

    return ToolSpec(name="gate_wait", description="等门",
                    input_schema={"type": "object", "properties": {}},
                    handler=h, parallel_safe=True)


def _tools(gate: asyncio.Event | None = None) -> list[ToolSpec]:
    tools = [make_spawn_skill_tool(), make_send_message_tool(),
             make_wait_peer_tool()]
    if gate is not None:
        tools.append(_gate_tool(gate))
    return tools


async def _make_engine(skills, threads_dir, client, *, gate=None):
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir,
        model_client=client, compressors=[], extra_tools=_tools(gate))
    engine = await pool.get_or_create(
        session_id="peer", entry_skill_id="coordinator")
    return pool, engine


def test_peer_event_kinds() -> None:
    """四个 peer 事件类与 kind 字面量。"""
    from taifeng.loop.event import (
        PeerAgentWoken,
        PeerMessageSent,
        PeerWaitResolved,
        PeerWaitStarted,
    )
    assert PeerMessageSent().kind == "peer_message_sent"
    assert PeerAgentWoken().kind == "peer_agent_woken"
    assert PeerWaitStarted().kind == "peer_wait_started"
    assert PeerWaitResolved().kind == "peer_wait_resolved"


def test_send_to_peer_op_shape() -> None:
    """SendToPeer Op 可构造 + 序列化默认值。"""
    op = SendToPeer(target_thread_id="t1", text="hi")
    assert op.mode == "queue_only"
    assert op.from_thread_id is None


@pytest.mark.asyncio
async def test_queue_only_idle_child_persists(peer_skills, threads_dir) -> None:
    """QueueOnly 投空闲(done)专家:即时落子 thread 历史(R5),事件 delivered_via=history。"""
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [SimTurn(text="专家结论")],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client)
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid, child_tid = h["handle_id"], h["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    out = await engine.deliver_peer_message(
        target=child_tid, text="A 的关键发现:血糖异常", mode="queue_only",
        from_thread_id=engine.thread_id)
    assert out["delivered_via"] == "history"
    assert out["mode_downgraded"] is False

    # R5:即时持久化 —— 子 thread 重载历史含 peer 项(payload 标注)
    items = [it async for it in await pool.store.load_thread(child_tid)]
    peer = [it for it in items if it.kind == "user_message"
            and it.payload.get("source") == "peer"]
    assert len(peer) == 1
    assert peer[0].payload["from_thread"] == engine.thread_id
    assert "血糖异常" in peer[0].payload["text"]

    assert await _wait(lambda: any(
        m.kind == "peer_message_sent" for m in events))
    ev = next(m for m in events if m.kind == "peer_message_sent")
    assert ev.data["to"] == child_tid
    assert ev.data["mode"] == "queue_only"
    assert "血糖异常" not in str({k: v for k, v in ev.data.items()
                              if k != "text_preview"})  # 正文不进事件(仅预览)
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_handle_id_and_parent_addressing(peer_skills, threads_dir) -> None:
    """handle_id 等价寻址;"parent" 解析为 root thread(落 engine 历史)。"""
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [SimTurn(text="结论")],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client)
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid, child_tid = h["handle_id"], h["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")

    # handle_id 寻址 → 解析到 child_thread_id
    out = await engine.deliver_peer_message(
        target=hid, text="经句柄投递", mode="queue_only",
        from_thread_id=engine.thread_id)
    assert out["target_thread_id"] == child_tid

    # "parent" 寻址(从 child 视角)→ root thread,落 engine 历史
    out2 = await engine.deliver_peer_message(
        target="parent", text="上行汇报", mode="queue_only",
        from_thread_id=child_tid)
    assert out2["target_thread_id"] == engine.thread_id
    root_peer = [it for it in engine.history_snapshot()
                 if it.kind == "user_message"
                 and it.payload.get("source") == "peer"]
    assert len(root_peer) == 1
    assert root_peer[0].payload["from_thread"] == child_tid
    await pool.close()


@pytest.mark.asyncio
async def test_unknown_target_explicit_error(peer_skills, threads_dir) -> None:
    """未知目标 → 显式 ValueError(不静默丢弃)。"""
    client = RoutingSimClient(routes={})
    pool, engine = await _make_engine(peer_skills, threads_dir, client)
    with pytest.raises(ValueError, match="unknown_peer_target"):
        await engine.deliver_peer_message(
            target="no-such-thread", text="x", mode="queue_only",
            from_thread_id=engine.thread_id)
    await pool.close()


@pytest.mark.asyncio
async def test_trigger_turn_root_rejected(peer_skills, threads_dir) -> None:
    """TriggerTurn 打 root 被拒;QueueOnly 投 root 正常落史。"""
    client = RoutingSimClient(routes={})
    pool, engine = await _make_engine(peer_skills, threads_dir, client)
    with pytest.raises(ValueError, match="trigger_turn_root_forbidden"):
        await engine.deliver_peer_message(
            target=engine.thread_id, text="醒醒", mode="trigger_turn",
            from_thread_id=engine.thread_id)
    out = await engine.deliver_peer_message(
        target=engine.thread_id, text="留言", mode="queue_only",
        from_thread_id=engine.thread_id)
    assert out["delivered_via"] == "history"
    await pool.close()


@pytest.mark.asyncio
async def test_trigger_turn_wakes_idle_child(peer_skills, threads_dir) -> None:
    """TriggerTurn 唤醒空闲(done)专家:落史 → 新 detached turn → 句柄重回 done。"""
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [SimTurn(text="首轮结论"), SimTurn(text="被唤醒后的补充")],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client)
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid, child_tid = h["handle_id"], h["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    out = await engine.deliver_peer_message(
        target=child_tid, text="请补充意见", mode="trigger_turn",
        from_thread_id=engine.thread_id)
    assert out["delivered_via"] == "history"
    assert out["woken"] is True

    assert await _wait(lambda: any(
        m.kind == "peer_agent_woken" for m in events))
    # 唤醒 turn 跑完 → 句柄重回 done,result 为新 turn 产物
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
        and engine.spawn_status([hid])[hid]["result"] == "被唤醒后的补充")
    # 唤醒 turn 的历史含 peer 消息(在采样前可见)
    items = [it async for it in await pool.store.load_thread(child_tid)]
    assert any(it.payload.get("source") == "peer" for it in items
               if it.kind == "user_message")
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_trigger_turn_running_downgrades(peer_skills, threads_dir) -> None:
    """TriggerTurn 投运行中专家 → 降级 QueueOnly(pending_input),mode_downgraded=true;
    drain 后消息并入子 thread 历史。"""
    gate = asyncio.Event()
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [
            SimTurn(text="先等门", tool_calls=[
                {"id": "g1", "name": "gate_wait", "arguments": "{}"}]),
            SimTurn(text="门开后收尾"),
        ],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client,
                                      gate=gate)
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid, child_tid = h["handle_id"], h["child_thread_id"]
    # 等子 turn 进入 gate_wait(运行中)
    await asyncio.sleep(0.05)
    assert engine.spawn_status([hid])[hid]["status"] == "running"

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    out = await engine.deliver_peer_message(
        target=child_tid, text="插播:紧急发现", mode="trigger_turn",
        from_thread_id=engine.thread_id)
    assert out["delivered_via"] == "pending_input"
    assert out["mode_downgraded"] is True

    gate.set()  # 放行 → 迭代边界 drain 并入
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    items = [it async for it in await pool.store.load_thread(child_tid)]
    assert any(it.payload.get("source") == "peer" for it in items
               if it.kind == "user_message")
    ev = next(m for m in events if m.kind == "peer_message_sent")
    assert ev.data["mode_downgraded"] is True
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_suspended_child_not_woken(peer_skills, threads_dir) -> None:
    """挂起目标:两模式都只落史,不唤醒,句柄保持 suspended。"""
    from taifeng.tool.builtins.request_user_input import (
        make_request_user_input_tool,
    )
    gate = asyncio.Event()
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [
            SimTurn(text="需要补充", tool_calls=[
                {"id": "r1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充血脂?"}'}]),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=peer_skills, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[*_tools(gate), make_request_user_input_tool()])
    engine = await pool.get_or_create(
        session_id="peer", entry_skill_id="coordinator")
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid, child_tid = h["handle_id"], h["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    out = await engine.deliver_peer_message(
        target=child_tid, text="补充线索", mode="trigger_turn",
        from_thread_id=engine.thread_id)
    assert out["delivered_via"] == "history"
    assert out["woken"] is False
    await asyncio.sleep(0.1)
    # 不唤醒:句柄保持 suspended、无 peer_agent_woken
    assert engine.spawn_status([hid])[hid]["status"] == "suspended"
    assert not any(m.kind == "peer_agent_woken" for m in events)
    # 消息已落史(续跑可见)
    items = [it async for it in await pool.store.load_thread(child_tid)]
    assert any(it.payload.get("source") == "peer" for it in items
               if it.kind == "user_message")
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_wait_peer_terminal_and_timeout(peer_skills, threads_dir) -> None:
    """wait_peer:等到终态返回 status/result;未终态超时返回 timeout。"""
    gate = asyncio.Event()
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [
            SimTurn(text="等门", tool_calls=[
                {"id": "g1", "name": "gate_wait", "arguments": "{}"}]),
            SimTurn(text="完成"),
        ],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client,
                                      gate=gate)
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid = h["handle_id"]
    await asyncio.sleep(0.05)

    from taifeng.loop.cancellation import CancellationToken

    # 未终态 → 超时
    out = await engine.wait_spawn_terminal(
        handle_id=hid, timeout_seconds=0.2,
        cancel=CancellationToken(name="w"))
    assert out["outcome"] == "timeout"

    gate.set()
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    out2 = await engine.wait_spawn_terminal(
        handle_id=hid, timeout_seconds=5.0,
        cancel=CancellationToken(name="w2"))
    assert out2["outcome"] == "terminal"
    assert out2["status"] == "done"
    assert "完成" in out2["result"]  # final_text 跨迭代累积("等门"+"完成")
    await pool.close()


@pytest.mark.asyncio
async def test_wait_peer_cancel_cascades(peer_skills, threads_dir) -> None:
    """等待期间取消 → 立即中止(CancelledError 沿既有取消路径)。"""
    gate = asyncio.Event()
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [
            SimTurn(text="等门", tool_calls=[
                {"id": "g1", "name": "gate_wait", "arguments": "{}"}]),
            SimTurn(text="完成"),
        ],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client,
                                      gate=gate)
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid = h["handle_id"]
    await asyncio.sleep(0.05)

    from taifeng.loop.cancellation import CancellationToken

    token = CancellationToken(name="w")
    wait_task = asyncio.create_task(engine.wait_spawn_terminal(
        handle_id=hid, timeout_seconds=30.0, cancel=token))
    await asyncio.sleep(0.05)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task
    gate.set()
    await _wait(lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    await pool.close()


@pytest.mark.asyncio
async def test_send_to_peer_op_same_path(peer_skills, threads_dir) -> None:
    """SendToPeer Op 与 deliver_peer_message 同路径:事件一致、消息落史。"""
    client = RoutingSimClient(routes={
        "EXPERT_MARK": [SimTurn(text="结论")],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client)
    h = await engine.spawn_skill(skill_id="expert", args={}, reason="x")
    hid, child_tid = h["handle_id"], h["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await engine.submit(SendToPeer(
        target_thread_id=child_tid, text="程序化投递", mode="queue_only"))
    assert await _wait(lambda: any(
        m.kind == "peer_message_sent" for m in events))
    items = [it async for it in await pool.store.load_thread(child_tid)]
    assert any(it.payload.get("source") == "peer" for it in items
               if it.kind == "user_message")
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_llm_sibling_messaging_e2e(peer_skills, threads_dir) -> None:
    """旗舰 e2e:LLM 在 coordinator turn 内 spawn 专家 → 专家完成后经 send_message
    (trigger_turn)唤醒,专家新 turn 看到 peer 消息并产出补充结论;wait_peer 等到终态。"""
    client = RoutingSimClient(routes={
        "COORD_MARK": [
            # 第一 turn:迭代1 spawn,迭代2 收尾
            SimTurn(text="先派专家", tool_calls=[
                {"id": "s1", "name": "spawn_skill", "arguments":
                 '{"skill_id":"expert","reason":"collab","args":{}}'}]),
            SimTurn(text="已派出"),
            # 第二 turn:迭代1 send_message(占位符在提交前替换),迭代2 收尾
            SimTurn(text="专家已完成,推送发现并唤醒", tool_calls=[
                {"id": "m1", "name": "send_message", "arguments":
                 '{"target":"__CHILD_TID__","text":"补充:患者有家族史",'
                 '"mode":"trigger_turn"}'}]),
            SimTurn(text="协调完毕"),
        ],
        "EXPERT_MARK": [SimTurn(text="初步结论"), SimTurn(text="结合家族史的补充结论")],
    })
    pool, engine = await _make_engine(peer_skills, threads_dir, client)

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    # 第一 turn:spawn 专家
    sub_id = await engine.submit(taifeng.UserMessage(text="开始协作"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break
    started = next(m for m in events if m.kind == "spawn_started")
    hid, child_tid = started.data["handle_id"], started.data["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")

    # 把真实 child_tid 注入 LLM 脚本(mock 不解析 prompt,直接改 route 参数)
    coord_turns = client._routes["COORD_MARK"]  # noqa: SLF001
    coord_turns[2].tool_calls[0]["arguments"] = (
        coord_turns[2].tool_calls[0]["arguments"]
        .replace("__CHILD_TID__", child_tid))

    # 第二 turn:LLM 发 send_message(trigger_turn)唤醒专家
    sub_id2 = await engine.submit(taifeng.UserMessage(text="推进"))
    async for ev in engine.subscribe(sub_id2):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break

    assert any(m.kind == "peer_message_sent" for m in events)
    assert await _wait(lambda: any(
        m.kind == "peer_agent_woken" for m in events))
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["result"] == "结合家族史的补充结论")
    task.cancel()
    await pool.close()


# ---------------------------------------------------------------------------
# wait_any —— any-of-N 等待(等待原语的中间档:等一个 / 等任一 / 等全部)
# 用独立 fixture 而非扩展 peer_skills:需要「一慢一快」两种子 skill 才能确定性地
# 造出「部分终态」,而给共享 fixture 加 child 会改动其他用例的 prompt。
# ---------------------------------------------------------------------------

_WA_COORD = """---
name: wa-coord
description: any 等待协调者
version: 1.0.0
type: composite
entry: true
child_skills: [wa-slow, wa-fast]
tool_names: [spawn_skill, wait_any]
max_call_depth: 3
---
# WA_COORD_MARK 协调者
"""

_WA_SLOW = """---
name: wa-slow
description: 慢专家(钉在门上)
version: 1.0.0
type: composite
tool_names: [gate_wait]
max_call_depth: 2
---
# WA_SLOW_MARK 慢专家
"""

_WA_FAST = """---
name: wa-fast
description: 快专家(立即完成)
version: 1.0.0
type: atomic
---
# WA_FAST_MARK 快专家
"""


@pytest.fixture
def wait_any_skills(tmp_path):
    """wa-coord(entry)+ wa-slow(门控)+ wa-fast(立即完成)三 skill 目录。"""
    skills = tmp_path / "wa_skills"
    for name, body in (("wa-coord", _WA_COORD), ("wa-slow", _WA_SLOW),
                       ("wa-fast", _WA_FAST)):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


def _wa_routes() -> dict:
    """慢专家钉在 gate_wait 上;快专家一轮即完成。"""
    return {
        "WA_SLOW_MARK": [
            SimTurn(text="慢-等门", tool_calls=[
                {"id": "g1", "name": "gate_wait", "arguments": "{}"}]),
            SimTurn(text="慢-完成"),
        ],
        "WA_FAST_MARK": [SimTurn(text="快-完成"), SimTurn(text="快-完成2")],
    }


async def _make_wa_engine(skills, threads_dir, gate):
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir,
        model_client=RoutingSimClient(routes=_wa_routes()), compressors=[],
        extra_tools=[make_spawn_skill_tool(), make_wait_any_tool(),
                     _gate_tool(gate)])
    engine = await pool.get_or_create(
        session_id="wa", entry_skill_id="wa-coord")
    return pool, engine


@pytest.mark.asyncio
async def test_wait_any_wakes_on_first_terminal(wait_any_skills,
                                                threads_dir) -> None:
    """任一终态即唤醒:快专家跑完就返回,不等仍被门钉住的慢专家。

    同时校验两个 wait_any 事件的 data 形态(R3)。
    """
    gate = asyncio.Event()
    pool, engine = await _make_wa_engine(wait_any_skills, threads_dir, gate)
    events = []

    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind.startswith("peer_wait_any_"):
                events.append(ev.msg)

    task = asyncio.create_task(watch())
    slow = (await engine.spawn_skill(
        skill_id="wa-slow", args={}, reason="s"))["handle_id"]
    fast = (await engine.spawn_skill(
        skill_id="wa-fast", args={}, reason="f"))["handle_id"]

    from taifeng.loop.cancellation import CancellationToken

    out = await engine.wait_spawn_any(
        handle_ids=[slow, fast], timeout_seconds=5.0,
        cancel=CancellationToken(name="wa"))
    # 快专家终态即唤醒;慢专家仍钉在门上 → 落 pending,不被等
    assert out["outcome"] == "terminal"
    assert list(out["settled"]) == [fast]
    assert out["settled"][fast]["status"] == "done"
    assert out["pending"] == [slow]
    assert engine.spawn_status([slow])[slow]["status"] == "running"

    assert await _wait(lambda: len(events) == 2)
    started, resolved = events[0], events[1]
    assert started.kind == "peer_wait_any_started"
    assert started.data["handle_ids"] == [slow, fast]
    assert started.data["timeout_seconds"] == 5.0
    assert resolved.kind == "peer_wait_any_resolved"
    assert resolved.data == {"settled_ids": [fast], "pending_ids": [slow],
                             "outcome": "terminal"}

    gate.set()
    await _wait(lambda: engine.spawn_status([slow])[slow]["status"] == "done")
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_wait_any_collects_all_settled_at_wake(wait_any_skills,
                                                     threads_dir) -> None:
    """唤醒时收走当时**全部**已终态句柄,且已终态时立即返回(不空转一个轮询周期)。"""
    gate = asyncio.Event()
    pool, engine = await _make_wa_engine(wait_any_skills, threads_dir, gate)
    a = (await engine.spawn_skill(
        skill_id="wa-fast", args={}, reason="a"))["handle_id"]
    b = (await engine.spawn_skill(
        skill_id="wa-fast", args={}, reason="b"))["handle_id"]
    # 两个都跑到终态后再等 —— 构造「同一轮询周期内多个已终态」
    assert await _wait(lambda: all(
        engine.spawn_status([h])[h]["status"] == "done" for h in (a, b)))

    from taifeng.loop.cancellation import CancellationToken

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = await engine.wait_spawn_any(
        handle_ids=[a, b], timeout_seconds=5.0,
        cancel=CancellationToken(name="wa2"))
    elapsed = loop.time() - t0

    assert out["outcome"] == "terminal"
    assert set(out["settled"]) == {a, b}, "同批多个终态须一次收全,不逼调用方复调"
    assert out["pending"] == []
    assert elapsed < 0.05, f"已终态应立即返回,实测等了 {elapsed:.3f}s"
    gate.set()
    await pool.close()


@pytest.mark.asyncio
async def test_wait_any_timeout_and_arg_rejections(wait_any_skills,
                                                   threads_dir) -> None:
    """全 pending 至超时不失败 turn;空集 / 未知句柄显式抛(禁 silent fallback)。"""
    gate = asyncio.Event()
    pool, engine = await _make_wa_engine(wait_any_skills, threads_dir, gate)
    slow = (await engine.spawn_skill(
        skill_id="wa-slow", args={}, reason="s"))["handle_id"]

    from taifeng.loop.cancellation import CancellationToken

    # 1. 全 pending → timeout(settled 空、pending 全量,turn 不失败)
    out = await engine.wait_spawn_any(
        handle_ids=[slow], timeout_seconds=0.2,
        cancel=CancellationToken(name="wa3"))
    assert out["outcome"] == "timeout"
    assert out["settled"] == {} and out["pending"] == [slow]

    # 2. 空集永不可能被满足 → 显式抛,不静默等到超时
    with pytest.raises(ValueError, match="empty_handle_ids"):
        await engine.wait_spawn_any(
            handle_ids=[], timeout_seconds=1.0,
            cancel=CancellationToken(name="wa4"))

    # 3. 未知句柄 → 显式抛,不跳过它继续等其余(禁 silent skip)
    with pytest.raises(ValueError, match="unknown_spawn_handle"):
        await engine.wait_spawn_any(
            handle_ids=[slow, "ghost"], timeout_seconds=1.0,
            cancel=CancellationToken(name="wa5"))

    gate.set()
    await _wait(lambda: engine.spawn_status([slow])[slow]["status"] == "done")
    await pool.close()


@pytest.mark.asyncio
async def test_wait_any_cancel_cascades(wait_any_skills, threads_dir) -> None:
    """等待期间取消 → 立即中止(CancelledError 沿既有取消路径,R4)。"""
    gate = asyncio.Event()
    pool, engine = await _make_wa_engine(wait_any_skills, threads_dir, gate)
    slow = (await engine.spawn_skill(
        skill_id="wa-slow", args={}, reason="s"))["handle_id"]

    from taifeng.loop.cancellation import CancellationToken

    token = CancellationToken(name="wa-cancel")
    wait_task = asyncio.create_task(engine.wait_spawn_any(
        handle_ids=[slow], timeout_seconds=30.0, cancel=token))
    await asyncio.sleep(0.05)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task
    gate.set()
    await _wait(lambda: engine.spawn_status([slow])[slow]["status"] == "done")
    await pool.close()
