# 分离式 skill spawn + join-barrier 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 taifeng 加「分离式 sub-skill spawn」—— 主编排者并发起多个专家、各自独立 suspend-resume/完成(错峰 HITL),并用 join-barrier 在全终态时自动起聚合 skill;LLM 工具 + 业务 API 双入口,句柄/屏障落 store 支持冷恢复。

**Architecture:** 每个 spawn = parent engine 下的独立 child thread + 分离 asyncio 任务(复用 `_spawn_sub_runner`,差别是不 await、不阻塞父 turn);句柄登记在 `SpawnHandleRegistry`;child 的 HITL 走放宽前提的 `_handle_child_resume` 独立续跑;barrier 在 `spawn_completed`/`spawn_failed` 后检查全终态 → 自动 `submit` 聚合 turn。engine 引用计数保活(有未终结 detached child / 未触发 barrier 不释放)。

**Tech Stack:** Python 3.12 + anyio/asyncio,pydantic/dataclass,pytest(`asyncio_mode=auto`),MockClient/RoutingMockClient。所有测试 `PYTHONPATH=src uv run pytest`。

设计 spec:`docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md`

---

## 文件结构(决策锁定)

| 文件 | 职责 | 动作 |
| --- | --- | --- |
| `src/taifeng/loop/spawn_handle.py` | `SpawnHandle`(frozen 数据)+ `SpawnStatus` 字面量 + `SpawnHandleRegistry`(登记/查询/状态更新/重建) + `JoinBarrier` | 新建 |
| `src/taifeng/loop/submission.py` | 无新 Op(spawn 经工具/engine API;resume 复用现有 `Resume`) | 不改 |
| `src/taifeng/loop/event.py` | 7 个 spawn/barrier 事件 + MsgKind + Msg union | 改 |
| `src/taifeng/loop/turn.py` | `run_sub_skill` 已有;无需改(detached 在 engine 侧调 `_spawn_sub_runner` 等价逻辑) | 视情况只读 |
| `src/taifeng/loop/engine.py` | `spawn_skill()` / `set_join_barrier()` / `spawn_status()` / `kill_spawn()` API + 分离任务驱动 + 句柄回写事件 + barrier 触发 + 引用计数保活 + 冷恢复重建 + detached child resume 放宽 | 改(主战场) |
| `src/taifeng/loop/pool.py` | engine 释放条件接入「无未终结 detached child」 | 改 |
| `src/taifeng/conversation/models.py` | `spawn` / `join_barrier` / `join_barrier_fired` 三类 ResponseItem 工厂(落 parent thread,冷恢复用) | 改 |
| `src/taifeng/tool/builtins/spawn_skill.py` | `spawn_skill` / `await_skills` / `join_skill` / `kill_skill` 四工具(薄封装 engine API,经 ctx.extras 取 engine handle) | 新建 |
| `src/taifeng/tool/builtins/__init__.py` | 注册四工具 | 改 |
| `tests/loop/test_detached_spawn.py` | 全部测试 | 新建 |
| `docs/architecture/capabilities/detached-spawn.md` | 能力契约 | 新建 |
| `docs/decisions/0015-detached-skill-spawn.md` | ADR | 新建 |
| `docs/architecture/agent-loop.md` / `configurable-knobs.md` | 活文档 + 旋钮 | 改 |
| `examples/multi_expert_consult/{demo.py,README.md}` | 体验 example | 新建 |

> 关键约束:`src/taifeng/loop/engine.py` 已近大文件,新增 API 控制在必要量;若超 800 行硬线,把 spawn 相关方法抽到 `loop/spawn_driver.py`(engine 持有一个 `_SpawnDriver`)。**Task 12 含一次规模检查**。

---

## Task 1: SpawnHandle + SpawnHandleRegistry 数据结构

**Files:**
- Create: `src/taifeng/loop/spawn_handle.py`
- Test: `tests/loop/test_detached_spawn.py`

- [ ] **Step 1: 写失败测试**

```python
"""分离式 skill spawn + join-barrier 测试。设计见
docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md"""
from __future__ import annotations

from taifeng.loop.spawn_handle import SpawnHandle, SpawnHandleRegistry


def test_registry_register_and_lookup() -> None:
    reg = SpawnHandleRegistry()
    h = reg.register(handle_id="sp0", skill_id="analyzer", child_thread_id="t-1")
    assert h.status == "running"
    assert reg.get("sp0") is h
    assert reg.get("nope") is None


def test_registry_set_status_terminal() -> None:
    reg = SpawnHandleRegistry()
    reg.register(handle_id="sp0", skill_id="a", child_thread_id="t-1")
    reg.set_result("sp0", status="done", result="结论A")
    h = reg.get("sp0")
    assert h.status == "done" and h.result == "结论A"
    assert reg.is_terminal("sp0")


def test_registry_all_terminal() -> None:
    reg = SpawnHandleRegistry()
    reg.register(handle_id="a", skill_id="x", child_thread_id="t1")
    reg.register(handle_id="b", skill_id="x", child_thread_id="t2")
    reg.set_result("a", status="done", result="ra")
    assert not reg.all_terminal(["a", "b"])
    reg.set_result("b", status="error", result="boom")
    assert reg.all_terminal(["a", "b"])  # done + error 都算终态
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py -q`
Expected: FAIL — `ModuleNotFoundError: taifeng.loop.spawn_handle`

- [ ] **Step 3: 写实现**

```python
"""分离式 spawn 的句柄登记 + join-barrier(纯数据/机制,无 IO)。

句柄只记 child thread 引用 + 终态/结果;可由 parent thread 的 spawn 项重建(冷恢复)。
设计:docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SpawnStatus = Literal["running", "suspended", "done", "error", "cancelled"]
_TERMINAL: frozenset[str] = frozenset({"done", "error", "cancelled"})


@dataclass
class SpawnHandle:
    """一次分离式 spawn 的运行态句柄(handle_id ↔ child_thread_id 一一对应)。"""
    handle_id: str
    skill_id: str
    child_thread_id: str
    status: SpawnStatus = "running"
    result: str | None = None


@dataclass(frozen=True)
class JoinBarrier:
    """登记「{句柄集}全终态 → 起聚合 skill」;fired 幂等由 store 标记保证。"""
    barrier_id: str
    handle_ids: tuple[str, ...]
    then_skill_id: str
    then_args_template: dict | None = None


@dataclass
class SpawnHandleRegistry:
    """句柄登记表 + barrier 集;engine 持有,可由 store 项重建。"""
    handles: dict[str, SpawnHandle] = field(default_factory=dict)
    barriers: dict[str, JoinBarrier] = field(default_factory=dict)

    def register(self, *, handle_id: str, skill_id: str, child_thread_id: str) -> SpawnHandle:
        h = SpawnHandle(handle_id=handle_id, skill_id=skill_id, child_thread_id=child_thread_id)
        self.handles[handle_id] = h
        return h

    def get(self, handle_id: str) -> SpawnHandle | None:
        return self.handles.get(handle_id)

    def set_result(self, handle_id: str, *, status: SpawnStatus, result: str | None) -> None:
        h = self.handles[handle_id]
        h.status = status
        h.result = result

    def is_terminal(self, handle_id: str) -> bool:
        h = self.handles.get(handle_id)
        return h is not None and h.status in _TERMINAL

    def all_terminal(self, handle_ids: list[str]) -> bool:
        return all(self.is_terminal(hid) for hid in handle_ids)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/spawn_handle.py tests/loop/test_detached_spawn.py
git commit -m "feat(loop): SpawnHandle + SpawnHandleRegistry + JoinBarrier 数据结构"
```

---

## Task 2: spawn 事件类型(event.py)

**Files:**
- Modify: `src/taifeng/loop/event.py`(MsgKind literal、新增 7 个 `_Msg` 子类、Msg union)
- Test: `tests/loop/test_detached_spawn.py`

- [ ] **Step 1: 写失败测试**

```python
def test_spawn_event_kinds() -> None:
    from taifeng.loop.event import (
        JoinBarrierFired, JoinBarrierRegistered, SpawnCancelled,
        SpawnCompleted, SpawnFailed, SpawnStarted, SpawnSuspended,
    )
    assert SpawnStarted().kind == "spawn_started"
    assert SpawnSuspended().kind == "spawn_suspended"
    assert SpawnCompleted().kind == "spawn_completed"
    assert SpawnFailed().kind == "spawn_failed"
    assert SpawnCancelled().kind == "spawn_cancelled"
    assert JoinBarrierRegistered().kind == "join_barrier_registered"
    assert JoinBarrierFired().kind == "join_barrier_fired"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_event_kinds -q`
Expected: FAIL — ImportError

- [ ] **Step 3: 写实现**

在 `event.py` 的 `MsgKind` literal 末尾(`rewind_rejected` 之后)追加:

```python
    # detached-spawn 生命周期
    "spawn_started",
    "spawn_suspended",
    "spawn_completed",
    "spawn_failed",
    "spawn_cancelled",
    "join_barrier_registered",
    "join_barrier_fired",
```

在 `RewindRejected` 类之后追加 7 个类(每个仿 `RewindCheckpointRecorded` 样式):

```python
class SpawnStarted(_Msg):
    """data = {handle_id, skill_id, child_thread_id}"""
    kind: Literal["spawn_started"] = "spawn_started"

class SpawnSuspended(_Msg):
    """data = {handle_id, thread_id, pending}(= 该 child thread 的挂起)"""
    kind: Literal["spawn_suspended"] = "spawn_suspended"

class SpawnCompleted(_Msg):
    """data = {handle_id, result}"""
    kind: Literal["spawn_completed"] = "spawn_completed"

class SpawnFailed(_Msg):
    """data = {handle_id, error}"""
    kind: Literal["spawn_failed"] = "spawn_failed"

class SpawnCancelled(_Msg):
    """data = {handle_id}"""
    kind: Literal["spawn_cancelled"] = "spawn_cancelled"

class JoinBarrierRegistered(_Msg):
    """data = {barrier_id, handle_ids, then_skill_id}"""
    kind: Literal["join_barrier_registered"] = "join_barrier_registered"

class JoinBarrierFired(_Msg):
    """data = {barrier_id, then_thread_id}"""
    kind: Literal["join_barrier_fired"] = "join_barrier_fired"
```

在 `Msg = Union[...]` 末尾(`RewindRejected` 之后)追加这 7 个类名。

- [ ] **Step 4: 跑测试确认通过 + 无回归**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_event_kinds tests/loop/test_events.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/event.py tests/loop/test_detached_spawn.py
git commit -m "feat(loop): spawn/barrier 七类事件(R3)"
```

---

## Task 3: store 落盘项(冷恢复用的 spawn / join_barrier / fired 项)

**Files:**
- Modify: `src/taifeng/conversation/models.py`(新增三个工厂,仿 `function_call` / `system_injection`)
- Test: `tests/loop/test_detached_spawn.py`

- [ ] **Step 1: 写失败测试**

```python
def test_spawn_response_items() -> None:
    from taifeng.conversation.models import (
        join_barrier_item, join_barrier_fired_item, spawn_item,
    )
    si = spawn_item(handle_id="sp0", skill_id="a", child_thread_id="t1", thread_id="root")
    assert si.kind == "spawn" and si.payload["handle_id"] == "sp0"
    bi = join_barrier_item(barrier_id="b0", handle_ids=["sp0"],
                           then_skill_id="merge", then_args_template=None, thread_id="root")
    assert bi.kind == "join_barrier" and bi.payload["barrier_id"] == "b0"
    fi = join_barrier_fired_item(barrier_id="b0", then_thread_id="t9", thread_id="root")
    assert fi.kind == "join_barrier_fired"
```

- [ ] **Step 2: 跑确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_response_items -q`
Expected: FAIL — ImportError

- [ ] **Step 3: 写实现**

在 `models.py` 末尾追加(`ResponseItem.kind` 是自由字符串字段;如其为受限 Literal,需同步在该 Literal 加 `spawn` / `join_barrier` / `join_barrier_fired` —— 实现前先 `grep -n "kind:" src/taifeng/conversation/models.py` 确认):

```python
def spawn_item(*, handle_id: str, skill_id: str, child_thread_id: str,
               thread_id: str) -> ResponseItem:
    """parent thread 落:一次分离 spawn 的句柄锚(冷恢复重建 registry)。"""
    return ResponseItem(kind="spawn", thread_id=thread_id, payload={
        "handle_id": handle_id, "skill_id": skill_id, "child_thread_id": child_thread_id})


def join_barrier_item(*, barrier_id: str, handle_ids: list[str], then_skill_id: str,
                      then_args_template: dict | None, thread_id: str) -> ResponseItem:
    """parent thread 落:一个 join-barrier 登记锚。"""
    return ResponseItem(kind="join_barrier", thread_id=thread_id, payload={
        "barrier_id": barrier_id, "handle_ids": list(handle_ids),
        "then_skill_id": then_skill_id, "then_args_template": then_args_template})


def join_barrier_fired_item(*, barrier_id: str, then_thread_id: str,
                            thread_id: str) -> ResponseItem:
    """parent thread 落:barrier 已触发的幂等标记(重载不重复触发)。"""
    return ResponseItem(kind="join_barrier_fired", thread_id=thread_id, payload={
        "barrier_id": barrier_id, "then_thread_id": then_thread_id})
```

- [ ] **Step 4: 跑确认通过 + 持久化回归**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_response_items tests/conversation -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/conversation/models.py tests/loop/test_detached_spawn.py
git commit -m "feat(conversation): spawn/join_barrier/fired 三类 ResponseItem(冷恢复落盘)"
```

---

## Task 4: engine.spawn_skill() —— 分离发起 + 立即返回句柄

**Files:**
- Modify: `src/taifeng/loop/engine.py`(新增 `_spawn_registry` 字段 + `spawn_skill()` + 分离驱动 `_drive_spawn()`)
- Test: `tests/loop/test_detached_spawn.py`

实现要点(基于已读源码):
- `engine.spawn_skill(skill_id, args, reason)`:
  1. 取 `target = self._snapshot.get(skill_id)`;`None` → 抛/返回 error(`unknown_skill`)。
  2. 过 `DispatchPolicy.check`(用 entry 的 call_stack,即 `CallStack().push(entry)`)+ `SpawnSlotRegistry.reserve()`(K1)。
  3. 生成 `handle_id = f"sp_{secrets.token_hex(4)}"`;调 `await self._store.create_thread(entry_skill_id=skill_id, source=f"spawn:{self._entry_skill.id}", extra={parent_thread_id, ...})` 得 `child_thread_id`;seed `user_message(json(args))` append。
  4. `self._spawn_registry.register(handle_id, skill_id, child_thread_id)`;append `spawn_item(...)` 到 **parent thread**;emit `SpawnStarted`。
  5. `asyncio.create_task(self._drive_spawn(handle_id, target, child_thread_id, args))`;**立即返回** `{handle_id, child_thread_id}`。
- `_drive_spawn`:构造子 `TurnRunner`(同 `_spawn_sub_runner` 的参数:thread_id=child,cancel=`self._root_cancel.child(f"spawn:{handle_id}")`),`await runner.run()`;据 `TurnOutcome.end_reason`:
  - `completed` → `set_result(done, final_text)` + emit `SpawnCompleted` + `await self._check_barriers(handle_id)`。
  - `suspended` → `set_result(status="suspended")` (保留 result=None) + emit `SpawnSuspended{handle_id, thread_id, pending}`。
  - `error`/`cancelled` → `set_result(error/cancelled)` + emit `SpawnFailed`/`SpawnCancelled` + `_check_barriers`。

- [ ] **Step 1: 写失败测试(立即返回 + 独立完成事件 + 同 skill 多实例)**

```python
import asyncio
import pytest
import taifeng
from taifeng.llm.providers import MockClient, RoutingMockClient, MockTurn
from taifeng.loop.submission import Rewind  # noqa: F401  (同文件已用)


async def _wait(cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_spawn_returns_handle_nonblocking(skills_dir, threads_dir):
    # style-checker 子 skill 一句话完成
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="风格结论")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    out = await engine.spawn_skill(skill_id="style-checker", args={}, reason="并发分析")
    assert out["handle_id"] and out["child_thread_id"]
    # 立即返回,专家随后独立完成
    assert await _wait(lambda: engine.spawn_status([out["handle_id"]])[out["handle_id"]]["status"] == "done")
    await pool.close()
```

- [ ] **Step 2: 跑确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_returns_handle_nonblocking -q`
Expected: FAIL — `AttributeError: 'AgentEngine' object has no attribute 'spawn_skill'`

- [ ] **Step 3: 写实现**

在 `engine.__init__` 加 `self._spawn_registry = SpawnHandleRegistry()`(import 见 Task 1)。新增 `spawn_skill` / `_drive_spawn` / `spawn_status`(`spawn_status` 见 Task 7,本任务先实现返回 `{hid: {"status":..., "result":...}}` 的最小版),按上方「实现要点」落地。复用 `_spawn_sub_runner` 的 runner 构造参数;`_root_cancel` 取本 engine 持有的根 cancel(查 `grep -n "root_cancel\|self._root_cancel\|CancellationToken(" src/taifeng/loop/engine.py` 确认字段名)。

> 注:`_drive_spawn` 内构造 child TurnRunner 与 `turn.py::_spawn_sub_runner` 高度重合 —— **抽一个 `engine` 私有 helper `_build_child_runner(target, child_thread_id, cancel)`** 复用,避免与 turn.py 重复(DRY)。

- [ ] **Step 4: 跑确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_returns_handle_nonblocking -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_detached_spawn.py
git commit -m "feat(loop): engine.spawn_skill 分离发起 + _drive_spawn 独立驱动"
```

---

## Task 5: 独立完成事件 + 同 skill 多实例

**Files:**
- Modify: `src/taifeng/loop/engine.py`(确保 `SpawnCompleted` 带 handle_id;无需新逻辑)
- Test: `tests/loop/test_detached_spawn.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_same_skill_multiple_instances(skills_dir, threads_dir):
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="路线1"), MockTurn(text="路线2"), MockTurn(text="路线3")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")
    handles = [(await engine.spawn_skill(skill_id="style-checker", args={"i": i}, reason="路线"))["handle_id"]
               for i in range(3)]
    assert len(set(handles)) == 3  # 三个独立句柄
    assert await _wait(lambda: all(
        engine.spawn_status([h])[h]["status"] == "done" for h in handles))
    # 三条独立 child thread
    threads = {engine._spawn_registry.get(h).child_thread_id for h in handles}  # noqa: SLF001
    assert len(threads) == 3
    await pool.close()
```

- [ ] **Step 2: 跑确认失败/通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_same_skill_multiple_instances -q`
Expected: 若 Task 4 已正确则 PASS;否则据失败修 `_drive_spawn`(确保每次 spawn 独立 thread + 独立 handle)。

- [ ] **Step 3: Commit**

```bash
git add tests/loop/test_detached_spawn.py
git commit -m "test(loop): 同 skill spawn 多实例(3 路线)各自独立"
```

---

## Task 6: 错峰独立 HITL —— detached child 独立 resume

**Files:**
- Modify: `src/taifeng/loop/engine.py`(`_handle_child_resume` 放宽:允许对一个 detached child thread 独立续跑,完成后更新句柄 + emit + `_check_barriers`)
- Test: `tests/loop/test_detached_spawn.py`

实现要点:
- spawn 的专家在 child thread 上 HITL → `_drive_spawn` 收到 `end_reason="suspended"` → 句柄 status=suspended + emit `SpawnSuspended`(任务结束、不阻塞)。
- 业务 `engine.submit(Resume(thread_id=child_thread, resolutions=...))` → engine dispatch 路由到 child resume。**现有 `_handle_resume` 默认作用在 `self._thread_id`(root)**;需新增分支:`Resume.thread_id != self._thread_id 且命中某 spawn 句柄` → 走 `_resume_spawn(handle, resolutions)`:在该 child thread 上补 gap + 续跑子 runner(复用 `_execute_resumed_tool_on_thread` + `_build_child_runner`),完成后 `set_result(done)` + emit `SpawnCompleted` + `_check_barriers`。
- 「错峰」由测试脚本控制:A 先 HITL、resume A、A done;再触发 B 的 HITL、resume B、B done。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_spawn_staggered_hitl(expert_skills, threads_dir):
    """A HITL→resume→done,之后 B HITL→resume→done(错峰,互不耦合)。

    expert_skills fixture:见 Task 6 Step 0(造会 request_user_input 的专家 skill)。
    """
    # 见下方 fixture;expert skill 第一轮调 request_user_input,resume 后第二轮收尾
    ...
```

- [ ] **Step 0(前置):造 HITL 子 skill fixture**

在 `tests/loop/test_detached_spawn.py` 顶部加 fixture(参照 `examples/form_hitl` / `tests/test_suspend.py` 的 `request_user_input` 用法;先 `grep -rn "request_user_input" tests/ examples/form_hitl` 取真实 SKILL.md + tool_calls 形状):

```python
@pytest.fixture
def expert_skills(tmp_path):
    """两个会 request_user_input 的专家 skill + 一个 entry。返回 skills 目录。"""
    # EXPERT_MD: composite, entry:true(便于独立 spawn), tool_names:[request_user_input]
    # ... 写 expert-a / expert-b / orchestrator 三个 SKILL.md
    ...
    return skills_dir
```

- [ ] **Step 2: 跑确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_staggered_hitl -q`
Expected: FAIL(resume 路由到 child 未实现 → B 永不 done 或 resume 被拒)

- [ ] **Step 3: 写实现**

在 `engine.py` 的 dispatch 循环(`isinstance(sub.op, Resume)` 处)之前/之内加 child-thread 路由:若 `sub.op.thread_id` 命中某 spawn 句柄的 `child_thread_id` → `asyncio.create_task(self._resume_spawn(sub, ...))`;否则维持原 root resume。`_resume_spawn` 复用现有 child resume 机制 + 完成回写句柄/事件/barrier。

- [ ] **Step 4: 跑确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_detached_spawn.py::test_spawn_staggered_hitl -q`
Expected: PASS(A、B 各自 spawn_suspended → resume → spawn_completed,顺序错峰)

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_detached_spawn.py
git commit -m "feat(loop): detached child 独立 resume —— 错峰 HITL"
```

---

## Task 7: spawn_status / kill_spawn / 引用计数保活

**Files:**
- Modify: `src/taifeng/loop/engine.py`(`spawn_status`、`kill_spawn`、`has_live_spawns()`)、`src/taifeng/loop/pool.py`(释放条件接入 `has_live_spawns()`)
- Test: `tests/loop/test_detached_spawn.py`

- [ ] **Step 1: 写失败测试(非阻塞读 + kill 隔离)**

```python
@pytest.mark.asyncio
async def test_join_skill_nonblocking_and_kill_isolates(skills_dir, threads_dir):
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="A done"), MockTurn(text="B done")],
        "code-reviewer": [MockTurn(text="主")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s3", entry_skill_id="code-reviewer")
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    st = engine.spawn_status([a])  # 非阻塞:可能 running 或 done
    assert a in st and st[a]["status"] in ("running", "done", "suspended")
    # kill 未知 handle 显式报错
    with pytest.raises(KeyError):
        await engine.kill_spawn("nope")
    await pool.close()
```

- [ ] **Step 2-4:** 跑失败 → 实现 `spawn_status(handle_ids)->{hid:{status,result}}`(未知 hid 该项 `{"status":"unknown"}` 或抛,二选一并在契约写明,本计划取**未知 hid 抛 `KeyError`**,与 kill 一致)、`kill_spawn(handle)`(取消该 spawn 的 cancel child + 句柄 cancelled + emit `SpawnCancelled`;未知 → `KeyError`)、`has_live_spawns()`(registry 有非终态句柄)。在 `pool.py` 找 engine 释放/驱逐判定处(`grep -n "evict\|release\|close\|idle" src/taifeng/loop/pool.py`),加 `and not engine.has_live_spawns()` 条件。→ 跑通。

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py src/taifeng/loop/pool.py tests/loop/test_detached_spawn.py
git commit -m "feat(loop): spawn_status/kill_spawn + 引用计数保活(有未终结 spawn 不释放)"
```

---

## Task 8: join-barrier 自动聚合

**Files:**
- Modify: `src/taifeng/loop/engine.py`(`set_join_barrier()` + `_check_barriers()` 触发逻辑)
- Test: `tests/loop/test_detached_spawn.py`

实现要点:
- `engine.set_join_barrier(handle_ids, then_skill_id, then_args_template=None)`:校验全部 handle 已知 + `then_skill_id` 可作 entry(`snapshot.get(then_skill_id).entry`),否则抛;生成 `barrier_id`,登记 + append `join_barrier_item` + emit `JoinBarrierRegistered`;**注册即检查**(可能已全 done)。
- `_check_barriers(changed_handle_id)`:遍历含该 handle 的 barrier;若 `registry.all_terminal(barrier.handle_ids)` 且 parent thread 无该 barrier 的 `join_barrier_fired` 标记 → 用 `then_args_template`(缺省 = `{h: result}` 映射)构造 args,`await self.submit(UserMessage(...))` 或内部派发一个以 `then_skill_id` 为 entry 的聚合 turn;append `join_barrier_fired_item` + emit `JoinBarrierFired`。

> 聚合 turn 的发起:最简做法 = 在**当前 engine**(parent thread)上 submit 一个 user_message,内容含各专家结果摘要,entry 仍是 parent;但契约要求「起 `then_skill_id` 的 turn」。落地选择:engine 直接构造一个以 `then_skill_id` 为 entry 的子 runner(同 `_build_child_runner`)在新 thread 跑,完成 emit。**实现前确认** entry 切换语义(engine 绑定 entry_skill);若 engine entry 固定,则聚合走「spawn 一个 then_skill_id」(复用 `_drive_spawn`),其 thread 即 `then_thread_id`。

- [ ] **Step 1: 写失败测试(全 done 触发 + 失败专家不丢)**

```python
@pytest.mark.asyncio
async def test_join_barrier_fires_when_all_done(skills_dir, threads_dir):
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="结论A"), MockTurn(text="结论B")],
        "code-reviewer": [MockTurn(text="会诊:综合A+B")],  # 聚合 skill 用 code-reviewer 占位
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s4", entry_skill_id="code-reviewer")
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    b = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    fired = {"v": None}
    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "join_barrier_fired":
                fired["v"] = dict(ev.msg.data); return
    task = asyncio.create_task(watch())
    await engine.set_join_barrier([a, b], then_skill_id="code-reviewer")
    assert await _wait(lambda: fired["v"] is not None)
    assert "then_thread_id" in fired["v"]
    task.cancel()
    await pool.close()
```

- [ ] **Step 2-4:** 跑失败 → 实现 `set_join_barrier` + `_check_barriers`(在 `_drive_spawn`/`_resume_spawn` 完成处调 `_check_barriers`)→ 跑通。再加 `test_join_barrier_with_failed_expert`(一个专家 error,barrier 仍触发,聚合 args 含该 handle 的 error 终态)。

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_detached_spawn.py
git commit -m "feat(loop): join-barrier 全终态自动起聚合 turn(失败专家不丢)"
```

---

## Task 9: LLM 工具封装(spawn_skill / await_skills / join_skill / kill_skill)

**Files:**
- Create: `src/taifeng/tool/builtins/spawn_skill.py`
- Modify: `src/taifeng/tool/builtins/__init__.py`(注册四工具到内置集)
- Test: `tests/loop/test_detached_spawn.py`

实现要点:工具 handler 经 `ctx.extras` 取 engine 引用(需在 `_build_tool_context` 注入 `"engine": <weakref or self-engine handle>`;当前 ctx.extras 有 `dispatcher`=runner,**engine ≠ runner**——需让 runner 持有 engine 回调或把 spawn API 暴露在 dispatcher 上)。**实现前定**:把 `spawn_skill`/`set_join_barrier`/`spawn_status`/`kill_spawn` 作为 `dispatcher`(TurnRunner)的转发方法,内部回调到 engine(engine 构造 runner 时注入 `spawn_coordinator=self`)。工具经 `ctx.extras["dispatcher"].spawn_skill(...)` 调用。

- [ ] **Step 1: 写失败测试(LLM 一条消息里 spawn 3 个 + await_skills)**

```python
@pytest.mark.asyncio
async def test_llm_spawn_and_barrier_via_tools(skills_dir, threads_dir):
    # orchestrator turn1: 并发 spawn_skill×2 + await_skills;子专家完成→barrier 起会诊
    client = RoutingMockClient(routes={
        "spawn-orch": [MockTurn(text="并发分析", tool_calls=[
            {"id":"s1","name":"spawn_skill","arguments":'{"skill_id":"style-checker","reason":"a","args":{}}'},
            {"id":"s2","name":"spawn_skill","arguments":'{"skill_id":"style-checker","reason":"b","args":{}}'},
        ]), MockTurn(text="已发起")],
        "style-checker": [MockTurn(text="结论A"), MockTurn(text="结论B")],
    })
    # 需 spawn-orch SKILL.md: composite entry, child_skills:[style-checker],
    #   tool_names:[spawn_skill, await_skills, join_skill, kill_skill]
    ...
```

- [ ] **Step 0(前置):** 造 `spawn-orch` SKILL.md fixture(tool_names 含四工具)。

- [ ] **Step 2-4:** 跑失败 → 实现 `spawn_skill.py` 四工具 + dispatcher 转发 + `__init__` 注册 + `_build_tool_context` 注入 → 跑通。工具 schema:`spawn_skill{skill_id,args?,reason}`、`await_skills{handle_ids[],then_skill_id,then_args_template?}`、`join_skill{handle_ids[],mode?}`、`kill_skill{handle_id}`;均 `parallel_safe=True`(只登记/读,真执行在 detached 任务)。

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/tool/builtins/spawn_skill.py src/taifeng/tool/builtins/__init__.py src/taifeng/loop/turn.py tests/loop/test_detached_spawn.py
git commit -m "feat(tool): spawn_skill/await_skills/join_skill/kill_skill 四工具(LLM 入口)"
```

---

## Task 10: 冷恢复(重建 registry + barriers + 幂等触发)

**Files:**
- Modify: `src/taifeng/loop/engine.py`(加载后 `_rebuild_spawn_state_from_history()`;`get_or_create` 流程接入)
- Test: `tests/loop/test_detached_spawn.py`

实现要点:engine 加载 parent thread 后扫 `_history`:`spawn` 项 → `registry.register`(status 由 child thread 终态推定:载入 child thread 末态);`join_barrier` 项 → 登记 barrier;`join_barrier_fired` 项 → 标记已触发。重建后对每个未触发且全终态的 barrier 调 `_check_barriers`(幂等:有 fired 标记不重复)。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_cold_recovery_rebuilds_handles_and_barrier(skills_dir, threads_dir):
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="A"), MockTurn(text="B")],
        "code-reviewer": [MockTurn(text="会诊")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="cold1", entry_skill_id="code-reviewer")
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    b = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    await _wait(lambda: engine.spawn_status([a, b]).get(a, {}).get("status") == "done"
                and engine.spawn_status([b])[b]["status"] == "done")
    # 释放 engine(模拟冷态)
    await pool.evict(session_id="cold1")  # 若无 evict,用 pool 内部释放 API;见 pool.py
    # 重新加载同 session → registry 重建
    engine2 = await pool.get_or_create(session_id="cold1", entry_skill_id="code-reviewer")
    st = engine2.spawn_status([a, b])
    assert st[a]["status"] == "done" and st[b]["status"] == "done"
    await pool.close()
```

- [ ] **Step 2-4:** 跑失败 → 实现 `_rebuild_spawn_state_from_history` + 接入加载流程 → 跑通。再加「重载后挂起专家可 Resume」「barrier 重载幂等不重复触发」两个断言/小测试。

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_detached_spawn.py
git commit -m "feat(loop): spawn 冷恢复 —— 重载重建句柄/屏障 + 幂等触发(R5)"
```

---

## Task 11: 拒绝路径 + 配额(错误边界)

**Files:**
- Modify: `src/taifeng/loop/engine.py`(spawn/await/join/kill 各拒绝路径)
- Test: `tests/loop/test_detached_spawn.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_spawn_rejections(skills_dir, threads_dir):
    client = RoutingMockClient(routes={"code-reviewer":[MockTurn(text="主")],
                                       "style-checker":[MockTurn(text="x")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="rej", entry_skill_id="code-reviewer")
    # 未知 skill
    with pytest.raises(Exception):
        await engine.spawn_skill(skill_id="ghost", args={}, reason="x")
    # 非白名单(style-checker 在白名单内则换一个不在的;此处用 await_skills 的 then 校验)
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    with pytest.raises(Exception):
        await engine.set_join_barrier([a, "unknown-handle"], then_skill_id="code-reviewer")
    with pytest.raises(Exception):
        await engine.set_join_barrier([a], then_skill_id="style-checker")  # 非 entry 不可作聚合
    await pool.close()
```

- [ ] **Step 2-4:** 跑失败 → 在各 API 加显式校验(unknown_skill / not_in_whitelist / depth / cycle / spawn_limit / unknown_handle / then 非 entry),禁 silent fallback → 跑通。K1 配额测试 `test_spawn_quota_rejected`:`EnginePool.create(..., max_concurrent_spawns=1)`(查构造参数名)后并发 spawn 2 个,第 2 个被拒。

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_detached_spawn.py
git commit -m "test+feat(loop): spawn/barrier 拒绝路径 + K1 配额(禁 silent fallback)"
```

---

## Task 12: 规模检查 + 全量回归 + lint

**Files:** 视情况 `src/taifeng/loop/spawn_driver.py`(若 engine.py 超线则抽出)

- [ ] **Step 1: 文件规模检查**

Run: `wc -l src/taifeng/loop/engine.py`
若 > 800:把 `spawn_skill/_drive_spawn/_resume_spawn/_check_barriers/set_join_barrier/spawn_status/kill_spawn/_rebuild_spawn_state_from_history` 抽到 `loop/spawn_driver.py` 的 `SpawnDriver`(engine 持有 `self._spawn = SpawnDriver(self)`),engine 上保留薄转发。同步跑测试确保零回归。

- [ ] **Step 2: 全量测试**

Run: `PYTHONPATH=src uv run pytest tests/ -q`
Expected: 全绿(含既有 698+ 与新增 detached_spawn 用例)。

- [ ] **Step 3: lint/类型**

Run: `uv run ruff check src/taifeng/loop/spawn_handle.py src/taifeng/tool/builtins/spawn_skill.py && uv run mypy src/taifeng/loop/spawn_handle.py`
Expected: 新文件 clean(既有基线噪音不计入)。

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(loop): spawn 逻辑规模检查/抽 SpawnDriver(如需)+ 全量回归"
```

---

## Task 13: 文档义务(契约 + ADR + 活文档 + knobs)

**Files:**
- Create: `docs/architecture/capabilities/detached-spawn.md`(仿 turn-rewind.md:Purpose / 数据契约 / Requirements+Scenario / R1–R5 / v1 边界)
- Create: `docs/decisions/0015-detached-skill-spawn.md`(为何模型 A+barrier 而非 B、child-thread vs sibling-session、barrier 全终态策略、引用计数保活)
- Modify: `docs/architecture/capabilities/README.md`(加 detached-spawn 行)、`docs/architecture/agent-loop.md`(spawn/barrier/引用计数生命周期段 + 工具清单)、`docs/configurable-knobs.md`(四工具/API + barrier + max_concurrent_spawns)

- [ ] **Step 1-2:** 写契约 + ADR(内容同 spec §3/§6/§7/§8 凝练),更新索引/活文档/knobs。
- [ ] **Step 3: Commit**

```bash
git add docs/ && git commit -m "docs(detached-spawn): 能力契约 + ADR 0015 + agent-loop + knobs"
```

---

## Task 14: 体验 example(multi_expert_consult)

**Files:**
- Create: `examples/multi_expert_consult/{demo.py,README.md,skills/...}`
- Modify: `examples/README.md`、`taifeng/CLAUDE.md`(example 索引)

- [ ] **Step 1:** 写 `demo.py`(纯 MockClient/RoutingMockClient):用户说身体情况 → orchestrator 并发 spawn 专家A/专家B(其中一个走错峰 HITL)+ await_skills(联合会诊)→ 各自完成 → barrier 自动起会诊 → 打印事件时间线(`spawn_started/suspended/completed` + `join_barrier_fired` + 最终报告)。`attach_console_sink` 可视化。
- [ ] **Step 2:** 跑 `PYTHONPATH=src uv run python examples/multi_expert_consult/demo.py`,确认 exit=0 + 时间线符合预期;写 README(三种姿态对照表 + 与 step_pipeline/turn_rewind 关系)。
- [ ] **Step 3: Commit**

```bash
git add examples/multi_expert_consult/ examples/README.md taifeng/CLAUDE.md
git commit -m "docs(examples): multi_expert_consult —— 并发多专家+错峰HITL+联合会诊"
```

---

## 收尾验证(DoD)

- [ ] `PYTHONPATH=src uv run pytest tests/ -q` 全绿,复述命令+输出。
- [ ] `examples/multi_expert_consult/demo.py` 端到端 exit=0,贴时间线。
- [ ] 契约/ADR/活文档/knobs 已同步(四象限收尾红线)。
- [ ] 集成:`feat/detached-spawn` 一次合一条 + 全量回归。
