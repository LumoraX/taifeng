# web_ui 集成「跨根 turn 异步交互」demo 实现计划（detached-spawn + turn-rewind）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 multi_expert_consult（detached-spawn 并发多专家 + 错峰 HITL + 联合会诊）与 turn_rewind（节点重跑）两个交互能力接进 `examples/web_ui/`，浏览器可实时演示。

**Architecture:** 不改内核（`src/`）。给 web_ui 的 `DemoMeta` 加三个布尔开关，给事件桥 `_bridge_events` 加一个 detached 分支（跨提交跟踪 + spawn-aware 退出谓词），detached demo 的 `/api/resume` 不再另起 bridge（杜绝重复推送）。前端按 `handle_id` 渲染「每专家一张卡」+ 卡内并发表单；turn_rewind 新增节点表 + 两个 REST 端点。

**Tech Stack:** Python 3.12 / FastAPI / SSE / vanilla JS（单文件 `static/index.html`）/ httpx ASGITransport（smoke 测试）/ MockClient（CI 安全验证）。

**工作目录：** 全程在 worktree `/Volumes/Codes/Qiuben/qiuben/taifeng/.claude/worktrees/webui-detached/`（分支 `feat/webui-detached`）。所有命令的 CWD = 该 worktree 根；所有路径相对仓库根。

**设计依据：** `docs/superpowers/specs/2026-06-07-webui-detached-interactions-design.md`。

---

## 文件结构

| 文件 | 职责 | 改动 |
| --- | --- | --- |
| `examples/web_ui/server.py` | demo 注册 + pool 构建 + 事件桥 + REST 端点 | 改：DemoMeta 三字段、spawn 工具注入、bridge detached 分支、resume 分流、rewind 两端点、两 DemoMeta 注册 |
| `examples/web_ui/static/index.html` | 单文件前端 UI | 改：专家卡面板 + 并发表单 + spawn/barrier 事件渲染 + rewind 节点表 |
| `examples/web_ui/smoke_detached.py` | MockClient ASGI smoke（自动化验证） | 新增 |
| `examples/multi_expert_consult/skills/orchestrator/SKILL.md` | 编排器入口 skill | 校验/微调：真 LLM 自登记 barrier 指令 |
| `examples/turn_rewind/skills/{orchestrator,analyzer}/SKILL.md` | turn_rewind 的两个 skill | 新增：从 demo.py 内联字符串抽出落盘 |
| `examples/turn_rewind/demo.py` | turn_rewind standalone demo | 改：从磁盘 skills 加载（删 inline + `_write_skills`） |
| `examples/web_ui/README.md`、`examples/README.md` | 索引文档 | 同步：新增两 demo 入口 + detached bridge 说明 |

**验证策略：** 后端逻辑用 `smoke_detached.py`（MockClient，无需 key，可复跑）做集成 TDD 驱动；前端（vanilla JS in HTML，无单测框架）用「smoke 保证后端事件正确 + 真 LLM 浏览器人工跑」双重验证。每个后端任务跑 smoke 对应断言；前端任务跑真 LLM 浏览器确认。

---

## Phase 1 —— multi_expert_consult

### Task 1: smoke 骨架 + 「2× spawn_started」断言（RED）

**Files:**
- Create: `examples/web_ui/smoke_detached.py`

- [ ] **Step 1: 写 smoke 骨架与第一条断言**

创建 `examples/web_ui/smoke_detached.py`。它导入 web_ui `server` 模块、注入 MockClient、用 httpx ASGITransport 驱动 `/api/chat` + SSE，断言事件序列。第一阶段只断言「chat 后收到 2 条 spawn_started」。

```python
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
```

- [ ] **Step 2: 跑 smoke，确认按预期 RED**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`
Expected: FAIL —— `multi_expert_consult` 未注册（`/api/chat` 返回 404）或无 spawn_started（demo 不存在 / bridge 丢事件）。打印 `❌ SMOKE FAIL`。

- [ ] **Step 3: 提交（RED 基线）**

```bash
git add examples/web_ui/smoke_detached.py
git commit -m "test(webui): detached smoke 骨架 + multi_expert spawn_started 断言（RED）"
```

---

### Task 2: DemoMeta 加三个开关

**Files:**
- Modify: `examples/web_ui/server.py:182-188`（`wants_user_input_tool` 字段之后）

- [ ] **Step 1: 在 DemoMeta dataclass 末尾追加三字段**

在 `examples/web_ui/server.py` 的 `DemoMeta` 里、`wants_user_input_tool` 字段之后（约 188 行），追加：

```python
    streams_detached: bool = False
    """True 时事件桥走 detached 分支：不按 submission_id 过滤（spawn 事件
    submission_id=handle_id 不被丢），退出谓词改为「根 turn 终态 ∧ 无存活 spawn ∧
    无未触发 barrier ∧ 无在跑 then_thread」；且 ``/api/resume`` 不再另起 bridge
    （chat bridge 仍存活，resume 续跑事件经它回流，避免重复推送）。
    detached-spawn / turn-rewind 这类「根 turn 完成后仍有异步活动」的 demo 用。"""

    wants_spawn_tools: bool = False
    """True 时把 detached-spawn 的 4 个工具（spawn_skill / await_skills /
    join_skill / kill_skill）作为 extra_tools 注入 pool，让 LLM 能并发分离发起
    子 skill（multi_expert_consult demo 用）。"""

    wants_rewind: bool = False
    """True 时前端在根 turn 完成后拉 ``/api/rewind_nodes`` 渲染回访节点表，
    支持点节点重跑（``/api/rewind``）。仅 turn_rewind demo 置 True。"""
```

- [ ] **Step 2: 校验语法**

Run: `uv run ruff check examples/web_ui/server.py`
Expected: PASS（无新增告警）。

- [ ] **Step 3: 提交**

```bash
git add examples/web_ui/server.py
git commit -m "feat(webui): DemoMeta 加 streams_detached/wants_spawn_tools/wants_rewind 开关"
```

---

### Task 3: 注册 multi_expert_consult demo + 注入 spawn 工具

**Files:**
- Modify: `examples/web_ui/server.py`（import 段约 84 行后；`extra_tools` 拼装段约 770 行后；`DEMOS` dict 末尾约 532 行前）

- [ ] **Step 1: import 四个 spawn 工具**

在 `examples/web_ui/server.py` 第 84 行（`from taifeng.tool.builtins.request_user_input import make_request_user_input_tool`）之后追加：

```python
from taifeng.tool.builtins.spawn_skill import (
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
    make_spawn_skill_tool,
)
```

- [ ] **Step 2: pool 构建时按开关注入 spawn 工具**

在 `_get_or_create_pool` 内、`if meta.wants_user_input_tool:` 块（约 769-770 行）之后追加：

```python
        # opt-in 注入 detached-spawn 四工具（multi_expert_consult demo）
        if meta.wants_spawn_tools:
            extra_tools.extend([
                make_spawn_skill_tool(),
                make_await_skills_tool(),
                make_join_skill_tool(),
                make_kill_skill_tool(),
            ])
```

- [ ] **Step 3: 注册 multi_expert_consult DemoMeta**

在 `DEMOS` dict 内追加一项（放在 `numeric_loop` 等已有项旁，dict 末尾闭合 `}` 之前）：

```python
    "multi_expert_consult": DemoMeta(
        demo_id="multi_expert_consult",
        title="🩺 多专家会诊 (并发 spawn + 错峰 HITL + 联合会诊)",
        description=(
            "orchestrator 一个 turn 内对多个专科 spawn_skill（各自 detached child "
            "thread），await_skills 登记 join-barrier；各专家错峰 HITL，全终态 → "
            "barrier 自动起 joint-consult 聚合。演示 detached-spawn 完整闭环。"
        ),
        skills_dir=EXAMPLES_DIR / "multi_expert_consult" / "skills",
        entry_skill_id="orchestrator",
        sample_prompt="我最近血压偏高、体重也涨了，帮我看看身体情况。",
        hitl_on_skill_dispatch=False,
        streams_detached=True,
        wants_spawn_tools=True,
        wants_user_input_tool=True,
    ),
```

- [ ] **Step 4: 校验**

Run: `uv run ruff check examples/web_ui/server.py`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add examples/web_ui/server.py
git commit -m "feat(webui): 注册 multi_expert_consult demo + 注入 spawn 四工具"
```

---

### Task 4: bridge detached 分支 +「2× spawn_started」转 GREEN

**Files:**
- Modify: `examples/web_ui/server.py`（`_bridge_events` 约 990-1039 行；`/api/chat` 调用处约 986 行）

- [ ] **Step 1: `_bridge_events` 加 `detached` 形参与分支**

把 `_bridge_events` 签名改为带 `detached`，并在函数体里按 detached 走不同过滤/退出逻辑。完整替换 `_bridge_events` 函数（990-1039 行）为：

```python
async def _bridge_events(
    demo_id: str, session_id: str, engine: taifeng.AgentEngine, sub_id: str,
    *, detached: bool = False,
) -> None:
    """把 engine 事件流翻译成 dict 推给前端订阅者。

    **非 detached（默认）**：按 submission_id 过滤本提交事件；根 turn 终态或根
    thread 挂起即退出（见 form_hitl / code_review 等 demo）。

    **detached**（streams_detached demo）：engine 已 session 隔离，故不按
    submission_id 过滤（spawn 事件 submission_id=handle_id / barrier 事件
    =barrier_id / resume 事件=resume sub.id 都要转发）。退出谓词纯事件驱动：
    根 turn 终态 ∧ ``engine.has_live_spawns()`` 为假 ∧ 无未触发 barrier ∧
    无在跑 then_thread —— 保证 spawn 后台活动与 join-barrier 触发的 joint-consult
    输出都不会被提前截断。
    """
    sub_key = f"{demo_id}:{session_id}"
    # detached 退出谓词的 bookkeeping
    root_done = False
    open_barriers: set[str] = set()        # registered 未 fired 的 barrier
    pending_then_threads: set[str] = set()  # fired 后聚合 turn 仍在跑的 then_thread
    try:
        async for ev in engine.subscribe_all():
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if not detached:
                # ── 原有 per-submission 行为，保持不变 ──
                if ev.submission_id != sub_id:
                    continue
                payload = {"kind": ev.msg.kind, "submission_id": ev.submission_id,
                           "data": data}
                for q in _event_subs.get(sub_key, []):
                    q.put_nowait(payload)
                if ev.msg.kind in ("turn_completed", "turn_failed") and data.get(
                    "is_root", False
                ):
                    break
                if ev.msg.kind == "turn_suspended" and (
                    data.get("thread_id") == engine.thread_id
                ):
                    break
                continue

            # ── detached 分支：转发全部本 session 事件 ──
            payload = {"kind": ev.msg.kind, "submission_id": ev.submission_id,
                       "data": data}
            for q in _event_subs.get(sub_key, []):
                q.put_nowait(payload)

            # bookkeeping
            if ev.msg.kind in ("turn_completed", "turn_failed") and data.get(
                "is_root", False
            ):
                root_done = True
            elif ev.msg.kind == "join_barrier_registered":
                bid = data.get("barrier_id")
                if bid:
                    open_barriers.add(bid)
            elif ev.msg.kind == "join_barrier_fired":
                bid = data.get("barrier_id")
                if bid:
                    open_barriers.discard(bid)
                then_tid = data.get("then_thread_id")
                if then_tid:
                    pending_then_threads.add(then_tid)
            elif ev.msg.kind in ("turn_completed", "turn_failed"):
                # 聚合 turn（then_thread）跑完 → 解除其挂起标记
                pending_then_threads.discard(data.get("thread_id"))

            # 退出谓词
            if (root_done and not engine.has_live_spawns()
                    and not open_barriers and not pending_then_threads):
                break
    except Exception:
        logger.exception("event bridge failed for sub_key=%s", sub_key)
```

- [ ] **Step 2: `/api/chat` 传 detached 开关**

把 `/api/chat` 里启动桥接的那行（约 986 行）：

```python
    asyncio.create_task(_bridge_events(req.demo_id, req.session_id, engine, sub_id))
```

改为：

```python
    asyncio.create_task(_bridge_events(
        req.demo_id, req.session_id, engine, sub_id,
        detached=meta.streams_detached))
```

- [ ] **Step 3: 跑 smoke，确认「2× spawn_started」转 GREEN**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`
Expected: 打印 `✓ /api/chat 200` 与 `✓ 收到 2 条 spawn_started`（脚本到此已无更多断言，整体退出 0 / `🎉 全绿`）。

- [ ] **Step 4: 校验 + 提交**

Run: `uv run ruff check examples/web_ui/server.py`
Expected: PASS。

```bash
git add examples/web_ui/server.py
git commit -m "feat(webui): 事件桥 detached 分支（跨提交跟踪 + spawn-aware 退出）"
```

---

### Task 5: smoke 扩到完整闭环（错峰 resume + barrier）（RED）

**Files:**
- Modify: `examples/web_ui/smoke_detached.py`

- [ ] **Step 1: 扩展 `smoke_multi_expert` 到完整闭环**

把 `smoke_multi_expert` 整体替换为下面版本：先收 2× spawn_started 拿句柄与 child thread；经 engine API 登记 barrier；监听 spawn_suspended 后对各专家 `/api/resume`；断言 2× spawn_completed + join_barrier_fired。SSE 用一个持续订阅任务累积全部事件。

```python
async def smoke_multi_expert() -> None:
    """multi_expert_consult 完整闭环：spawn → 错峰 resume → barrier 聚合。"""
    demo_id, session_id = "multi_expert_consult", "smoke"
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        all_events: list[dict] = []

        async def pump() -> None:
            url = f"/api/events/{demo_id}/{session_id}"
            async with client.stream("GET", url, timeout=30.0) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        p = json.loads(line[len("data:"):].strip())
                        if p.get("kind") != "_connected":
                            all_events.append(p)

        pump_task = asyncio.create_task(pump())
        await asyncio.sleep(0.05)

        r = await client.post("/api/chat", json={
            "message": "我血压偏高、体重也涨了，帮我看看。",
            "demo_id": demo_id, "session_id": session_id})
        _check(r.status_code == 200, f"/api/chat 200（实际 {r.status_code}）")

        # 等 2 条 spawn_started
        async def wait_count(kind: str, n: int, tries: int = 500) -> list[dict]:
            for _ in range(tries):
                got = [e for e in all_events if e["kind"] == kind]
                if len(got) >= n:
                    return got
                await asyncio.sleep(0.02)
            return [e for e in all_events if e["kind"] == kind]

        spawned = await wait_count("spawn_started", 2)
        _check(len(spawned) == 2, f"2× spawn_started（实际 {len(spawned)}）")
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

        # 错峰 resume：先 cardio、后 metabolic
        async def resume_expert(handle_id: str, name: str) -> None:
            for _ in range(500):
                susp = next((e for e in all_events
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
            _check(rr.status_code == 200, f"{name} /api/resume 200")

        await resume_expert(cardio_h, "cardio")
        await resume_expert(metab_h, "metabolic")

        done = await wait_count("spawn_completed", 2)
        _check(len(done) == 2, f"2× spawn_completed（实际 {len(done)}）")
        fired = await wait_count("join_barrier_fired", 1)
        _check(len(fired) >= 1, "join_barrier_fired 出现")

        pump_task.cancel()
```

- [ ] **Step 2: 跑 smoke，确认 RED**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`
Expected: 在 resume 后 **可能** FAIL —— detached demo 的 `/api/resume` 当前仍另起一条 bridge（与 chat bridge 重复推送），且 resume 是否能续跑取决于路由；预期此处出现重复事件或断言不稳。若本步已偶发 GREEN，仍继续 Task 6 落实「resume 不另起 bridge」以消除重复推送（确定性修复）。

- [ ] **Step 3: 提交（RED 基线）**

```bash
git add examples/web_ui/smoke_detached.py
git commit -m "test(webui): smoke 扩到 multi_expert 完整闭环（错峰 resume + barrier）"
```

---

### Task 6: detached 的 `/api/resume` 不另起 bridge → 闭环 GREEN

**Files:**
- Modify: `examples/web_ui/server.py`（`resume_form` 约 1137-1139 行）

- [ ] **Step 1: resume 对 detached demo 跳过新 bridge**

在 `resume_form`（`/api/resume`）里，把无条件起桥那段（约 1137-1139 行）：

```python
    asyncio.create_task(
        _bridge_events(req.demo_id, req.session_id, engine, sub_id)
    )
```

改为：

```python
    # detached demo 的 chat bridge 仍存活（has_live_spawns 含 suspended），resume
    # 续跑事件经它回流；再起一条会重复推送，故仅非 detached demo 才另起 bridge。
    if not meta.streams_detached:
        asyncio.create_task(
            _bridge_events(req.demo_id, req.session_id, engine, sub_id)
        )
```

> `meta` 在 `resume_form` 内已有（约 1128 行 `meta = DEMOS[req.demo_id]`）。

- [ ] **Step 2: 跑 smoke，确认完整闭环 GREEN**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`
Expected: 全部 `✓` —— 2× spawn_started、barrier 登记、cardio/metabolic resume 200、2× spawn_completed、join_barrier_fired，末尾 `🎉 smoke_detached 全绿`，退出码 0。

- [ ] **Step 3: 校验 + 提交**

Run: `uv run ruff check examples/web_ui/server.py examples/web_ui/smoke_detached.py`
Expected: PASS。

```bash
git add examples/web_ui/server.py
git commit -m "fix(webui): detached demo 的 /api/resume 不另起 bridge（消除重复推送）"
```

---

### Task 7: 前端 —— 每专家一张卡 + 并发表单 + spawn/barrier 事件渲染

**Files:**
- Modify: `examples/web_ui/static/index.html`（CSS 段约 300-320 行；事件渲染 `summarize`/`routeToChat` 约 757-910 行；表单逻辑 `maybeShowForm`/`renderForm` 约 912-969 行；HTML 结构 + 全局 state 约 454-505 行）

> 前端为单文件 vanilla JS，无单测框架。本任务验证 = Task 6 smoke 已绿（后端事件正确）+ Task 8 真 LLM 浏览器人工跑确认卡片渲染。

- [ ] **Step 1: 加专家面板 HTML 容器 + 最终报告区**

在表单弹窗 HTML（`<!-- 表单型 HITL` 约 454 行）之前插入一个专家面板容器（默认隐藏，detached demo 才显示）：

```html
<!-- 专家面板（detached-spawn demo）：每个 spawn 一张卡，卡内可并发独立表单 -->
<div id="expert-panel" class="expert-panel" style="display:none">
  <div class="ep-title">专家面板</div>
  <div id="expert-cards"></div>
  <div id="consult-report" class="consult-report" style="display:none">
    <div class="cr-title">联合会诊报告</div>
    <div id="consult-report-body" class="cr-body"></div>
  </div>
</div>
```

- [ ] **Step 2: 加专家卡 / barrier / 表单 CSS**

在事件颜色 CSS 区（约 308 行 `.evt.k-hitl_required` 附近）追加：

```css
  /* 专家面板 + 卡片 */
  .expert-panel { border:1px solid var(--border); border-radius:8px; padding:10px; margin:8px 0; }
  .ep-title { font-weight:600; margin-bottom:6px; }
  .expert-card { border-left:4px solid var(--border); background:#1a1f29; border-radius:6px; padding:8px 10px; margin:6px 0; }
  .expert-card.running { border-left-color:#3b82f6; }
  .expert-card.waiting { border-left-color:var(--warn,#d6a700); }
  .expert-card.done    { border-left-color:#16a34a; }
  .expert-card.error   { border-left-color:var(--danger); }
  .ec-head { display:flex; justify-content:space-between; font-weight:600; }
  .ec-status { font-size:12px; opacity:.85; }
  .ec-result { margin-top:6px; white-space:pre-wrap; }
  .ec-form .fld { margin:6px 0; }
  .ec-form textarea { width:100%; }
  .consult-report { margin-top:8px; border-top:1px dashed var(--border); padding-top:8px; }
  .cr-title { font-weight:600; }
  .cr-body { white-space:pre-wrap; }
  /* spawn / barrier 事件时间线着色 */
  .evt.k-spawn_started .kind { color:#3b82f6; }
  .evt.k-spawn_suspended .kind { color:var(--warn,#d6a700); }
  .evt.k-spawn_completed .kind { color:#16a34a; }
  .evt.k-spawn_failed .kind, .evt.k-spawn_cancelled .kind { color:var(--danger); }
  .evt.k-join_barrier_registered .kind, .evt.k-join_barrier_fired .kind { color:#a855f7; }
```

- [ ] **Step 3: 全局 state + 卡片状态机**

在前端全局 state 区（约 482-505 行 `let resumeThreadId` 附近）追加专家卡注册表：

```javascript
// 专家卡注册表：handle_id → {el, skillId, formAnchor}。detached-spawn demo 用。
let expertCards = {};
```

在 `maybeShowForm` 函数（约 916 行）之前插入卡片状态机函数：

```javascript
// ── 专家卡（detached-spawn）────────────────────────────────────────
// 每个 spawn 一张卡；spawn_suspended 时卡内渲染独立表单，各自 thread_id+request_id
// 独立 Resume —— 支持错峰并发（A 已完成、B 仍待答）。
function showExpertPanel() { $("expert-panel").style.display = "block"; }

function ensureCard(handleId, skillId) {
  if (expertCards[handleId]) return expertCards[handleId];
  showExpertPanel();
  const el = document.createElement("div");
  el.className = "expert-card running";
  el.innerHTML =
    `<div class="ec-head"><span>${escapeHtml(skillId || handleId)}</span>` +
    `<span class="ec-status">running</span></div>` +
    `<div class="ec-body"></div>`;
  $("expert-cards").appendChild(el);
  const card = { el, skillId, formAnchor: null };
  expertCards[handleId] = card;
  return card;
}

function setCardStatus(card, statusCls, statusText) {
  card.el.className = `expert-card ${statusCls}`;
  card.el.querySelector(".ec-status").textContent = statusText;
}

// spawn_suspended：在卡内渲染表单。复用 renderFormInto（见 Step 4 重构）。
function showCardForm(handleId, data) {
  const card = expertCards[handleId];
  if (!card || card.formAnchor) return;
  const pend = (data.pending || []).find(
    (p) => p.detail && p.detail.response_schema) || (data.pending || [])[0];
  if (!pend) return;
  card.formAnchor = {
    thread_id: data.thread_id, request_id: pend.request_id,
    schema: pend.detail && pend.detail.response_schema,
  };
  setCardStatus(card, "waiting", "等你回答");
  const body = card.el.querySelector(".ec-body");
  body.innerHTML = `<div class="ec-prompt"></div><div class="ec-form"></div>` +
    `<button class="ec-submit">提交</button>`;
  body.querySelector(".ec-prompt").textContent =
    (pend.detail && pend.detail.prompt) || "请填写以下信息";
  renderFormInto(body.querySelector(".ec-form"), card.formAnchor.schema);
  body.querySelector(".ec-submit").onclick = () => submitCardForm(handleId);
}

async function submitCardForm(handleId) {
  const card = expertCards[handleId];
  if (!card || !card.formAnchor) return;
  const payload = collectFormFrom(card.el.querySelector(".ec-form"));
  const anchor = card.formAnchor;
  card.formAnchor = null;
  setCardStatus(card, "running", "处理中…");
  card.el.querySelector(".ec-body").innerHTML =
    "📝 已提交：" + escapeHtml(JSON.stringify(payload));
  try {
    const resp = await fetch("/api/resume", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        demo_id: demoSelect.value, session_id: activeSession(),
        thread_id: anchor.thread_id, request_id: anchor.request_id, payload }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
  } catch (e) {
    setCardStatus(card, "error", "提交失败");
    card.el.querySelector(".ec-body").textContent = "❌ " + e.message;
  }
}
```

- [ ] **Step 4: 把 `renderForm`/`collectForm` 重构成可复用（不破坏现有 form_hitl）**

现有 `renderForm(prompt, schema)`（932 行）写死操作 `$("form-fields")`；抽出纯渲染核心 `renderFormInto(container, schema)` 供卡片复用，`renderForm` 改为调用它。同理 `collectForm`（972 行）抽出 `collectFormFrom(container)`。

在 `renderForm` 之前插入：

```javascript
// 把 JSON Schema 渲染进任意容器（卡片表单 / 弹窗表单共用）。
function renderFormInto(box, schema) {
  const props = (schema && schema.properties) || {};
  const required = (schema && schema.required) || [];
  box.innerHTML = "";
  for (const [key, def] of Object.entries(props)) {
    const title = (def && def.title) || key;
    const isReq = required.includes(key);
    const fld = document.createElement("div");
    fld.className = "fld"; fld.dataset.key = key;
    let kind, inner = "";
    if (def && Array.isArray(def.enum)) {
      kind = "single";
      inner = def.enum.map((o) =>
        `<label class="opt"><input type="radio" name="f_${key}" value="${escapeHtml(o)}">`
        + `<span>${escapeHtml(o)}</span></label>`).join("");
    } else if (def && def.type === "array" && def.items && Array.isArray(def.items.enum)) {
      kind = "multi";
      inner = def.items.enum.map((o) =>
        `<label class="opt"><input type="checkbox" name="f_${key}" value="${escapeHtml(o)}">`
        + `<span>${escapeHtml(o)}</span></label>`).join("");
    } else {
      kind = "text";
      inner = `<textarea name="f_${key}" rows="2" placeholder="请输入"></textarea>`;
    }
    fld.dataset.kind = kind;
    const hint = { single: "单选", multi: "多选", text: "问答" }[kind];
    fld.innerHTML = `<span class="lbl">${escapeHtml(title)}`
      + (isReq ? `<span class="req">*</span>` : "")
      + `<span class="hint">(${hint})</span></span>` + inner;
    box.appendChild(fld);
  }
}

// 从任意容器收集表单值（single→字符串 / multi→数组 / text→字符串）。
function collectFormFrom(box) {
  const payload = {};
  for (const fld of box.querySelectorAll(".fld")) {
    const key = fld.dataset.key, kind = fld.dataset.kind;
    if (kind === "single") {
      const sel = fld.querySelector(`input[name="f_${key}"]:checked`);
      payload[key] = sel ? sel.value : "";
    } else if (kind === "multi") {
      payload[key] = [...fld.querySelectorAll(`input[name="f_${key}"]:checked`)].map((c) => c.value);
    } else {
      payload[key] = fld.querySelector(`[name="f_${key}"]`).value.trim();
    }
  }
  return payload;
}
```

把现有 `renderForm` 函数体（934-968 行，`$("form-prompt")` 之后到 `$("form-bg").classList.add("show")`）替换为复用：

```javascript
function renderForm(prompt, schema) {
  $("form-prompt").textContent = prompt;
  $("form-err").textContent = "";
  renderFormInto($("form-fields"), schema);
  $("form-bg").classList.add("show");
}
```

并把现有 `collectForm()`（972-988 行）整体替换为：

```javascript
function collectForm() { return collectFormFrom($("form-fields")); }
```

- [ ] **Step 5: 在事件分发里驱动卡片 + 报告 + timeline summary**

在 `summarize` 的 `switch`（约 807 行 `default:` 之前）追加 spawn/barrier 摘要：

```javascript
    case "spawn_started":
      return `▶ ${data.skill_id}  handle=${truncate(data.handle_id||"",12)}`;
    case "spawn_suspended":
      return `⏸ handle=${truncate(data.handle_id||"",12)} thread=${truncate(data.thread_id||"",12)}`;
    case "spawn_completed":
      return `✓ handle=${truncate(data.handle_id||"",12)}  ${truncate(data.result||"",120)}`;
    case "spawn_failed":
    case "spawn_cancelled":
      return `✗ handle=${truncate(data.handle_id||"",12)}`;
    case "join_barrier_registered":
      return `barrier=${truncate(data.barrier_id||"",12)} 守卫=${(data.handle_ids||[]).length}`;
    case "join_barrier_fired":
      return `barrier 触发 → then_thread=${truncate(data.then_thread_id||"",12)}`;
```

在 `routeToChat` 末尾（约 909 行 `if (kind === "turn_suspended")` 块之后）追加 detached 处理。注意：detached demo 的 spawn 卡内表单走 `showCardForm`，根 thread 的 `turn_suspended` 不再弹全局表单（避免与卡冲突）：

```javascript
  // ── detached-spawn 卡片状态机 ──
  if (kind === "spawn_started") {
    ensureCard(data.handle_id, data.skill_id);
  }
  if (kind === "spawn_suspended") {
    showCardForm(data.handle_id, data);
  }
  if (kind === "spawn_completed") {
    const c = expertCards[data.handle_id];
    if (c) { setCardStatus(c, "done", "已完成");
      c.el.querySelector(".ec-body").innerHTML =
        `<div class="ec-result">${escapeHtml(data.result||"")}</div>`; }
  }
  if (kind === "spawn_failed" || kind === "spawn_cancelled") {
    const c = expertCards[data.handle_id];
    if (c) setCardStatus(c, "error", kind === "spawn_failed" ? "失败" : "已取消");
  }
  if (kind === "join_barrier_fired") {
    $("consult-report").style.display = "block";
    consultThreadId = data.then_thread_id;  // 标记聚合 thread，其 assistant_text 汇入报告
  }
```

把现有 `maybeShowForm` 的早退条件加上「detached demo 不走全局表单」。在 `maybeShowForm`（916 行）开头加：

```javascript
function maybeShowForm(data) {
  // detached demo 的表单走专家卡（showCardForm），不弹全局表单
  if (window.__activeStreamsDetached) return;
  if (formSuspension) return;
```

并把 `assistant_text` 渲染（861 行）改为：聚合 thread 的文本汇入报告区。在 `if (kind === "assistant_text" && data.delta) {` 块开头加分流：

```javascript
  if (kind === "assistant_text" && data.delta) {
    if (typeof consultThreadId !== "undefined" && consultThreadId
        && data.thread_id === consultThreadId) {
      const body = $("consult-report-body");
      body.textContent += data.delta;
      return;
    }
```

> `consultThreadId` 需在全局 state 区声明：`let consultThreadId = null;`。

- [ ] **Step 6: 切 demo 时设置 detached 标志 + 清空卡片**

前端拿 `/api/demos` 时每条已带（Task 注：需在 `/api/demos` 响应里补 `streams_detached`/`wants_rewind`，见 Step 7）。在切换 demo 的处理处（搜索 `demoSelect` 的 change handler 或加载 demo meta 的地方）设置 `window.__activeStreamsDetached` 并重置卡片：

```javascript
// 切 demo / 新会话时：重置专家卡与报告区
function resetExpertPanel() {
  expertCards = {}; consultThreadId = null;
  $("expert-cards").innerHTML = "";
  $("consult-report").style.display = "none";
  $("consult-report-body").textContent = "";
  $("expert-panel").style.display = "none";
}
```

在选中 demo 后（已有 demo meta 对象处）设：`window.__activeStreamsDetached = !!meta.streams_detached;` 并调用 `resetExpertPanel()`（detached demo 才会在 spawn_started 时重新 showExpertPanel）。

- [ ] **Step 7: `/api/demos` 透出 detached/rewind 标志**

在 `list_demos`（server.py 约 946-958 行）的每条 demo dict 里补两字段：

```python
                "streams_detached": m.streams_detached,
                "wants_rewind": m.wants_rewind,
```

- [ ] **Step 8: smoke 回归 + ruff**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`
Expected: 仍全绿（前端改动不影响后端事件；`/api/demos` 新字段不破坏 smoke）。

Run: `uv run ruff check examples/web_ui/server.py`
Expected: PASS。

- [ ] **Step 9: 提交**

```bash
git add examples/web_ui/static/index.html examples/web_ui/server.py
git commit -m "feat(webui): multi_expert 前端 —— 每专家一卡 + 并发表单 + spawn/barrier 渲染"
```

---

### Task 8: 校验/微调 orchestrator SKILL.md（真 LLM 自登记 barrier）

**Files:**
- Modify（按需）: `examples/multi_expert_consult/skills/orchestrator/SKILL.md`

- [ ] **Step 1: 看现状**

Run: `PYTHONPATH=src uv run python -m taifeng skill show examples/multi_expert_consult/skills orchestrator`
Expected: 打印 frontmatter + body。检查 body 是否明确指示 LLM：① 对每个相关专科 `spawn_skill`，② 用 spawn 返回的 handle_id 调 `await_skills(handle_ids=[...], then_skill_id="joint-consult")`。

- [ ] **Step 2: 缺指令则补 body（仅补指令文字，不动 frontmatter 契约）**

若 body 未明确两步，编辑 `SKILL.md` body，加入明确工作流指令（示例文字，按现有 body 风格融入）：

```markdown
## 工作流程
1. 判断主诉涉及哪些专科，对每个专科调用 `spawn_skill(skill_id=..., reason=..., args=...)` 并发分离发起；记下每次返回的 `handle_id`。
2. 收齐所有 `handle_id` 后，调用一次 `await_skills(handle_ids=[全部 handle_id], then_skill_id="joint-consult")` 登记联合会诊；barrier 会在所有专家完成后自动起 joint-consult 聚合。
3. 输出一句话说明已并发发起、将自动联合会诊，然后结束本 turn（专家在后台错峰推进）。
```

- [ ] **Step 3: 真 LLM 端到端人工验证（.env 真 key）**

Run: `PYTHONPATH=src uv run python examples/web_ui/server.py`（另起终端），浏览器开 `http://localhost:8765`，选「🩺 多专家会诊」demo，发 sample_prompt。
Expected（人工观察）：时间线出现 `spawn_started`×N → `join_barrier_registered`（LLM 自调 await_skills）→ 各专家卡错峰出表单 → 逐个填 → `spawn_completed`×N → `join_barrier_fired` → 「联合会诊报告」区出文本。截图/记录到 commit message 或 PR。

> 若真 LLM 未稳定调用 await_skills，回 Step 2 强化 body 指令；这是 demo，允许多试几次 prompt。

- [ ] **Step 4: 提交（如有 SKILL.md 改动）**

```bash
git add examples/multi_expert_consult/skills/orchestrator/SKILL.md
git commit -m "docs(skill): orchestrator 明确 spawn + await_skills 工作流（真 LLM 自登记 barrier）"
```

> 若 Step 1 显示 body 已充分、无需改动，跳过 Step 4，仅在任务记录里写明「已核对，无需改」。

---

## Phase 2 —— turn_rewind

### Task 9: 把 turn_rewind 的 skill 落盘（单一真相）

**Files:**
- Create: `examples/turn_rewind/skills/orchestrator/SKILL.md`
- Create: `examples/turn_rewind/skills/analyzer/SKILL.md`
- Modify: `examples/turn_rewind/demo.py`（删 inline 字符串 + `_write_skills`，改读磁盘）

- [ ] **Step 1: 看现有内联 skill 文本**

Run: `PYTHONPATH=src uv run python -c "import ast,sys; src=open('examples/turn_rewind/demo.py').read(); print(src[src.index('ANALYZER_SKILL'):src.index('def _write_skills')])"`
Expected: 打印 `ANALYZER_SKILL` 与 `ORCHESTRATOR_SKILL` 两个三引号字符串原文。

- [ ] **Step 2: 落盘两个 SKILL.md**

把 `ANALYZER_SKILL` 内容写入 `examples/turn_rewind/skills/analyzer/SKILL.md`，`ORCHESTRATOR_SKILL` 写入 `examples/turn_rewind/skills/orchestrator/SKILL.md`（内容 = 字符串去掉首尾换行后的原文；frontmatter + body 一字不改，保持 `entry: true`/`entry: false`/`child_skills: [analyzer]` 不变）。

- [ ] **Step 3: demo.py 改读磁盘**

把 `examples/turn_rewind/demo.py` 里 `ANALYZER_SKILL`/`ORCHESTRATOR_SKILL` 两个常量与 `_write_skills` 函数删除；新增模块级常量并把两处 `skills = _write_skills(root)` 改为指向磁盘：

```python
SKILLS_DIR = Path(__file__).parent / "skills"
```

两处 `EnginePool.create(skills_dir=skills, ...)` 改为 `skills_dir=SKILLS_DIR`；`with tempfile.TemporaryDirectory() as ...` 仍保留用于 `threads_dir`（落盘 thread 到 tmp）。删除对 `_write_skills` 的调用与 `root / "skills"` 引用。

- [ ] **Step 4: 跑 turn_rewind demo 回归**

Run: `PYTHONPATH=src uv run python examples/turn_rewind/demo.py`
Expected: 与改动前一致——两个场景（retry_tool / re_reason）都跑通，打印重跑结果，无异常，退出 0。

- [ ] **Step 5: 提交**

```bash
git add examples/turn_rewind/skills examples/turn_rewind/demo.py
git commit -m "refactor(turn_rewind): skill 落盘为单一真相，demo.py 改读磁盘"
```

---

### Task 10: 注册 turn_rewind demo

**Files:**
- Modify: `examples/web_ui/server.py`（`DEMOS` dict）

- [ ] **Step 1: 注册 DemoMeta**

在 `DEMOS` dict 追加：

```python
    "turn_rewind": DemoMeta(
        demo_id="turn_rewind",
        title="⏮ Turn 回退重跑 (retry_tool / re_reason)",
        description=(
            "orchestrator 自治链跑完后，可回退到任意回访节点重跑：retry_tool 重跑"
            "一次 call_skill 换其输出、父基于新结论续推；re_reason 截到某圈采样前让 "
            "LLM 重新决定。演示 addressable turn-rewind。"
        ),
        skills_dir=EXAMPLES_DIR / "turn_rewind" / "skills",
        entry_skill_id="orchestrator",
        sample_prompt="帮我评估这位患者的健康风险并给建议。",
        hitl_on_skill_dispatch=False,
        streams_detached=True,
        wants_rewind=True,
    ),
```

- [ ] **Step 2: 校验 + 提交**

Run: `uv run ruff check examples/web_ui/server.py`
Expected: PASS。

```bash
git add examples/web_ui/server.py
git commit -m "feat(webui): 注册 turn_rewind demo（streams_detached + wants_rewind）"
```

---

### Task 11: rewind 两端点

**Files:**
- Modify: `examples/web_ui/server.py`（端点区，约 `/api/resume` 之后；新增 Pydantic model 到 model 定义区约 912-922 行）

- [ ] **Step 1: 新增 `RewindRequest` model**

在 `ResumeFormRequest` 类（约 922 行）之后追加：

```python
class RewindRequest(BaseModel):
    """节点重跑：回退到 root turn 录下的某回访节点并主动重推。"""

    demo_id: str
    session_id: str = "default"
    node_id: str
    """目标回访节点（来自 /api/rewind_nodes）。"""
    mode: str = "re_reason"
    """``re_reason``（截到采样前重新决定）/ ``retry_tool``（仅 dispatch 节点，重跑该工具）。"""
    new_args: dict[str, Any] | None = None
    """仅 retry_tool + dispatch 节点有意义：替换重跑入参。"""
```

- [ ] **Step 2: 新增 `GET /api/rewind_nodes/{demo_id}/{session_id}`**

在 `/api/resume` 端点（`resume_form`）之后追加：

```python
@app.get("/api/rewind_nodes/{demo_id}/{session_id}")
async def rewind_nodes(demo_id: str, session_id: str) -> dict[str, Any]:
    """列出当前 session engine 的回访节点（turn-rewind 前端节点表数据源）。"""
    if demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo_id: {demo_id}")
    pool = _pools.get(demo_id)
    if pool is None:
        return {"nodes": []}
    meta = DEMOS[demo_id]
    engine = await pool.get_or_create(
        session_id=f"{demo_id}:{session_id}", entry_skill_id=meta.entry_skill_id)
    nodes = [
        {
            "node_id": cp.node_id,
            "kind": cp.kind,
            "target_id": cp.target_id,
            "args_digest": cp.args_digest,
            "iteration_index": cp.iteration_index,
        }
        for cp in engine.rewind_nodes()
    ]
    return {"nodes": nodes}
```

- [ ] **Step 3: 新增 `POST /api/rewind`**

紧接其后追加：

```python
@app.post("/api/rewind")
async def rewind(req: RewindRequest) -> dict[str, Any]:
    """提交 Rewind op 重跑某节点；重跑事件经 detached bridge 回流前端。"""
    from taifeng.loop.submission import Rewind

    if req.demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo_id: {req.demo_id}")
    pool = _pools.get(req.demo_id)
    if pool is None:
        raise HTTPException(409, "no active pool for this demo")
    meta = DEMOS[req.demo_id]
    engine = await pool.get_or_create(
        session_id=f"{req.demo_id}:{req.session_id}",
        entry_skill_id=meta.entry_skill_id)
    sub_id = await engine.submit(Rewind(
        node_id=req.node_id, mode=req.mode, new_args=req.new_args))  # type: ignore[arg-type]
    asyncio.create_task(_bridge_events(
        req.demo_id, req.session_id, engine, sub_id, detached=True))
    logger.info("rewind: demo=%s node=%s mode=%s", req.demo_id, req.node_id, req.mode)
    return {"submission_id": sub_id, "demo_id": req.demo_id}
```

- [ ] **Step 4: 校验 + 提交**

Run: `uv run ruff check examples/web_ui/server.py`
Expected: PASS。

```bash
git add examples/web_ui/server.py
git commit -m "feat(webui): /api/rewind_nodes + /api/rewind 两端点"
```

---

### Task 12: smoke 扩到 turn_rewind（RED→GREEN）

**Files:**
- Modify: `examples/web_ui/smoke_detached.py`

- [ ] **Step 1: 加 turn_rewind smoke**

在 `smoke_detached.py` 加一个 `smoke_turn_rewind`，并在 `main()` 里调用（需用 turn_rewind 的 MockClient——复用其 demo.py 的路由 turn；turn_rewind/demo.py 用 `MockClient(turns=[...])` 非 routing，故 smoke 自建一个简单 MockClient 跑出含 call_skill 的根 turn）。turn_rewind 的入口是 `orchestrator`→`call_skill(analyzer)`→综合。MockClient 用一组固定 turns（够跑完一次 + 一次重跑）：

```python
from taifeng.llm.providers import MockClient, MockTurn  # noqa: E402

CALL_ANALYZER = '{"skill_id":"analyzer","reason":"取分析","args":{}}'

def _rewind_mock() -> MockClient:
    return MockClient(turns=[
        MockTurn(text="派发分析…", tool_calls=[
            {"id": "a0", "name": "call_skill", "arguments": CALL_ANALYZER}]),
        MockTurn(text="【分析】风险偏高(初版)"),
        MockTurn(text="【综合】建议:加强监测(初版)"),
        # 一次 retry_tool 重跑 analyzer 用的后续 turns：
        MockTurn(text="【分析】风险中等(修订)"),
        MockTurn(text="【综合】建议:常规随访(修订)"),
    ])


async def smoke_turn_rewind() -> None:
    """turn_rewind：chat 跑完自治链 → 取节点 → POST /api/rewind → 重跑回流。"""
    demo_id, session_id = "turn_rewind", "smoke"
    server._model_client = _rewind_mock()
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        all_events: list[dict] = []

        async def pump() -> None:
            async with client.stream(
                "GET", f"/api/events/{demo_id}/{session_id}", timeout=30.0) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        p = json.loads(line[len("data:"):].strip())
                        if p.get("kind") != "_connected":
                            all_events.append(p)

        pump_task = asyncio.create_task(pump())
        await asyncio.sleep(0.05)
        r = await client.post("/api/chat", json={
            "message": "评估健康风险", "demo_id": demo_id, "session_id": session_id})
        _check(r.status_code == 200, f"turn_rewind /api/chat 200（{r.status_code}）")

        # 等根 turn 完成
        for _ in range(500):
            if any(e["kind"] == "turn_completed" and e["data"].get("is_root")
                   for e in all_events):
                break
            await asyncio.sleep(0.02)

        nodes = (await client.get(
            f"/api/rewind_nodes/{demo_id}/{session_id}")).json()["nodes"]
        _check(len(nodes) > 0, f"rewind_nodes 非空（{len(nodes)}）")
        disp = next((n for n in nodes
                     if n["kind"] == "dispatch" and n["target_id"] == "call_skill"), None)
        _check(disp is not None, "存在 call_skill 的 dispatch 节点")

        before = len(all_events)
        rr = await client.post("/api/rewind", json={
            "demo_id": demo_id, "session_id": session_id,
            "node_id": disp["node_id"], "mode": "retry_tool"})
        _check(rr.status_code == 200, f"/api/rewind 200（{rr.status_code}）")
        for _ in range(500):
            if any(e["kind"] == "turn_completed" and e["data"].get("is_root")
                   for e in all_events[before:]):
                break
            await asyncio.sleep(0.02)
        reran = [e for e in all_events[before:] if e["kind"] == "assistant_text"]
        _check(len(reran) > 0, "rewind 重跑产生了新的 assistant_text 回流")
        pump_task.cancel()
```

在 `main()` 里、`smoke_multi_expert()` 之后追加调用：

```python
        await smoke_turn_rewind()
```

> 注意：`smoke_turn_rewind` 内部把 `server._model_client` 切到 `_rewind_mock()`；保持它在 `smoke_multi_expert()` 之后调用。

- [ ] **Step 2: 跑 smoke**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`
Expected: multi_expert 段全绿 + turn_rewind 段全绿（rewind_nodes 非空、dispatch 节点存在、/api/rewind 200、重跑 assistant_text 回流），末尾 `🎉 全绿`。

- [ ] **Step 3: 提交**

```bash
git add examples/web_ui/smoke_detached.py
git commit -m "test(webui): smoke 扩到 turn_rewind（节点表 + rewind 重跑回流）"
```

---

### Task 13: 前端 —— rewind 节点表 + 交互

**Files:**
- Modify: `examples/web_ui/static/index.html`（HTML 结构、CSS、根 turn 完成回调、新函数）

> 验证 = smoke 已绿（后端）+ 真 LLM 浏览器人工跑。

- [ ] **Step 1: 加节点表 HTML 容器**

在专家面板容器（Task 7 Step 1 加的 `#expert-panel`）之后插入：

```html
<!-- Rewind 节点表（turn_rewind demo）：根 turn 完成后列出回访节点 -->
<div id="rewind-panel" class="rewind-panel" style="display:none">
  <div class="rp-title">回访节点（点节点重跑）</div>
  <table id="rewind-table"><tbody></tbody></table>
</div>
```

- [ ] **Step 2: 加 CSS**

在 Task 7 的 CSS 之后追加：

```css
  .rewind-panel { border:1px solid var(--border); border-radius:8px; padding:10px; margin:8px 0; }
  .rp-title { font-weight:600; margin-bottom:6px; }
  #rewind-table { width:100%; border-collapse:collapse; font-size:13px; }
  #rewind-table td { padding:4px 6px; border-bottom:1px solid var(--border); }
  .rw-actions button { margin-right:6px; }
```

- [ ] **Step 3: 根 turn 完成后拉节点表**

在 `routeToChat` 里根 turn 收尾块（约 895 行 `if ((kind === "turn_completed" || kind === "turn_failed") && data.is_root)`）末尾追加：

```javascript
    if (window.__activeWantsRewind) loadRewindNodes();
```

新增 `loadRewindNodes` / 渲染 / 触发函数（放在 `resetExpertPanel` 附近）：

```javascript
// 拉回访节点表并渲染（turn_rewind demo）。
async function loadRewindNodes() {
  try {
    const did = demoSelect.value, sid = activeSession();
    const r = await fetch(`/api/rewind_nodes/${encodeURIComponent(did)}/${encodeURIComponent(sid)}`);
    if (!r.ok) return;
    const nodes = (await r.json()).nodes || [];
    const tb = $("rewind-table").querySelector("tbody");
    tb.innerHTML = "";
    if (!nodes.length) { $("rewind-panel").style.display = "none"; return; }
    $("rewind-panel").style.display = "block";
    for (const n of nodes) {
      const tr = document.createElement("tr");
      const isDispatch = n.kind === "dispatch";
      tr.innerHTML =
        `<td>${escapeHtml(n.node_id)}</td><td>${escapeHtml(n.kind)}</td>` +
        `<td>${escapeHtml(n.target_id||"")}</td>` +
        `<td class="rw-actions"></td>`;
      const act = tr.querySelector(".rw-actions");
      const reBtn = document.createElement("button");
      reBtn.textContent = "re_reason";
      reBtn.onclick = () => doRewind(n.node_id, "re_reason");
      act.appendChild(reBtn);
      if (isDispatch) {
        const rtBtn = document.createElement("button");
        rtBtn.textContent = "retry_tool";
        rtBtn.onclick = () => doRewind(n.node_id, "retry_tool");
        act.appendChild(rtBtn);
      }
      tb.appendChild(tr);
    }
  } catch (e) { /* demo 容错：节点表拉取失败不阻断主流程 */ }
}

// 触发一次 rewind 重跑（事件经 SSE 回流到 timeline）。
async function doRewind(nodeId, mode) {
  try {
    const r = await fetch("/api/rewind", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        demo_id: demoSelect.value, session_id: activeSession(),
        node_id: nodeId, mode }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    $("send").disabled = true;  // 重跑期间锁输入，根 turn 完成回调会解锁
  } catch (e) {
    alert("rewind 失败：" + e.message);
  }
}
```

- [ ] **Step 4: 切 demo 时设置 wants_rewind 标志 + 隐藏节点表**

在 Task 7 Step 6 的 demo 切换处补：`window.__activeWantsRewind = !!meta.wants_rewind;`，并在 `resetExpertPanel()` 里追加 `$("rewind-panel").style.display = "none";`。

- [ ] **Step 5: RewindRejected 提示**

在 `summarize` 的 switch 加：

```javascript
    case "rewind_rejected":
      return `✗ ${truncate(data.reason||data.node_id||"",120)}`;
    case "rewind_checkpoint_recorded":
      return `节点 ${truncate(data.node_id||"",24)}`;
```

> 事件 kind 命名以内核实际为准：实现时先 `grep -n "kind" src/taifeng/loop/event.py | grep -i rewind` 确认 `RewindRejected`/`RewindCheckpointRecorded` 对应的 wire kind 字符串，按实际填 `case`。

- [ ] **Step 6: smoke 回归 + 真 LLM 人工跑**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`
Expected: 全绿。

真 LLM：`PYTHONPATH=src uv run python examples/web_ui/server.py` → 浏览器选「⏮ Turn 回退重跑」→ 发 prompt → 根 turn 完成后出节点表 → 点 retry_tool/re_reason → timeline 出重跑事件。记录到 commit/PR。

- [ ] **Step 7: 提交**

```bash
git add examples/web_ui/static/index.html
git commit -m "feat(webui): turn_rewind 前端 —— 回访节点表 + retry_tool/re_reason 触发"
```

---

## Phase 3 —— 文档同步

### Task 14: 文档同步

**Files:**
- Modify: `examples/web_ui/README.md`、`examples/README.md`
- Modify（若需）: `docs/architecture/agent-loop.md` 或 `docs/architecture/capabilities/{detached-spawn,turn-rewind}.md`（补「web_ui 已接入」一行）

- [ ] **Step 1: examples/README.md 加两 demo 入口**

在 `examples/README.md` 的「多文件示例」表或对应分组，给 `web_ui/` 行补充「已接入 multi_expert_consult / turn_rewind 交互」；并确认 `multi_expert_consult/`、`turn_rewind/` 行注明「已注册进 web_ui」。

- [ ] **Step 2: web_ui/README.md 记 detached bridge + 两 demo + smoke**

在 `examples/web_ui/README.md` 增一节「detached 交互 demo」，写明：
- `streams_detached`/`wants_spawn_tools`/`wants_rewind` 三开关的含义；
- 事件桥 detached 分支的退出谓词；
- multi_expert（每专家卡 + 并发表单）与 turn_rewind（节点表）的交互说明；
- `smoke_detached.py` 的运行方式与覆盖范围（含「await_skills-via-LLM 仅真 LLM 覆盖」的说明）。

- [ ] **Step 3: 校验文档链接**

Run: `PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py`（最后全量回归一次）
Expected: 全绿。

人工：通读两个 README 改动，确认路径/命令准确。

- [ ] **Step 4: 提交**

```bash
git add examples/README.md examples/web_ui/README.md docs/
git commit -m "docs(webui): 同步 detached-spawn + turn-rewind 两 demo 接入说明"
```

---

## 收尾（全部任务后）

- [ ] **内核回归**：`PYTHONPATH=src uv run pytest tests/ -q` 全绿（本次不动内核，兜底确认）。
- [ ] **smoke 全量**：`PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py` 全绿。
- [ ] **standalone demo 回归**：`PYTHONPATH=src uv run python examples/multi_expert_consult/demo.py` 与 `examples/turn_rewind/demo.py` 均无异常。
- [ ] **真 LLM 浏览器人工跑**：两个 demo 各跑一遍，记录结果。
- [ ] 用 `superpowers:finishing-a-development-branch` 收尾（合并/PR + 清理 worktree）。

---

## 自检记录（writing-plans self-review）

**1. Spec coverage：**
- 脊柱（DemoMeta 三开关 / bridge detached 分支 / resume 分流）→ Task 2,4,6 + Step（list_demos 透字段在 Task 7 Step 7）。✓
- spawn 工具注入 → Task 3。✓
- multi_expert 前端每专家卡 + 并发表单 + 事件渲染 → Task 7。✓
- orchestrator SKILL.md 真 LLM 自登记 barrier → Task 8。✓
- turn_rewind skill 落盘 + demo.py 改读盘 → Task 9。✓
- rewind 两端点 + 前端节点表 → Task 11,13。✓
- smoke（MockClient ASGI）+ 内核回归 + 真 LLM 人工 → Task 1,5,12 + 收尾。✓
- 错误边界（resume 拒/rewind 拒/重复推送/无 key）→ bridge 分支 + resume 分流 + Task 13 Step 5。✓
- 文档同步 → Task 14。✓
- R1–R5 不变（仅动 examples/）。✓

**2. Placeholder scan：** 唯一「按实际填」之处 = Task 13 Step 5 的 rewind 事件 wire kind 字符串，已给出确认命令（grep event.py），非占位而是「实现时一条 grep 即定」。其余均含完整代码。✓

**3. Type/名一致：** `streams_detached`/`wants_spawn_tools`/`wants_rewind`（Task 2）→ 全程同名；`_bridge_events(..., detached=...)`（Task 4）→ Task 6/11 调用一致；`expertCards`/`consultThreadId`/`renderFormInto`/`collectFormFrom`（Task 7）→ Task 13 复用一致；`RewindRequest`/`rewind_nodes`/`rewind`（Task 11）→ Task 12/13 一致。✓
