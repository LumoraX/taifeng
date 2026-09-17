"""ADR 0042：provider 断路器三态上事件总线 + 与保守失败策略配合（R3）。

断路器的价值在**跨 turn**：状态挂在 client 装饰器实例上，因此本文件的用例都用
同一个 client 连跑多个 TurnRunner——第 N 个 turn 才是第 N 次「最终失败」。
改动前 `llm/breaker.py` 不存在，上游挂掉时每个 turn 各自烧满重试预算再挂起。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import user_message
from taifeng.llm.breaker import BreakerConfig, CircuitBreakingModelClient
from taifeng.llm.errors import ServerError
from taifeng.llm.events import completed, created, server_model, text_delta
from taifeng.llm.retry import RetryConfig
from taifeng.llm.types import TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.llm.types import ApiRequest
    from taifeng.loop.event import EventMsg

# 断路器用例一律关掉内层重试：要验的是「最终失败计数」，不是退避时序
_NO_RETRY = RetryConfig(max_attempts=1)


class _FakeStore:
    async def append(self, item: object) -> None:
        return None

    async def create_thread(self, **_: object) -> str:
        return "t"


class _Clock:
    """可推进的假时钟——冷却窗口不能靠真 sleep 验证。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _UpstreamSession:
    """按 owner 的开关决定这次采样是 502 还是正常完成。"""

    def __init__(self, owner: _UpstreamClient) -> None:
        self._owner = owner

    async def __aenter__(self) -> _UpstreamSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def stream(self, request: ApiRequest) -> AsyncIterator[Any]:
        self._owner.attempts += 1
        yield created()
        yield server_model("mock-model")
        if self._owner.healthy:
            yield text_delta("ok")
            yield completed(
                response_id="r",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                end_turn=True,
            )
            return
        raise ServerError("upstream 502")


class _UpstreamClient:
    """模拟中转：``healthy`` 一翻即恢复；``attempts`` 记真实触网次数。"""

    def __init__(self, *, healthy: bool = False) -> None:
        self.attempts = 0
        self.healthy = healthy

    def session(
        self, *, cancel: CancellationToken, model: str | None = None,
    ) -> _UpstreamSession:
        return _UpstreamSession(self)


async def _make_runner(
    skills_dir: Path, *, model_client: Any, events: list[Any],
) -> TurnRunner:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: EventMsg) -> None:
        events.append(ev.msg)

    return TurnRunner(
        entry_skill=entry,
        snapshot=registry.snapshot(),
        model_client=model_client,
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(),
        compressors=None,
        dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=[user_message("hi", thread_id="t")],
    )


@pytest.mark.asyncio
async def test_circuit_opens_across_turns_and_stops_touching_network(
    skills_dir: Path,
) -> None:
    """连续两个 turn 最终失败 → 跳闸；第三个 turn 不再触网，直接挂起。

    这正是缺口的形状：改动前第三个 turn 会照常再烧一遍重试预算。
    """
    events: list[Any] = []
    upstream = _UpstreamClient()
    breaker = CircuitBreakingModelClient(
        upstream,
        config=BreakerConfig(trip_after=2, cooldown_seconds=30.0),
        retry_config=_NO_RETRY,
        clock=_Clock(),
    )

    for _ in range(2):
        outcome = await (await _make_runner(skills_dir, model_client=breaker, events=events)).run()
        assert outcome.end_reason == "suspended"
    assert upstream.attempts == 2

    opened = [m for m in events if m.kind == "provider_circuit_opened"]
    assert len(opened) == 1, "恰在第 2 次最终失败时跳闸一次"
    assert opened[0].data["from_state"] == "closed"
    assert opened[0].data["to_state"] == "open"
    assert opened[0].data["consecutive_failures"] == 2
    assert opened[0].data["cooldown_seconds"] == pytest.approx(30.0)
    assert opened[0].data["last_failure_class"] == "provider_internal"
    assert opened[0].data["last_error_kind"] == "ServerError"
    assert opened[0].data["iteration"] == 1

    # 第 3 个 turn：断路器已 open → 快速失败，一次网络请求都不发
    before = upstream.attempts
    events.clear()
    outcome = await (await _make_runner(skills_dir, model_client=breaker, events=events)).run()

    assert outcome.end_reason == "suspended"
    assert upstream.attempts == before, "open 态必须零触网"
    assert not any(m.kind == "provider_circuit_opened" for m in events), "已 open，不重复跳闸"


@pytest.mark.asyncio
async def test_half_open_probe_recovers_and_emits_closed(skills_dir: Path) -> None:
    """冷却到期 → half_open 探测；上游恢复则闭合，两条事件都上总线。"""
    events: list[Any] = []
    upstream = _UpstreamClient()
    clock = _Clock()
    breaker = CircuitBreakingModelClient(
        upstream,
        config=BreakerConfig(trip_after=2, cooldown_seconds=10.0),
        retry_config=_NO_RETRY,
        clock=clock,
    )
    for _ in range(2):
        await (await _make_runner(skills_dir, model_client=breaker, events=events)).run()

    # 上游恢复 + 冷却到期 → 下一个 turn 是探测
    upstream.healthy = True
    clock.advance(10.0)
    events.clear()
    outcome = await (await _make_runner(skills_dir, model_client=breaker, events=events)).run()

    assert outcome.success and outcome.end_reason == "completed"
    kinds = [m.kind for m in events]
    assert "provider_circuit_half_open" in kinds
    assert "provider_circuit_closed" in kinds
    assert kinds.index("provider_circuit_half_open") < kinds.index("provider_circuit_closed")
    closed = next(m for m in events if m.kind == "provider_circuit_closed")
    assert closed.data["consecutive_failures"] == 0, "闭合即清零"


@pytest.mark.asyncio
async def test_plain_client_emits_no_circuit_events(skills_dir: Path) -> None:
    """没套断路器的裸 client：无 set_circuit_observer → 静默跳过，不报错也不虚报事件。"""
    events: list[Any] = []
    runner = await _make_runner(
        skills_dir, model_client=_UpstreamClient(healthy=True), events=events,
    )
    outcome = await runner.run()
    assert outcome.success
    assert not any(m.kind.startswith("provider_circuit") for m in events)


@pytest.mark.asyncio
async def test_circuit_open_suspends_with_circuit_open_detail(skills_dir: Path) -> None:
    """open 态的快速失败落 SUSPEND，挂起 detail 的 kind 为 CircuitOpenError（spec 场景）。

    业务侧据此能区分「上游整体降级」与「本次调用失败」——泛化成「模型调用异常」就没法
    对用户说「N 秒后自动恢复」。
    """
    import taifeng
    from taifeng.suspend.reason import SuspendReason
    from taifeng.suspend.record import SuspensionRecord

    upstream = _UpstreamClient()
    breaker = CircuitBreakingModelClient(
        upstream,
        config=BreakerConfig(trip_after=1, cooldown_seconds=30.0),
        retry_config=_NO_RETRY,
        clock=_Clock(),
    )
    events: list[Any] = []
    # 第 1 个 turn 打到 open（它自己以 ServerError 挂起）
    await (await _make_runner(skills_dir, model_client=breaker, events=events)).run()

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=skills_dir.parent / "threads",
        model_client=breaker,
        auto_retry=False,  # 断路器已自带有界重试层，外层不再套（用例要的是「最终结局」语义）
        compressors=[],
    )
    engine = await pool.get_or_create(session_id="cb", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
            break

    assert ev.msg.kind == "turn_suspended"
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    rec = SuspensionRecord.from_item([it for it in items if it.kind == "suspension"][0])
    pending = rec.pending[0]
    assert pending.reason is SuspendReason.SYSTEM_RETRY
    assert pending.detail["kind"] == "CircuitOpenError"
    # failure_class 继承触发跳闸的病根，便于看板归因
    assert pending.detail["failure_class"] == "provider_internal"
