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
    """multi_expert_consult：chat → 2× spawn_started（本阶段只验证到这）。"""
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
        _check(len(spawned) >= 2, f"收到 2 条 spawn_started（实际 {len(spawned)}）")
    pump_task.cancel()


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        server.STORAGE_DIR = Path(td) / "runs"  # 隔离存储到 tmp
        server._model_client = _routing_client()  # 注入 MockClient
        server._llm_meta = {"provider": "mock", "model": "mock",
                            "context_window": 128_000}
        await smoke_multi_expert()
    print("\n🎉 smoke_detached 全绿")


if __name__ == "__main__":
    asyncio.run(main())
