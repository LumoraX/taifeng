"""capability_matrix 多步编排 driver —— P0 高发链路的真实跑测剧本。

每个 driver 形如 ``async def d(engine, res)``：自行提交 Submission、按 ``res.events``
（subscribe_all 全量采集）轮询等待关键事件、完成自己的终态断言后返回。
判定仍统一走 Scenario.expect 事件集（driver 只负责把链路真实驱动起来）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.loop.submission import Resume, Rewind

if TYPE_CHECKING:
    from collections.abc import Callable


async def _wait_for(res: Any, predicate: Callable[[Any], bool], *,
                    wait_seconds: float = 180.0, what: str = "事件") -> Any:
    """轮询 res.events 直到谓词命中，返回命中的 EventMsg；超时抛 TimeoutError。"""
    deadline = asyncio.get_event_loop().time() + wait_seconds
    while True:
        for msg in list(res.events):
            if predicate(msg):
                return msg
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"等待{what}超时（{wait_seconds}s）")
        await asyncio.sleep(0.05)


def _root_completions(res: Any) -> int:
    """已观测到的 root turn 完成数（is_root 判根——子 turn 的 turn_completed 不算）。"""
    return sum(1 for m in res.events
               if m.kind == "turn_completed" and m.data.get("is_root"))


async def drive_suspend_resume(engine: Any, res: Any) -> None:
    """HITL 挂起 → Resume 续跑：真实 LLM 调 request_user_input → 回填答案。"""
    await engine.submit(taifeng.UserMessage(
        text="我想做一次健康咨询，请按你的流程先向我确认必要信息。"))
    susp = await _wait_for(res, lambda m: m.kind == "turn_suspended", what="turn_suspended")
    req_id = susp.data["pending"][0]["request_id"]
    await engine.submit(Resume(
        thread_id=engine.thread_id,
        resolutions={req_id: {"answer": "今年 42 岁，无慢性病史，最近容易疲劳。"}},
    ))
    await _wait_for(res, lambda m: _root_completions(res) >= 1, what="续跑 root turn_completed")


async def drive_turn_rewind(engine: Any, res: Any) -> None:
    """跑完一轮 → Rewind(re_reason) 回退重推 → 第二次 root 完成。

    陷阱教训（首轮真实回归的假 PASS）：call_skill 子 turn 也 emit turn_completed，
    不按 is_root 判根会在活跃 turn 期间提交 Rewind——节点表 turn 结束才回写，
    必吃 unknown_node 拒绝。这里 ①等 root 完成 ②用 engine.rewind_nodes() 权威表
    取节点 ③提交后盯 rewind_rejected 快速失败。
    """
    await engine.submit(taifeng.UserMessage(
        text="请分析「远程办公对团队协作的影响」并给出结论。"))
    await _wait_for(res, lambda m: _root_completions(res) >= 1, what="首轮 root turn_completed")
    nodes = engine.rewind_nodes()
    if not nodes:
        raise TimeoutError("root turn 完成后 rewind_nodes() 为空")
    await engine.submit(Rewind(node_id=nodes[0].node_id, mode="re_reason"))
    await _wait_for(
        res,
        lambda m: _root_completions(res) >= 2
        or m.kind == "rewind_rejected",
        what="重推后第二次 root turn_completed", wait_seconds=240.0,
    )
    if any(m.kind == "rewind_rejected" for m in res.events):
        rej = next(m for m in res.events if m.kind == "rewind_rejected")
        raise TimeoutError(f"Rewind 被拒: {rej.data}")


async def drive_spawn_join(engine: Any, res: Any) -> None:
    """并发 spawn 多专科 → 错峰 HITL 各自 Resume → join-barrier 聚合。"""
    await engine.submit(taifeng.UserMessage(
        text="患者男 58 岁，确诊高血压十年，近期空腹血糖 7.9：请按会诊流程并发安排"
             "心血管与代谢两个专科分析，最后汇总联合结论。"))
    resumed: set[str] = set()
    # 两个专科各自 HITL 挂起（错峰），逐个回填答案
    for _ in range(2):
        susp = await _wait_for(
            res,
            lambda m: m.kind == "spawn_suspended"
            and m.data.get("thread_id") not in resumed,
            what="spawn_suspended", wait_seconds=240.0,
        )
        child_tid = susp.data["thread_id"]
        req_id = susp.data["pending"][0]["request_id"]
        resumed.add(child_tid)
        await engine.submit(Resume(
            thread_id=child_tid,
            resolutions={req_id: {"answer": "血压 150/95，未规律服药；空腹血糖 7.9。"}},
        ))
    await _wait_for(res, lambda m: m.kind == "join_barrier_fired",
                    what="join_barrier_fired", wait_seconds=300.0)
    await _wait_for(
        res,
        lambda m: m.kind == "spawn_completed"
        and res.kinds.get("spawn_completed", 0) >= 2,
        what="两个专科 spawn_completed", wait_seconds=300.0,
    )


async def drive_peer_messaging(engine: Any, res: Any) -> None:
    """spawn 专家 + send_message 点对点投递 → 等专家终态。"""
    await engine.submit(taifeng.UserMessage(
        text="请启动一个独立调研专家分析「电动车电池回收」议题，并用消息把"
             "「重点关注欧盟新规」这条补充要求发给它，然后等它完成并汇总。"))
    await _wait_for(res, lambda m: m.kind == "peer_message_sent", what="peer_message_sent",
                    wait_seconds=240.0)
    await _wait_for(res, lambda m: m.kind == "spawn_completed", what="spawn_completed",
                    wait_seconds=300.0)
    await _wait_for(res, lambda m: _root_completions(res) >= 1,
                    what="root turn_completed", wait_seconds=300.0)


DRIVERS: dict[str, Any] = {
    "suspend_resume": drive_suspend_resume,
    "turn_rewind": drive_turn_rewind,
    "spawn_join": drive_spawn_join,
    "peer_messaging": drive_peer_messaging,
}
