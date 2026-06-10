"""failure-suspension-policy 链路端到端 —— spawn 链 + call_skill 子链。

覆盖 spec「spawn 链路零新增复用挂起路由」与「子 runner 继承 policy」:
- 被 spawn 的专科触 max_iterations → 句柄 suspended + SpawnSuspended(thread_id)
  → Resume(child_tid, retry) 续跑至 done;abort → error + SpawnFailed。
- call_skill 子链:SuspendByDefault 下子 skill 确定性 LLM 错误(content_filter)
  → 子 turn SYSTEM_RETRY 挂起 → 父 CHILD_SKILL 上浮 → 根 turn_suspended;
  Resume(leaf, retry) 经嵌套续跑链回填父 output → 根完成。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import taifeng
from taifeng import SuspendByDefaultPolicy
from taifeng.llm.errors import ContentFilterError
from taifeng.llm.providers import MockTurn
from taifeng.llm.providers.mock import RoutingMockClient
from taifeng.loop.submission import Resume
from taifeng.suspend.reason import SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.spec import ToolResult, ToolSpec

_EXPERT = """---
name: budget-expert
description: 预算受限专家
version: 1.0.0
type: composite
model: mock-model
tool_names: [echo]
max_call_depth: 2
---
# 专家 BUDGET_EXPERT_MARK
反复调用 echo。
"""

_HOST = """---
name: host
description: 宿主入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [budget-expert]
max_call_depth: 3
---
# 宿主 HOST_MARK
派发专家。
"""


def _echo_tool() -> ToolSpec:
    """极简 echo 工具(parallel_safe)。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("ok")

    return ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, parallel_safe=True,
    )


def _echo_turn(i: int) -> MockTurn:
    return MockTurn(text=f"第{i}轮。", tool_calls=[
        {"id": f"x{i}", "name": "echo", "arguments": "{}"}])


@pytest.fixture
def chain_skills(tmp_path):
    """host(entry) + budget-expert(被 spawn / call_skill 的目标)。"""
    skills = tmp_path / "chain_skills"
    for sub, body in (("host", _HOST), ("budget-expert", _EXPERT)):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


async def _wait(cond, tries: int = 200) -> bool:
    """轮询等待条件成立(spawn 子 task 调度无确定时序)。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def _spawn_until_suspended(pool, engine):
    """spawn budget-expert 至触顶挂起,返回 (handle_id, child_tid, record)。"""
    h = await engine.spawn_skill(skill_id="budget-expert", args={}, reason="t")
    hid, ctid = h["handle_id"], h["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"
    ), "spawn 专科应在 max_iterations 触顶后挂起"
    items = [it async for it in await pool.store.load_thread(ctid)]
    recs = [it for it in items if it.kind == "suspension"]
    assert len(recs) == 1
    rec = SuspensionRecord.from_item(recs[0])
    assert rec.pending[0].reason is SuspendReason.RESOURCE_LIMIT
    assert rec.pending[0].detail["end_reason"] == "max_iterations"
    return hid, ctid, rec


@pytest.mark.asyncio
async def test_spawn_resource_limit_resume_retry_to_done(chain_skills, threads_dir):
    """spawn 专科触顶挂起 → Resume(child_tid, retry) 续跑至 done,
    已走历史保留(续跑轮文本接续),句柄经既有 suspended 路由,零新增状态机。"""
    client = RoutingMockClient(routes={
        # 2 轮 echo 耗尽 cap=2 → 挂起;第 3 个脚本留给 resume 续跑(纯文本完成)
        "BUDGET_EXPERT_MARK": [_echo_turn(1), _echo_turn(2),
                               MockTurn(text="EXPERT_DONE")],
        "HOST_MARK": [MockTurn(text="host idle")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=chain_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[_echo_tool()], max_iterations=2,
        failure_policy=SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="sp-retry", entry_skill_id="host")

    hid, ctid, rec = await _spawn_until_suspended(pool, engine)

    # Resume retry → 重建 runner 续跑(预算按原 cap 重置)→ 终态 done
    await engine.submit(Resume(
        thread_id=ctid,
        resolutions={rec.pending[0].request_id: {"action": "retry"}},
    ))
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    ), "Resume retry 后 spawn 应续跑至 done"
    assert engine.spawn_status([hid])[hid]["result"] == "EXPERT_DONE"
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_resource_limit_resume_abort_to_failed(chain_skills, threads_dir):
    """spawn 专科触顶挂起 → Resume(child_tid, abort) → 句柄 error + SpawnFailed
    (人显式放弃成终态,barrier 全终态条件得以推进)。"""
    client = RoutingMockClient(routes={
        "BUDGET_EXPERT_MARK": [_echo_turn(1), _echo_turn(2),
                               MockTurn(text="不应被采样")],
        "HOST_MARK": [MockTurn(text="host idle")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=chain_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[_echo_tool()], max_iterations=2,
        failure_policy=SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="sp-abort", entry_skill_id="host")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    hid, ctid, rec = await _spawn_until_suspended(pool, engine)
    await engine.submit(Resume(
        thread_id=ctid,
        resolutions={rec.pending[0].request_id: {"action": "abort"}},
    ))
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "error"
    ), "abort 后句柄应落 error 终态"
    failed = [m for m in events if m.kind == "spawn_failed"
              and m.data.get("handle_id") == hid]
    assert failed, "abort 应 emit SpawnFailed(终态可被 barrier 消费)"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


class _FlakyOnceClient(RoutingMockClient):
    """指定 marker 的第 1 次采样抛确定性 ContentFilterError,之后走正常脚本。

    模拟「子 skill 确定性 LLM 失败一次,人工裁决 retry 后恢复」——
    SuspendByDefault 下该失败应转挂起而非终态(这正是与保守 policy 的差异点)。
    """

    def __init__(self, *, routes: dict[str, list[MockTurn]], flaky_marker: str) -> None:
        super().__init__(routes=routes)
        self._flaky_marker = flaky_marker
        self._raised = False

    def session(self, *, cancel: Any, model: str | None = None):  # noqa: ANN201
        outer = self
        inner = super().session(cancel=cancel, model=model)

        class _S:
            async def __aenter__(self):  # noqa: ANN204
                return self

            async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
                pass

            async def stream(self, request):  # noqa: ANN001, ANN201
                parts = list(request.system_prompt)
                parts.extend(
                    str(getattr(m, "content", "")) for m in request.messages
                )
                blob = " ".join(parts)
                if outer._flaky_marker in blob and not outer._raised:
                    outer._raised = True
                    raise ContentFilterError("blocked once")
                async with inner as s:
                    async for ev in s.stream(request):
                        yield ev

        return _S()


@pytest.mark.asyncio
async def test_call_skill_chain_inherits_policy_and_nested_resume(
    chain_skills, threads_dir
):
    """call_skill 子链继承 policy:子 skill 确定性失败(content_filter)在
    SuspendByDefault 下转 SYSTEM_RETRY 挂起 → 父 CHILD_SKILL 上浮 → 根
    turn_suspended;Resume(leaf, retry) 经嵌套续跑链回填父 → 根完成。"""
    client = _FlakyOnceClient(
        routes={
            # host:第 1 轮派 call_skill;续跑轮收到子结果后给最终文本
            "HOST_MARK": [
                MockTurn(text="派发专家", tool_calls=[
                    {"id": "ck1", "name": "call_skill",
                     "arguments": '{"skill_id": "budget-expert", "args": {}}'}]),
                MockTurn(text="HOST_DONE"),
            ],
            # 专家:第 1 次采样被 flaky 拦截(抛 content_filter);retry 后直接出结论
            "BUDGET_EXPERT_MARK": [MockTurn(text="EXPERT_OK")],
        },
        flaky_marker="BUDGET_EXPERT_MARK",
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=chain_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], failure_policy=SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="ck-chain", entry_skill_id="host")

    # 子 thread 先 emit 自己的 turn_suspended(system_retry),根的 CHILD_SKILL 挂起
    # 随后;subscribe(sub_id) 在首条 turn_suspended 即终止 → 用 subscribe_all 看护。
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    watch_task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    def _root_suspended():
        for m in events:
            if (m.kind == "turn_suspended"
                    and m.data["thread_id"] == engine.thread_id):
                return m.data
        return None

    await engine.submit(taifeng.UserMessage(text="开始"))
    assert await _wait(lambda: _root_suspended() is not None), \
        "根 turn 应以 CHILD_SKILL 挂起(而非 TurnFailed)"
    assert not any(m.kind == "turn_failed" for m in events), \
        "SuspendByDefault 下确定性子失败应挂起而非 TurnFailed"
    suspended = _root_suspended()
    assert suspended is not None

    # 根挂起的 pending 是 CHILD_SKILL(内核态),真实可答挂起在 leaf 子 thread
    root_pending = suspended["pending"][0]
    assert root_pending["reason"] == SuspendReason.CHILD_SKILL.value
    leaf_tid = root_pending["detail"]["sub_thread_id"]

    # leaf 的真实挂起:SYSTEM_RETRY(llm_error 路径),detail 带 content_filter 归因
    items = [it async for it in await pool.store.load_thread(leaf_tid)]
    recs = [it for it in items if it.kind == "suspension"]
    leaf_rec = SuspensionRecord.from_item(recs[-1])
    assert leaf_rec.pending[0].reason is SuspendReason.SYSTEM_RETRY
    assert leaf_rec.pending[0].detail["failure_class"] == "content_filter"

    # Resume(leaf, retry) → 嵌套续跑链:leaf 重采样成功 → 回填父 → 根完成
    await engine.submit(Resume(
        thread_id=leaf_tid,
        resolutions={leaf_rec.pending[0].request_id: {"action": "retry"}},
    ))

    def _root_completed():
        for m in events:
            if m.kind == "turn_completed" and m.data.get("is_root"):
                return m.data
        return None

    assert await _wait(lambda: _root_completed() is not None), \
        "嵌套续跑链应推进至根 turn 完成"
    assert _root_completed()["end_reason"] == "completed"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(watch_task, timeout=5.0)
    await pool.close()


class _CancelRaisingClient(RoutingMockClient):
    """首次采样抛 llm.errors.CancelledError —— 模拟按 ModelClient 协议字面实现的
    provider 在检测到取消时上抛(suspension-ttl-hardening 边界)。"""

    def __init__(self, *, routes: dict[str, list[MockTurn]]) -> None:
        super().__init__(routes=routes)
        self._raised = False

    def session(self, *, cancel: Any, model: str | None = None):  # noqa: ANN201
        outer = self
        inner = super().session(cancel=cancel, model=model)

        class _S:
            async def __aenter__(self):  # noqa: ANN204
                return self

            async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
                pass

            async def stream(self, request):  # noqa: ANN001, ANN201
                if not outer._raised:
                    outer._raised = True
                    from taifeng.llm.errors import CancelledError
                    raise CancelledError("user cancelled")
                async with inner as s:
                    async for ev in s.stream(request):
                        yield ev

        return _S()


@pytest.mark.asyncio
async def test_cancelled_error_bypasses_policy(chain_skills, threads_dir) -> None:
    """取消非失败:llm CancelledError 不进 failure policy —— 即便注入
    SuspendByDefaultPolicy,用户取消也不得被转成 SYSTEM_RETRY 挂起。"""
    import taifeng

    client = _CancelRaisingClient(routes={
        "HOST_MARK": [MockTurn(text="host done")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=chain_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], failure_policy=taifeng.SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="cxl", entry_skill_id="host")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind in ("turn_failed", "turn_suspended", "shutdown"):
                if ev.msg.kind != "shutdown":
                    return
                return

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="go"))
    await asyncio.wait_for(task, timeout=8.0)

    kinds = [m.kind for m in events]
    assert "turn_suspended" not in kinds, \
        f"用户取消被误转挂起(policy 不应裁决 cancelled): {kinds}"
    assert "turn_failed" in kinds

    await engine.submit(taifeng.loop.Shutdown())
    await pool.close()
