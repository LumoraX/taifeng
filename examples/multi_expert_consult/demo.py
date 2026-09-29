"""multi_expert_consult 体验 demo —— 并发多专家 + 错峰 HITL + 联合评审聚合（纯 SimClient）。

演示内核 **detached-spawn** 能力的完整闭环（场景：多专家并行评审一份上线方案）：

    用户提交评审请求（新订单接口上线）
      → orchestrator 一个 turn 内：
           ├─ spawn_skill(security-expert)   ┐ 两个专家分离发起、各自后台 child thread
           ├─ spawn_skill(perf-expert)       ┘ 立即返回句柄、不阻塞编排 turn
           └─ await_skills([两个句柄], then=joint-review)  登记 join-barrier
      → 两个专家各自走 **错峰 HITL**：
           security 先挂起 → Resume(security 的 child thread) → security 完成；
           过一会 perf 才挂起 → Resume(perf 的 child thread) → perf 完成。
      → 两个句柄全终态 → join-barrier 自动触发 → joint-review 起聚合 turn → 最终联合评审报告。

错峰（staggered）与 concurrent_fanout 的「同步收齐」对照：
    - concurrent_fanout：一条消息里 N 个 call_skill 同批派发、**同步阻塞**等全部回流，
      HITL 也是被批量收口的——一个 barrier 卡住整批。
    - 本 demo：每个专家是**独立 detached child thread**，各自在自己的节奏上挂起 / 恢复，
      A 先跑完、B 过一会才 HITL，互不耦合；收齐由 join-barrier 异步触发，不占编排 turn。

可视化：自定义事件打印器订阅 subscribe_all，按时间线打印
    spawn_started / spawn_suspended / spawn_completed / join_barrier_registered /
    join_barrier_fired，以及聚合 turn 的最终文本。

运行（SimClient，**无需 API key**）：

    cd taifeng
    PYTHONPATH=src uv run python examples/multi_expert_consult/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.loop.submission import Resume
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
from taifeng.tool.builtins.spawn_skill import (
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
    make_spawn_skill_tool,
)

SKILLS_DIR = Path(__file__).parent / "skills"


def _routing_client() -> RoutingSimClient:
    """按各 skill body 唯一标记路由的 SimClient。

    - orchestrator（ORCH_REVIEW_MARK）：一个 turn 内连发两个 spawn_skill +
      一个 await_skills（登记 join-barrier → joint-review），再吐收尾文本。
    - security-expert（SECURITY_MARK）：turn1 调 request_user_input（挂起），
      Resume 后 turn2 出结论。
    - perf-expert（PERF_MARK）：同上，独立节奏。
    - joint-review（JOINT_REVIEW_MARK）：barrier 触发后自动起，吐最终联合评审报告。
    """
    return RoutingSimClient(routes={
        "ORCH_REVIEW_MARK": [
            SimTurn(text="评审请求涉及安全与性能两个专项，并发分离发起两个专家，收齐后联合评审。",
                     tool_calls=[
                         {"id": "sp_security", "name": "spawn_skill",
                          "arguments": '{"skill_id":"security-expert",'
                                       '"reason":"评估安全风险","args":{}}'},
                         {"id": "sp_perf", "name": "spawn_skill",
                          "arguments": '{"skill_id":"perf-expert",'
                                       '"reason":"评估性能风险","args":{}}'},
                     ]),
            SimTurn(text="编排完成，专家在后台错峰推进，收齐自动联合评审。"),
        ],
        "SECURITY_MARK": [
            SimTurn(text="安全评审专家向用户补问。", tool_calls=[
                {"id": "security_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "接口是否对外网开放、鉴权方式？"}'},
            ]),
            SimTurn(text="安全结论：对外开放需补签名校验与限流，其余无高危项。"),
        ],
        "PERF_MARK": [
            SimTurn(text="性能评审专家向用户补问。", tool_calls=[
                {"id": "perf_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "预估峰值 QPS 与数据量？"}'},
            ]),
            SimTurn(text="性能结论：峰值下连接池余量偏紧，建议扩容 + 压测复核。"),
        ],
        "JOINT_REVIEW_MARK": [
            SimTurn(text="【联合评审报告】安全与性能各有一项待办：先补签名校验与限流，"
                          "再扩容连接池并压测复核，通过后灰度上线。"),
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
        text="我们准备上线新的订单接口，帮我从安全和性能两方面评审一下。"))
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
        thread_id=child_tid, resolutions={req_id: {"answer": "已补充，见评审材料"}}))
    done = await _wait_event(events, "spawn_completed", handle_id)
    if done is None:
        raise RuntimeError(f"{name} 未在预期内完成")
    _print(f"[{name}] spawn_completed —— 结论：{done.msg.data.get('result', '')}")


async def main() -> None:
    """端到端跑一次多专家评审：并发 spawn → 错峰 HITL → join-barrier 聚合。"""
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
            session_id="multi-expert-review", entry_skill_id="orchestrator")

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
        security_hid = handles["security-expert"]
        perf_hid = handles["perf-expert"]
        _print(f"[编排] spawn_started ×2 —— security={security_hid} "
               f"perf={perf_hid}")

        # 两个专家已在后台分离发起；现在登记 join-barrier（收齐 → joint-review）。
        # （demo 直接调 engine API 登记，等价于 LLM 调 await_skills；用真实句柄。）
        print("\n=== ② 登记 join-barrier：两专家全跑完 → 自动起联合评审 ===")
        await engine.set_join_barrier(
            [security_hid, perf_hid], then_skill_id="joint-review")

        print("\n=== ③ 错峰 HITL：security 先挂起→恢复→完成；之后 perf 才恢复→完成 ===")
        # 错峰：先把 security 推到完成，perf 此刻仍挂在自己的 HITL 上
        await _resume_expert(engine, events, "security-expert", security_hid)
        _print("…security 已完成；perf 仍在自己的 child thread 上等待补问答复（错峰）")
        await _resume_expert(engine, events, "perf-expert", perf_hid)

        print("\n=== ④ 两专家全终态 → join-barrier 自动触发联合评审聚合 ===")
        fired = await _wait_barrier_fired(events)
        then_tid = fired.msg.data["then_thread_id"]
        _print(f"[barrier] join_barrier_fired —— 自动起 joint-review "
               f"于 thread {then_tid[:8]}…")
        report = await _read_review_report(pool, then_tid)
        print("\n=== ⑤ 最终联合评审报告 ===")
        _print(report)

        # SimClient 瞬时完成，给后台聚合 turn 落盘时间后收尾
        await asyncio.sleep(0.3)
        await pool.close()
        watch_task.cancel()
        print("\n🎉 multi_expert_consult：并发多专家 + 错峰 HITL + 联合评审聚合 演示完毕")


async def _wait_barrier_fired(events: list, tries: int = 300):
    """轮询等待 join_barrier_fired 事件出现。"""
    for _ in range(tries):
        for ev in events:
            if ev.msg.kind == "join_barrier_fired":
                return ev
        await asyncio.sleep(0.01)
    raise RuntimeError("join-barrier 未在预期内触发")


async def _read_review_report(
    pool: taifeng.EnginePool, then_tid: str, tries: int = 300,
) -> str:
    """从聚合 child thread 读回 joint-review 产出的最终 assistant 文本。"""
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
