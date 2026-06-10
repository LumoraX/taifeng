"""peer-mailbox demo —— 活体专家间点对点消息(mock,无需 key)。

MDT 场景缩影:协调者 spawn 专家 → 专家完成首轮 → 协调者把新发现
``send_message(mode=trigger_turn)`` 推给专家并唤醒其新 turn → 专家结合新信息
产出补充结论 → 协调者 ``wait_peer`` 等到终态取结果。演示:

1. ``send_message`` 工具(LLM 入口)与 ``SendToPeer`` Op(业务入口)同一路径;
2. TriggerTurn 唤醒空闲专家(``peer_agent_woken``)与运行中自动降级;
3. peer 消息以 ``source="peer", from_thread`` 标注落入对方历史(R5 持久);
4. ``wait_peer(handle_id, timeout_seconds)``(timeout 必填防互等死锁)。

契约:docs/architecture/capabilities/peer-mailbox-messaging.md。

运行:
    PYTHONPATH=src uv run python examples/peer_messaging/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.loop.submission import SendToPeer
from taifeng.tool.builtins.send_message import make_send_message_tool
from taifeng.tool.builtins.spawn_skill import make_spawn_skill_tool
from taifeng.tool.builtins.wait_peer import make_wait_peer_tool

_COORD = """---
name: coordinator
description: 会诊协调者
version: 1.0.0
type: composite
entry: true
child_skills: [expert]
tool_names: [spawn_skill, send_message, wait_peer]
max_call_depth: 3
---
# COORD_MARK 会诊协调者
"""

_EXPERT = """---
name: expert
description: 代谢专科专家
version: 1.0.0
type: composite
tool_names: [send_message]
max_call_depth: 2
---
# EXPERT_MARK 代谢专科专家
"""


async def _wait(cond, tries: int = 300) -> bool:
    """轮询等待后台分离 task 收敛(10ms 粒度)。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, body in (("coordinator", _COORD), ("expert", _EXPERT)):
            (root / "skills" / name).mkdir(parents=True)
            (root / "skills" / name / "SKILL.md").write_text(
                body, encoding="utf-8")

        client = RoutingMockClient(routes={
            "COORD_MARK": [
                MockTurn(text="派出代谢专家", tool_calls=[
                    {"id": "s1", "name": "spawn_skill", "arguments":
                     '{"skill_id":"expert","reason":"会诊","args":{}}'}]),
                MockTurn(text="专家已派出"),
            ],
            "EXPERT_MARK": [
                MockTurn(text="初步结论:糖耐量异常,建议复查 OGTT"),
                MockTurn(text="结合家族史修正:升级为糖尿病前期高危,建议立即干预"),
            ],
        })
        pool = await taifeng.EnginePool.create(
            skills_dir=root / "skills", threads_dir=root / "threads",
            model_client=client, compressors=[],
            extra_tools=[make_spawn_skill_tool(), make_send_message_tool(),
                         make_wait_peer_tool()])
        engine = await pool.get_or_create(
            session_id="demo", entry_skill_id="coordinator")

        events: list = []

        async def watch():
            async for ev in engine.subscribe_all():
                events.append(ev.msg)

        task = asyncio.create_task(watch())
        await asyncio.sleep(0)

        # 1. 协调者 turn:LLM 经 spawn_skill 派出专家
        sub_id = await engine.submit(taifeng.UserMessage(text="开始会诊"))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break
        started = next(m for m in events if m.kind == "spawn_started")
        hid = started.data["handle_id"]
        child_tid = started.data["child_thread_id"]
        assert await _wait(
            lambda: engine.spawn_status([hid])[hid]["status"] == "done")
        print(f"[1] 专家首轮完成: {engine.spawn_status([hid])[hid]['result']}")

        # 2. 业务侧 SendToPeer Op:把新发现推给空闲专家并唤醒(trigger_turn)
        await engine.submit(SendToPeer(
            target_thread_id=child_tid,
            text="补充病史:患者父母均有 2 型糖尿病",
            mode="trigger_turn"))
        assert await _wait(lambda: any(
            m.kind == "peer_agent_woken" for m in events))
        print("[2] peer_agent_woken:空闲专家被唤醒(新 detached turn)")

        # 3. wait_peer 语义:等句柄重回终态(这里直接用 engine 公开方法演示)
        from taifeng.loop.cancellation import CancellationToken
        out = await engine.wait_spawn_terminal(
            handle_id=hid, timeout_seconds=10.0,
            cancel=CancellationToken(name="demo"))
        print(f"[3] wait 终态: outcome={out['outcome']} "
              f"status={out['status']}")
        print(f"[4] 专家修正结论: {out['result']}")

        # 4. peer 消息以 source=peer 标注持久化(R5)
        items = [it async for it in await pool.store.load_thread(child_tid)]
        peer = next(it for it in items if it.kind == "user_message"
                    and it.payload.get("source") == "peer")
        print(f"[5] 子 thread 历史中的 peer 项: from={peer.payload['from_thread'][:8]}…"
              f" text={peer.payload['text']}")

        sent = next(m for m in events if m.kind == "peer_message_sent")
        print(f"[6] peer_message_sent: mode={sent.data['mode']} "
              f"via={sent.data['delivered_via']} "
              f"downgraded={sent.data['mode_downgraded']}")

        task.cancel()
        await pool.close()
        print("\n✅ demo 完成:专家被 peer 消息唤醒并产出修正结论(codex multi_agents_v2 范式)")


if __name__ == "__main__":
    asyncio.run(main())
