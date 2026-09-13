"""ADR 0041：内核默认套有界重试。

改动前 ``RetryingModelClient`` 只在 examples 的 bootstrap 里手工套；业务侧漏套 = 生产零自动重试
（实测 qiuben api 即如此：中转一次瞬时抖动直接把 turn 打成挂起等人）。本文件钉死：
``AgentEngine`` / ``AgentEnginePool`` / ``EnginePool.create`` 默认包装；幂等（已套过不重复、透明
包装可穿透）；strict audit 适配器原样；``auto_retry=False`` 可关；``retry_config`` 可注入。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.errors import TransientNetworkError
from taifeng.llm.events import completed, created, server_model, text_delta
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.retry import RetryConfig
from taifeng.llm.retrying import RetryingModelClient, with_default_retry
from taifeng.llm.types import TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime
from tests.conftest import GUARD_TIMEOUT_SECONDS, last_turn_terminal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.llm.types import ApiRequest


class _FakeStore:
    async def append(self, item: object) -> None:
        return None

    async def create_thread(self, **_: object) -> str:
        return "t"


class _FlakySession:
    def __init__(self, owner: _FlakyClient) -> None:
        self._owner = owner

    async def __aenter__(self) -> _FlakySession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def stream(self, request: ApiRequest) -> AsyncIterator[Any]:
        self._owner.attempts += 1
        yield created()
        yield server_model("mock-model")
        if self._owner.attempts <= self._owner.fail_times:
            raise TransientNetworkError("relay reset")
        yield text_delta("ok")
        yield completed(
            response_id="r", usage=TokenUsage(input_tokens=1, output_tokens=1), end_turn=True,
        )


class _FlakyClient:
    """前 ``fail_times`` 次 attempt 零产出即抛瞬时网络错；无 __getattr__（裸 client 形态）。"""

    def __init__(self, *, fail_times: int) -> None:
        self.attempts = 0
        self.fail_times = fail_times

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> _FlakySession:
        return _FlakySession(self)


class _Forwarding:
    """只转发 session + __getattr__ 的透明包装（模拟台账录制 RecordingClient）。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def session(self, **kw: Any) -> Any:
        return self._inner.session(**kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _drive(engine: taifeng.AgentEngine) -> list[Any]:
    """驱动裸 engine 跑一个 turn 到任一终态（含 turn_suspended），返回全部事件。

    ``run_until_root_done`` 不启动 ``engine.run()`` 且不认 ``turn_suspended``；裸 engine 的
    submit 只入队，必须自己驱动 actor 主循环。
    """
    events: list[Any] = []
    done = asyncio.Event()

    async def _collect() -> None:
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
                done.set()

    collector = asyncio.create_task(_collect())
    await asyncio.sleep(0)
    runner = asyncio.create_task(engine.run(CancellationToken(name="root")))
    await engine.submit(taifeng.UserMessage(text="hi"))
    try:
        await asyncio.wait_for(done.wait(), timeout=GUARD_TIMEOUT_SECONDS)
    finally:
        await engine.shutdown()
        collector.cancel()
        runner.cancel()
    return events


def _fast(**kw: Any) -> RetryConfig:
    base: dict[str, Any] = {"max_attempts": 3, "min_delay_ms": 1, "max_delay_ms": 2}
    base.update(kw)
    return RetryConfig(**base)


async def _engine(skills_dir: Path, client: Any, **kw: Any) -> taifeng.AgentEngine:
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    assert entry is not None
    return taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=reg.snapshot(),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        model_client=client,
        store=_FakeStore(),
        thread_id="t",
        **kw,
    )


# ───────────────────────── with_default_retry 单元 ─────────────────────────


def test_default_config_covers_unreliable_finish() -> None:
    """默认可重试集合并入 unreliable_finish：两套「可重试」真相合一（ADR 0041）。"""
    kinds = RetryConfig().retryable_kinds
    assert {"rate_limit", "transient_network", "server_error", "unreliable_finish"} <= kinds


def test_with_default_retry_wraps_plain_client() -> None:
    inner = _FlakyClient(fail_times=0)
    wrapped = with_default_retry(inner)
    assert isinstance(wrapped, RetryingModelClient)
    assert wrapped.inner is inner
    assert wrapped.bounded_retry == RetryConfig(), "未注入 config 时用默认策略"


def test_with_default_retry_is_idempotent() -> None:
    """已套过原样返回（identity），attempt 上限不会被乘起来。"""
    once = RetryingModelClient(_FlakyClient(fail_times=0), config=_fast(max_attempts=2))
    assert with_default_retry(once) is once
    assert with_default_retry(once).bounded_retry.max_attempts == 2


def test_with_default_retry_sees_marker_through_forwarding_wrapper() -> None:
    """透明包装（__getattr__ 转发）里的已套标记可穿透 → 不再包一层。"""
    wrapped = _Forwarding(RetryingModelClient(_FlakyClient(fail_times=0), config=_fast()))
    assert with_default_retry(wrapped) is wrapped


def test_with_default_retry_leaves_audit_adapter_alone() -> None:
    """strict audit 适配器契约是一次 stream 恰一个 attempt，套重试会破坏 lineage → 原样。"""
    adapter = AttemptObservableClientAdapter(
        SimClient(turns=[SimTurn(text="x")]), provider="sim", default_model="sim-model",
    )
    assert with_default_retry(adapter) is adapter


def test_with_default_retry_can_be_disabled() -> None:
    inner = _FlakyClient(fail_times=0)
    assert with_default_retry(inner, enabled=False) is inner


# ───────────────────────── AgentEngine 默认行为 ─────────────────────────


@pytest.mark.asyncio
async def test_engine_wraps_by_default_and_recovers_transient_failures(skills_dir: Path) -> None:
    """裸 client 两次瞬时错 → 引擎默认重试后 turn 正常完成，且每次重试可观测。"""
    inner = _FlakyClient(fail_times=2)
    engine = await _engine(skills_dir, inner, retry_config=_fast())
    assert isinstance(engine._model_client, RetryingModelClient)  # noqa: SLF001

    events = await _drive(engine)

    assert last_turn_terminal(events) == "turn_completed"
    assert inner.attempts == 3
    assert [e.msg.data["attempt"] for e in events if e.msg.kind == "provider_retry"] == [1, 2]


@pytest.mark.asyncio
async def test_engine_default_config_is_production_default(skills_dir: Path) -> None:
    """不传 retry_config → RetryConfig() 生产默认（3 次 / 500ms 起 / 尊重 hint），非测试快配置。"""
    engine = await _engine(skills_dir, _FlakyClient(fail_times=0))
    assert engine._model_client.bounded_retry == RetryConfig()  # noqa: SLF001


@pytest.mark.asyncio
async def test_engine_does_not_double_wrap(skills_dir: Path) -> None:
    """接入方已自己套 RetryingModelClient(max_attempts=2) → 引擎沿用，总 attempt 恰 2 而非 4。"""
    inner = _FlakyClient(fail_times=99)
    own = RetryingModelClient(inner, config=_fast(max_attempts=2))
    engine = await _engine(skills_dir, own)
    assert engine._model_client is own  # noqa: SLF001

    events = await _drive(engine)

    assert last_turn_terminal(events) == "turn_suspended"
    assert inner.attempts == 2


@pytest.mark.asyncio
async def test_engine_opt_out_keeps_raw_client(skills_dir: Path) -> None:
    """auto_retry=False → 裸 client 原样，一次瞬时错即按失败策略挂起（复现「重试已耗尽」）。"""
    inner = _FlakyClient(fail_times=99)
    engine = await _engine(skills_dir, inner, auto_retry=False)
    assert engine._model_client is inner  # noqa: SLF001

    events = await _drive(engine)

    assert last_turn_terminal(events) == "turn_suspended"
    assert inner.attempts == 1


# ───────────────────────── EnginePool 透传 ─────────────────────────


@pytest.mark.asyncio
async def test_pool_forwards_retry_config_and_opt_out(skills_dir: Path, tmp_path: Path) -> None:
    """池级 retry_config 透传到引擎；auto_retry=False 时池与引擎都不包装。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=str(skills_dir),
        storage_dir=str(tmp_path / "threads"),
        model_client=SimClient(turns=[SimTurn(text="ok")]),
        retry_config=_fast(max_attempts=5),
    )
    try:
        engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
        client = engine._model_client  # noqa: SLF001
        assert client.bounded_retry.max_attempts == 5
    finally:
        await pool.close()

    raw = SimClient(turns=[SimTurn(text="ok")])
    pool_off = await taifeng.EnginePool.create(
        skills_dir=str(skills_dir),
        storage_dir=str(tmp_path / "threads2"),
        model_client=raw,
        auto_retry=False,
    )
    try:
        engine = await pool_off.get_or_create(
            session_id="s2", entry_skill_id="code-reviewer",
        )
        assert engine._model_client is raw  # noqa: SLF001
    finally:
        await pool_off.close()
