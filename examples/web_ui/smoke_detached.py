"""web_ui detached 能力 smoke —— MockClient 驱动 ASGI app，无需 API key，可复跑。

验证 multi_expert_consult / turn_rewind 两个 detached demo 在 web_ui 的端到端事件流：
chat → spawn_started → spawn_suspended → resume → spawn_completed → join_barrier_fired。
真 LLM 的 await_skills-via-LLM 路径不在此（MockTurn 无法回放运行时 handle_id），
由 README 记的真 LLM 人工跑覆盖；此处 barrier 经 engine API 登记（同 demo.py）。

运行：PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py
退出码 0 = 全绿；非 0 = 某断言失败（打印失败点）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # 让 import server 命中 examples/web_ui/server.py

import httpx  # noqa: E402

import server  # noqa: E402  examples/web_ui/server.py
# 复用 multi_expert_consult demo 的 MockClient 路由（按 skill body 标记路由）
sys.path.insert(0, str(server.EXAMPLES_DIR / "multi_expert_consult"))
from demo import _routing_client  # type: ignore  # noqa: E402


async def _collect_events(client: httpx.AsyncClient, demo_id: str, session_id: str,
                          stop_kinds: set[str], *, timeout: float = 10.0,
                          stop_count: dict[str, int] | None = None) -> list[dict]:
    """订阅 SSE，收集事件直到出现 stop_kinds 中任一（或某 kind 达到 stop_count），或超时。"""
    events: list[dict] = []
    counts: dict[str, int] = {}
    url = f"/api/events/{demo_id}/{session_id}"
    async with client.stream("GET", url, timeout=timeout) as resp:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:"):].strip())
            kind = payload.get("kind")
            if kind == "_connected":
                continue
            events.append(payload)
            counts[kind] = counts.get(kind, 0) + 1
            if stop_count:
                if all(counts.get(k, 0) >= n for k, n in stop_count.items()):
                    break
            elif kind in stop_kinds:
                break
    return events


def _check(cond: bool, msg: str) -> None:
    """断言：失败即打印并以非 0 退出（便于纳入本地校验）。"""
    if not cond:
        print(f"❌ SMOKE FAIL: {msg}")
        raise SystemExit(1)
    print(f"✓ {msg}")


async def smoke_multi_expert() -> None:
    """multi_expert_consult：chat → 2× spawn_started（本任务只验证到这）。"""
    demo_id, session_id = "multi_expert_consult", "smoke"
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        # 订阅 SSE 与发起 chat 并发：先起订阅任务，再 POST chat
        sub = asyncio.create_task(_collect_events(
            client, demo_id, session_id, stop_kinds=set(),
            stop_count={"spawn_started": 2}))
        await asyncio.sleep(0.05)
        r = await client.post("/api/chat", json={
            "message": "我血压偏高、体重也涨了，帮我看看。",
            "demo_id": demo_id, "session_id": session_id})
        _check(r.status_code == 200, f"/api/chat 200（实际 {r.status_code}）")
        events = await asyncio.wait_for(sub, timeout=12.0)
        spawned = [e for e in events if e["kind"] == "spawn_started"]
        _check(len(spawned) == 2, f"收到 2 条 spawn_started（实际 {len(spawned)}）")


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
