"""B1 midturn-input-steering：运行中 turn 不打断地接收增量用户输入。

参照 codex session/inject.rs + input_queue.rs：注入投进活跃 turn 的 pending 队列，
在迭代边界并入 prompt；无活跃 turn 则落历史不起新 turn。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import user_message
from taifeng.llm.client import ModelClient
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    server_model,
    text_delta,
)
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.engine import _PendingTurn
from taifeng.loop.submission import InjectUserInput
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _FakeStore:
    """最小内存 store。"""

    def __init__(self) -> None:
        self.items: list[object] = []

    async def append(self, item: object) -> None:
        self.items.append(item)

    async def create_thread(self, **_: object) -> str:
        return "sub-thread"


async def _make_runner(
    skills_dir: Path,
    *,
    events: list | None = None,
    model_client: ModelClient | None = None,
    **extra: object,
) -> TurnRunner:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: object) -> None:
        if events is not None:
            events.append(ev.msg)  # type: ignore[attr-defined]

    return TurnRunner(
        entry_skill=entry,
        snapshot=registry.snapshot(),
        model_client=model_client or SimClient(turns=[SimTurn(text="ok")]),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(),
        compressors=None,
        dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        **extra,
    )


@pytest.mark.asyncio
async def test_drain_appends_user_message(skills_dir: Path) -> None:
    """drain 把 pending 转入 history + store，清空队列，emit user_input_injected。"""
    events: list = []
    pending = [user_message("换个方向", thread_id="t")]
    runner = await _make_runner(skills_dir, events=events, pending_input=pending)

    assert len(runner.history_buffer) == 0
    await runner._drain_pending_input()  # noqa: SLF001

    assert len(runner.history_buffer) == 1
    assert runner.history_buffer[-1].kind == "user_message"
    assert len(runner.store.items) == 1  # type: ignore[attr-defined]
    assert pending == []  # 队列清空
    kinds = [m.kind for m in events]
    assert "user_input_injected" in kinds


async def _collect(engine: object) -> tuple[asyncio.Task, list]:
    """起一个 subscribe_all consume task，收集 msg 直到 shutdown。"""
    events: list = []

    async def consume() -> None:
        async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # 让 consume 注册订阅
    return task, events


async def _wait_for(events: list, kind: str) -> None:
    for _ in range(100):
        if any(m.kind == kind for m in events):
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_inject_routed_to_active_turn(
    skills_dir: Path, threads_dir: Path
) -> None:
    """有活跃 turn → 注入投进其 pending 队列、emit delivered=true、不落历史。"""
    client = SimClient(turns=[SimTurn(text="ok")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer"
    )
    task, events = await _collect(engine)
    # 白盒：模拟一个活跃 turn 注册
    fake = _PendingTurn(
        submission_id="active", cancel=CancellationToken(name="active")
    )
    engine._pending["active"] = fake  # noqa: SLF001
    await engine.submit(InjectUserInput(submission_id="active", text="补充看肝功能"))
    await _wait_for(events, "user_input_injected")

    await pool.close()
    await asyncio.wait_for(task, timeout=2.0)

    injected = [m for m in events if m.kind == "user_input_injected"]
    assert injected and injected[0].data["delivered"] is True
    # 投进活跃 turn 共享队列（未落历史，等 runner drain 时才并入）
    assert len(fake.pending_input) == 1
    assert fake.pending_input[0].kind == "user_message"


@pytest.mark.asyncio
async def test_inject_no_active_turn_no_new_turn(
    skills_dir: Path, threads_dir: Path
) -> None:
    """无活跃 turn → 落历史不起新 turn、emit delivered=false。"""
    client = SimClient(turns=[SimTurn(text="ok")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer"
    )
    task, events = await _collect(engine)
    before_turns = engine._turn_index  # noqa: SLF001
    await engine.submit(InjectUserInput(submission_id="ghost", text="无人接收"))
    await _wait_for(events, "user_input_injected")

    await pool.close()
    await asyncio.wait_for(task, timeout=2.0)

    injected = [m for m in events if m.kind == "user_input_injected"]
    assert injected and injected[0].data["delivered"] is False
    # 未起新 turn
    assert engine._turn_index == before_turns  # noqa: SLF001
    assert not any(m.kind == "turn_started" for m in events)


@pytest.mark.asyncio
async def test_inject_consumed_by_running_turn_e2e(
    skills_dir: Path, threads_dir: Path
) -> None:
    """真端到端：运行中 turn 在后续迭代 drain 消费注入，文本进入 history。

    多轮 SimClient（前两轮各带 tool call 维持 turn）确保注入有迭代边界可消费。
    """
    # delay_seconds 模拟真实 LLM 每轮采样的秒级延迟 —— 确保注入往返落在 turn
    # 运行窗口内（mock 无延迟时 turn 会在 polling 间隙跑完，inject 错过窗口）。
    client = SimClient(
        turns=[
            SimTurn(
                text="第1轮",
                delay_seconds=0.05,
                tool_calls=[
                    {"id": "c1", "name": "read_skill",
                     "arguments": '{"skill_id": "style-checker"}'}
                ],
            ),
            SimTurn(
                text="第2轮",
                delay_seconds=0.05,
                tool_calls=[
                    {"id": "c2", "name": "read_skill",
                     "arguments": '{"skill_id": "style-checker"}'}
                ],
            ),
            SimTurn(text="最终结论", delay_seconds=0.05),
        ]
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer"
    )
    task, events = await _collect(engine)
    sub_id = await engine.submit(taifeng.UserMessage(text="开始分析"))
    # 等 turn 真正进入（第一个 tool_call → _pending 已注册、turn 在多轮中）
    await _wait_for(events, "tool_call_started")
    # 运行中注入
    await engine.submit(
        InjectUserInput(submission_id=sub_id, text="STEER看肝功能")
    )
    await _wait_for(events, "turn_completed")

    await pool.close()
    await asyncio.wait_for(task, timeout=2.0)

    # 注入被活跃 turn 接收
    injected = [m for m in events if m.kind == "user_input_injected"]
    assert injected and injected[0].data["delivered"] is True
    # 注入文本真的并入了 history（runner drain 落盘）
    hist = engine.history_snapshot()
    assert any("STEER看肝功能" in str(it.payload) for it in hist), (
        "注入文本未进入 history —— drain 未消费"
    )


class _LateInjectSession:
    """采样会话：stream 期间往目标 runner.pending_input append 一条注入，模拟
    「注入恰在最后一轮采样期间到达」；随后无 tool call 正常结束 turn。"""

    def __init__(
        self, client: _LateInjectClient, cancel: CancellationToken
    ) -> None:
        self._client = client
        self._cancel = cancel

    async def __aenter__(self) -> _LateInjectSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        if not self._client.fired and self._client.runner is not None:
            self._client.fired = True
            self._client.runner.pending_input.append(self._client.inject)
        yield created()
        yield server_model("mock-model")
        yield text_delta("done")
        yield completed(
            response_id="r",
            usage=TokenUsage(input_tokens=5, output_tokens=2),
            end_turn=True,
        )


class _LateInjectClient(ModelClient):
    """单轮 client；在采样期间触发一次「晚到注入」。"""

    def __init__(self) -> None:
        self.runner: TurnRunner | None = None
        self.inject = user_message("最后一刻补充", thread_id="t")
        self.fired = False

    def session(
        self, *, cancel: CancellationToken, model: str | None = None
    ) -> _LateInjectSession:
        return _LateInjectSession(self, cancel)


@pytest.mark.asyncio
async def test_late_inject_not_lost_at_turn_end(skills_dir: Path) -> None:
    """注入恰在最后一轮采样期间到达（无后续迭代 drain）→ turn 收尾 drain 落历史不丢（R5）。"""
    client = _LateInjectClient()
    runner = await _make_runner(skills_dir, model_client=client)
    client.runner = runner
    outcome = await runner.run()

    assert outcome.success
    assert outcome.end_reason == "completed"
    # 收尾 drain 把晚到注入并入 history + 持久化（R5 不丢）
    assert any(
        "最后一刻补充" in str(it.payload) for it in runner.history_buffer
    ), "晚到注入未进 history —— turn 收尾 drain 缺失"
    assert any(
        "最后一刻补充" in str(getattr(it, "payload", {}))
        for it in runner.store.items  # type: ignore[attr-defined]
    )
