"""web_ui detached 能力 smoke —— MockClient 驱动 ASGI app，无需 API key，可复跑。

验证 multi_expert_consult / turn_rewind 两个 detached demo 在 web_ui 的端到端事件流。
**事件订阅走进程内队列**（直接注册到 server._event_subs），绕开 httpx
ASGITransport 对长连 SSE 的整体缓冲；/api/chat、/api/resume 等非流式 POST/GET 仍走 HTTP。
真 LLM 的 await_skills-via-LLM 路径不在此（MockTurn 无法回放运行时 handle_id），
由 README 记的真 LLM 人工跑覆盖；此处 barrier 经 engine API 登记（同 demo.py）。

运行：PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py
退出码 0 = 全绿；非 0 = 某断言失败（打印失败点）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # 让 import server 命中 examples/web_ui/server.py

import httpx  # noqa: E402,I001
import server  # noqa: E402,I001  examples/web_ui/server.py
# 复用 multi_expert_consult demo 的 MockClient 路由（按 skill body 标记路由）
sys.path.insert(0, str(server.EXAMPLES_DIR / "multi_expert_consult"))
from demo import _routing_client  # type: ignore  # noqa: E402

from taifeng.llm.providers import MockClient, MockTurn  # noqa: E402

CALL_ANALYZER = '{"skill_id":"analyzer","reason":"取分析","args":{}}'


def _rewind_mock() -> MockClient:
    """turn_rewind 用的固定 turns：跑完一次自治链 + 一次 retry_tool 重跑。"""
    return MockClient(turns=[
        MockTurn(text="派发分析…", tool_calls=[
            {"id": "a0", "name": "call_skill", "arguments": CALL_ANALYZER}]),
        MockTurn(text="【分析】风险偏高(初版)"),
        MockTurn(text="【综合】建议:加强监测(初版)"),
        # retry_tool 重跑 analyzer 用的后续 turns：
        MockTurn(text="【分析】风险中等(修订)"),
        MockTurn(text="【综合】建议:常规随访(修订)"),
    ])


def _check(cond: bool, msg: str) -> None:
    """断言：失败即打印并以非 0 退出（便于纳入本地校验）。"""
    if not cond:
        print(f"❌ SMOKE FAIL: {msg}")
        raise SystemExit(1)
    print(f"✓ {msg}")


def _subscribe(demo_id: str, session_id: str) -> asyncio.Queue[dict]:
    """在 server._event_subs 注册一个进程内队列，直接收 bridge 推送的事件。

    绕开 httpx ASGITransport 对 SSE 长连的整体缓冲：bridge 按 sub_key 推到该队列，
    smoke 直接 drain。必须在 POST /api/chat 之前注册，bridge 才看得到这个订阅者。
    """
    q: asyncio.Queue[dict] = asyncio.Queue()
    server._event_subs.setdefault(f"{demo_id}:{session_id}", []).append(q)
    return q


async def _pump(q: asyncio.Queue[dict], sink: list[dict]) -> None:
    """后台持续把队列事件累积到 sink（_connected 跳过；bridge 实际不推它）。"""
    while True:
        ev = await q.get()
        if ev.get("kind") != "_connected":
            sink.append(ev)


async def _wait_count(sink: list[dict], kind: str, n: int, tries: int = 500) -> list[dict]:
    """轮询等待 sink 中某 kind 事件累计达到 n 条；返回该类事件列表。"""
    for _ in range(tries):
        got = [e for e in sink if e["kind"] == kind]
        if len(got) >= n:
            return got
        await asyncio.sleep(0.02)
    return [e for e in sink if e["kind"] == kind]


async def smoke_multi_expert() -> None:
    """multi_expert_consult 完整闭环：spawn → 错峰 resume → barrier 聚合。"""
    demo_id, session_id = "multi_expert_consult", "smoke"
    q = _subscribe(demo_id, session_id)
    sink: list[dict] = []
    pump_task = asyncio.create_task(_pump(q, sink))
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/chat", json={
            "message": "我血压偏高、体重也涨了，帮我看看。",
            "demo_id": demo_id, "session_id": session_id})
        _check(r.status_code == 200, f"/api/chat 200（实际 {r.status_code}）")

        spawned = await _wait_count(sink, "spawn_started", 2)
        _check(len(spawned) >= 2, f"2× spawn_started（实际 {len(spawned)}）")
        handles = {e["data"]["skill_id"]: e["data"]["handle_id"] for e in spawned}
        cardio_h = handles["cardio-expert"]
        metab_h = handles["metabolic-expert"]

        # 经 engine API 登记 barrier（真 LLM 走 await_skills 工具；mock 不能回放
        # 运行时 handle_id，故此处直登记，等价于 LLM 自登记的效果）
        pool = server._pools[demo_id]
        engine = await pool.get_or_create(
            session_id=f"{demo_id}:{session_id}", entry_skill_id="orchestrator")
        await engine.set_join_barrier([cardio_h, metab_h],
                                      then_skill_id="joint-consult")

        async def resume_expert(handle_id: str, name: str) -> None:
            susp = None
            for _ in range(500):
                susp = next((e for e in sink
                             if e["kind"] == "spawn_suspended"
                             and e["data"].get("handle_id") == handle_id), None)
                if susp:
                    break
                await asyncio.sleep(0.02)
            _check(susp is not None, f"{name} 出现 spawn_suspended")
            tid = susp["data"]["thread_id"]
            rid = susp["data"]["pending"][0]["request_id"]
            rr = await client.post("/api/resume", json={
                "demo_id": demo_id, "session_id": session_id,
                "thread_id": tid, "request_id": rid,
                "payload": {"answer": "知道了"}})
            _check(rr.status_code == 200, f"{name} /api/resume 200（{rr.status_code}）")

        # 错峰：先 cardio 跑到完成，再 metabolic
        await resume_expert(cardio_h, "cardio")
        await resume_expert(metab_h, "metabolic")

        done = await _wait_count(sink, "spawn_completed", 2)
        _check(len(done) >= 2, f"2× spawn_completed（实际 {len(done)}）")
        fired = await _wait_count(sink, "join_barrier_fired", 1)
        _check(len(fired) >= 1, "join_barrier_fired 出现")
    pump_task.cancel()


async def smoke_turn_rewind() -> None:
    """turn_rewind：chat 跑完自治链 → 取节点 → POST /api/rewind → 重跑回流。"""
    demo_id, session_id = "turn_rewind", "smoke"
    server._model_client = _rewind_mock()
    q = _subscribe(demo_id, session_id)
    sink: list[dict] = []
    pump_task = asyncio.create_task(_pump(q, sink))
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/chat", json={
            "message": "评估健康风险", "demo_id": demo_id, "session_id": session_id})
        _check(r.status_code == 200, f"turn_rewind /api/chat 200（{r.status_code}）")

        # 等根 turn 完成
        for _ in range(500):
            if any(e["kind"] == "turn_completed" and e["data"].get("is_root")
                   for e in sink):
                break
            await asyncio.sleep(0.02)

        nodes = (await client.get(
            f"/api/rewind_nodes/{demo_id}/{session_id}")).json()["nodes"]
        _check(len(nodes) > 0, f"rewind_nodes 非空（{len(nodes)}）")
        disp = next((n for n in nodes
                     if n["kind"] == "dispatch" and n["target_id"] == "call_skill"), None)
        _check(disp is not None, "存在 call_skill 的 dispatch 节点")

        before = len(sink)
        rr = await client.post("/api/rewind", json={
            "demo_id": demo_id, "session_id": session_id,
            "node_id": disp["node_id"], "mode": "retry_tool"})
        _check(rr.status_code == 200, f"/api/rewind 200（{rr.status_code}）")
        for _ in range(500):
            if any(e["kind"] == "turn_completed" and e["data"].get("is_root")
                   for e in sink[before:]):
                break
            await asyncio.sleep(0.02)
        reran = [e for e in sink[before:] if e["kind"] == "assistant_text"]
        _check(len(reran) > 0, "rewind 重跑产生了新的 assistant_text 回流")
    pump_task.cancel()


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        server.STORAGE_DIR = Path(td) / "runs"  # 隔离存储到 tmp
        server._model_client = _routing_client()  # 注入 MockClient
        server._llm_meta = {"provider": "mock", "model": "mock",
                            "context_window": 128_000}
        await smoke_multi_expert()
        await smoke_turn_rewind()
    print("\n🎉 smoke_detached 全绿")


if __name__ == "__main__":
    asyncio.run(main())
