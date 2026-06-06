"""multi_expert_consult 体验 demo —— 并发多专家 + 错峰 HITL + 联合会诊聚合（纯 MockClient）。

演示内核 **detached-spawn** 能力的完整闭环：

    用户说身体情况
      → orchestrator 一个 turn 内：
           ├─ spawn_skill(cardio-expert)     ┐ 两个专家分离发起、各自后台 child thread
           ├─ spawn_skill(metabolic-expert)  ┘ 立即返回句柄、不阻塞编排 turn
           └─ await_skills([两个句柄], then=joint-consult)  登记 join-barrier
      → 两个专家各自走 **错峰 HITL**：
           cardio 先挂起 → Resume(cardio 的 child thread) → cardio 完成；
           过一会 metabolic 才挂起 → Resume(metabolic 的 child thread) → metabolic 完成。
      → 两个句柄全终态 → join-barrier 自动触发 → joint-consult 起聚合 turn → 最终会诊报告。

错峰（staggered）与 concurrent_fanout 的「同步收齐」对照：
    - concurrent_fanout：一条消息里 N 个 call_skill 同批派发、**同步阻塞**等全部回流，
      HITL 也是被批量收口的——一个 barrier 卡住整批。
    - 本 demo：每个专家是**独立 detached child thread**，各自在自己的节奏上挂起 / 恢复，
      A 先跑完、B 过一会才 HITL，互不耦合；收齐由 join-barrier 异步触发，不占编排 turn。

可视化：自定义事件打印器订阅 subscribe_all，按时间线打印
    spawn_started / spawn_suspended / spawn_completed / join_barrier_registered /
    join_barrier_fired，以及聚合 turn 的最终文本。

运行（MockClient，**无需 API key**）：

    cd taifeng
    PYTHONPATH=src uv run python examples/multi_expert_consult/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.loop.submission import Resume
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
from taifeng.tool.builtins.spawn_skill import (
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
    make_spawn_skill_tool,
)

SKILLS_DIR = Path(__file__).parent / "skills"


def _routing_client() -> RoutingMockClient:
    """按各 skill body 唯一标记路由的 MockClient。

    - orchestrator（ORCH_CONSULT_MARK）：一个 turn 内连发两个 spawn_skill +
      一个 await_skills（登记 join-barrier → joint-consult），再吐收尾文本。
    - cardio-expert（CARDIO_MARK）：turn1 调 request_user_input（挂起），
      Resume 后 turn2 出结论。
    - metabolic-expert（METABOLIC_MARK）：同上，独立节奏。
    - joint-consult（JOINT_CONSULT_MARK）：barrier 触发后自动起，吐最终会诊报告。
    """
    return RoutingMockClient(routes={
        "ORCH_CONSULT_MARK": [
            MockTurn(text="主诉涉及多系统，并发分离发起两个专科专家，收齐后联合会诊。",
                     tool_calls=[
                         {"id": "sp_cardio", "name": "spawn_skill",
                          "arguments": '{"skill_id":"cardio-expert",'
                                       '"reason":"评估心血管风险","args":{}}'},
                         {"id": "sp_metab", "name": "spawn_skill",
                          "arguments": '{"skill_id":"metabolic-expert",'
                                       '"reason":"评估代谢风险","args":{}}'},
                     ]),
            MockTurn(text="编排完成，专家在后台错峰推进，收齐自动联合会诊。"),
        ],
        "CARDIO_MARK": [
            MockTurn(text="心血管专家向用户补问。", tool_calls=[
                {"id": "cardio_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "近期是否有胸闷 / 血压波动？"}'},
            ]),
            MockTurn(text="心血管结论：血压偏高但无急性风险，建议低盐 + 监测。"),
        ],
        "METABOLIC_MARK": [
            MockTurn(text="代谢专家向用户补问。", tool_calls=[
                {"id": "metab_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "近期体重 / 血糖 / 饮食有何变化？"}'},
            ]),
            MockTurn(text="代谢结论：空腹血糖临界，建议控糖 + 复查糖化。"),
        ],
        "JOINT_CONSULT_MARK": [
            MockTurn(text="【联合会诊报告】心血管与代谢双高危：优先控糖控压，"
                          "4 周后心内 + 内分泌联合复诊。"),
        ],
    })


def _print(line: str) -> None:
    """统一前缀的时间线打印（便于人读）。"""
    print(f"  {line}")


async def _drive_orchestrator(engine: taifeng.AgentEngine) -> dict[str, str]:
    """跑 orchestrator 入口 turn，返回两个专家的句柄表 {skill_id: handle_id}。

    必须用 subscribe_all + is_root 过滤判定 root turn 终结（subscribe(sub_id) 会在
    spawn 的工具批处理点上歧义断流）。这里同时收集两条 spawn_started 句柄。
    """
    handles: dict[str, str] = {}
    sub_id = await engine.submit(taifeng.UserMessage(
        text="我最近血压偏高、体重也涨了，帮我看看身体情况。"))
    async for ev in engine.subscribe_all():
        if ev.msg.kind == "spawn_started":
            handles[ev.msg.data["skill_id"]] = ev.msg.data["handle_id"]
        if ev.msg.kind == "assistant_text" and ev.msg.data.get("delta"):
            _print(f"[编排 LLM] {ev.msg.data['delta']}")
        # root turn（orchestrator）跑完即收口；专家在后台继续
        if (ev.msg.kind in ("turn_completed", "turn_failed")
                and ev.submission_id == sub_id
                and ev.msg.data.get("is_root")):
            break
    return handles


async def _wait_event(events: list, kind: str, handle_id: str, tries: int = 300):
    """轮询等待某 handle 的某类事件出现，返回该事件（超时返回 None）。"""
    for _ in range(tries):
        for ev in events:
            if (ev.msg.kind == kind
                    and ev.msg.data.get("handle_id") == handle_id):
                return ev
        await asyncio.sleep(0.01)
    return None


async def _resume_expert(
    engine: taifeng.AgentEngine, events: list, name: str, handle_id: str,
) -> None:
    """等某专家 HITL 挂起 → 取 request_id → Resume 其 child thread → 等其完成。

    错峰的关键就在这个函数的调用次序：demo 串行地先 resume 一个、再 resume 下一个，
    每个专家在自己的 child thread 上独立挂起 / 恢复，互不影响。
    """
    susp = await _wait_event(events, "spawn_suspended", handle_id)
    if susp is None:
        raise RuntimeError(f"{name} 未在预期内挂起（HITL）")
    child_tid = susp.msg.data["thread_id"]
    # DATA 挂起：request_id == call_id，直接从事件 pending 里取（无需读 store）
    req_id = susp.msg.data["pending"][0]["request_id"]
    _print(f"[{name}] spawn_suspended —— HITL 挂起于 child thread {child_tid[:8]}…"
           f"（待答 request_id={req_id}）")
    # Resume 该专家的 child thread，回填问询答案
    await engine.submit(Resume(
        thread_id=child_tid, resolutions={req_id: {"answer": "知道了，已告知"}}))
    done = await _wait_event(events, "spawn_completed", handle_id)
    if done is None:
        raise RuntimeError(f"{name} 未在预期内完成")
    _print(f"[{name}] spawn_completed —— 结论：{done.msg.data.get('result', '')}")


async def main() -> None:
    """端到端跑一次多专家会诊：并发 spawn → 错峰 HITL → join-barrier 聚合。"""
    with tempfile.TemporaryDirectory() as td:
        threads = Path(td) / "threads"
        client = _routing_client()
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=threads,
            model_client=client,
            compressors=[],
            extra_tools=[
                make_spawn_skill_tool(),
                make_await_skills_tool(),
                make_join_skill_tool(),
                make_kill_skill_tool(),
                make_request_user_input_tool(),
            ],
        )
        engine = await pool.get_or_create(
            session_id="multi-expert-consult", entry_skill_id="orchestrator")

        # 全局事件时间线：单独 task 持续订阅 subscribe_all，收集关键事件
        events: list = []

        async def watch() -> None:
            async for ev in engine.subscribe_all():
                events.append(ev)
                if ev.msg.kind == "join_barrier_registered":
                    _print(f"[barrier] join_barrier_registered "
                           f"barrier_id={ev.msg.data.get('barrier_id')} "
                           f"守卫句柄={ev.msg.data.get('handle_ids')}")
                if ev.msg.kind == "shutdown":
                    break

        watch_task = asyncio.create_task(watch())
        await asyncio.sleep(0)  # 让 subscribe_all 先注册队列

        print("\n=== ① 编排入口 turn：并发分离发起两个专家 ===")
        handles = await _drive_orchestrator(engine)
        cardio_hid = handles["cardio-expert"]
        metab_hid = handles["metabolic-expert"]
        _print(f"[编排] spawn_started ×2 —— cardio={cardio_hid} "
               f"metabolic={metab_hid}")

        # 两个专家已在后台分离发起；现在登记 join-barrier（收齐 → joint-consult）。
        # （demo 直接调 engine API 登记，等价于 LLM 调 await_skills；用真实句柄。）
        print("\n=== ② 登记 join-barrier：两专家全跑完 → 自动起联合会诊 ===")
        await engine.set_join_barrier(
            [cardio_hid, metab_hid], then_skill_id="joint-consult")

        print("\n=== ③ 错峰 HITL：cardio 先挂起→恢复→完成；之后 metabolic 才恢复→完成 ===")
        # 错峰：先把 cardio 推到完成，metabolic 此刻仍挂在自己的 HITL 上
        await _resume_expert(engine, events, "cardio-expert", cardio_hid)
        _print("…cardio 已完成；metabolic 仍在自己的 child thread 上等待问诊答复（错峰）")
        await _resume_expert(engine, events, "metabolic-expert", metab_hid)

        print("\n=== ④ 两专家全终态 → join-barrier 自动触发联合会诊聚合 ===")
        fired = await _wait_barrier_fired(events)
        then_tid = fired.msg.data["then_thread_id"]
        _print(f"[barrier] join_barrier_fired —— 自动起 joint-consult "
               f"于 thread {then_tid[:8]}…")
        report = await _read_consult_report(pool, then_tid)
        print("\n=== ⑤ 最终联合会诊报告 ===")
        _print(report)

        # MockClient 瞬时完成，给后台聚合 turn 落盘时间后收尾
        await asyncio.sleep(0.3)
        await pool.close()
        watch_task.cancel()
        print("\n🎉 multi_expert_consult：并发多专家 + 错峰 HITL + 联合会诊聚合 演示完毕")


async def _wait_barrier_fired(events: list, tries: int = 300):
    """轮询等待 join_barrier_fired 事件出现。"""
    for _ in range(tries):
        for ev in events:
            if ev.msg.kind == "join_barrier_fired":
                return ev
        await asyncio.sleep(0.01)
    raise RuntimeError("join-barrier 未在预期内触发")


async def _read_consult_report(
    pool: taifeng.EnginePool, then_tid: str, tries: int = 300,
) -> str:
    """从聚合 child thread 读回 joint-consult 产出的最终 assistant 文本。"""
    for _ in range(tries):
        items = [it async for it in await pool.store.load_thread(then_tid)]
        texts = [
            it.payload.get("text", "")
            for it in items
            if it.kind == "assistant_message"
        ]
        if any(texts):
            return "\n".join(t for t in texts if t)
        await asyncio.sleep(0.01)
    return "(聚合报告尚未落盘)"


if __name__ == "__main__":
    asyncio.run(main())
