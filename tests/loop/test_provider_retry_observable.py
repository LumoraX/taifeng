"""ADR 0039：网络退避重试上 ``provider_retry`` 事件（R3）。

改动前 ``RetryingModelClient`` 只写一条 ``logger.info``，事件总线上看不到任何一次网络重试：
一个 turn 烧掉 3 次 attempt 然后挂起，运维只看到 ``turn_started → turn_suspended``。
本文件钉死：TurnRunner 自动接入重试型 session 的观察者，每次退避重试各 emit 一条
``provider_retry``，``reason`` 为错误 kind，并与 overflow 自愈那条 ``reason=context_overflow``
形状区分。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import user_message
from taifeng.llm.errors import TransientNetworkError
from taifeng.llm.events import completed, created, server_model, text_delta
from taifeng.llm.retry import RetryConfig
from taifeng.llm.retrying import RetryingModelClient
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


class _FakeStore:
    async def append(self, item: object) -> None:
        return None

    async def create_thread(self, **_: object) -> str:
        return "t"


class _FlakySession:
    """前 ``fail_times`` 次 attempt 零产出即抛瞬时网络错，之后正常完成。"""

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
            raise TransientNetworkError("relay reset", transport_phase="connect")
        yield text_delta("ok")
        yield completed(
            response_id="r",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            end_turn=True,
        )


class _FlakyClient:
    def __init__(self, *, fail_times: int) -> None:
        self.attempts = 0
        self.fail_times = fail_times

    def session(
        self, *, cancel: CancellationToken, model: str | None = None,
    ) -> _FlakySession:
        return _FlakySession(self)


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


def _fast() -> RetryConfig:
    return RetryConfig(max_attempts=3, min_delay_ms=1, max_delay_ms=2)


@pytest.mark.asyncio
async def test_each_network_retry_emits_provider_retry(skills_dir: Path) -> None:
    """两次退避重试 → 两条 provider_retry（序号 1、2，reason=transient_network），turn 正常完成。"""
    events: list[Any] = []
    inner = _FlakyClient(fail_times=2)
    client = RetryingModelClient(inner, config=_fast())
    runner = await _make_runner(skills_dir, model_client=client, events=events)

    outcome = await runner.run()

    assert outcome.success and outcome.end_reason == "completed"
    assert inner.attempts == 3
    retries = [m for m in events if m.kind == "provider_retry"]
    assert [m.data["attempt"] for m in retries] == [1, 2]
    for m in retries:
        assert m.data["reason"] == "transient_network"
        assert m.data["failure_class"] == "provider_transport"
        assert m.data["error_kind"] == "TransientNetworkError"
        assert m.data["transport_phase"] == "connect"
        assert m.data["max_attempts"] == 3
        assert m.data["iteration"] == 1, "采样圈序号 1-based（与 FailureContext.iteration 同口径）"
        assert m.data["retry_after_seconds"] is None
        assert m.data["delay_seconds"] >= 0.0
    # 事件顺序：重试事件在 turn 终态之前（退避前 emit，不是事后补记）
    kinds = [m.kind for m in events]
    assert kinds.index("provider_retry") < kinds.index("turn_completed")


@pytest.mark.asyncio
async def test_no_retry_no_provider_retry_event(skills_dir: Path) -> None:
    """首发即成功 → 零条 provider_retry（无重试就无事件，不虚报）。"""
    events: list[Any] = []
    client = RetryingModelClient(_FlakyClient(fail_times=0), config=_fast())
    runner = await _make_runner(skills_dir, model_client=client, events=events)
    outcome = await runner.run()
    assert outcome.success
    assert not any(m.kind == "provider_retry" for m in events)


@pytest.mark.asyncio
async def test_retries_exhausted_still_visible_before_suspension(skills_dir: Path) -> None:
    """3 次全失败 → 2 条 provider_retry 后才挂起：运维能看到「烧了几次」而不只看到挂起。"""
    events: list[Any] = []
    inner = _FlakyClient(fail_times=99)
    client = RetryingModelClient(inner, config=_fast())
    runner = await _make_runner(skills_dir, model_client=client, events=events)

    outcome = await runner.run()

    assert outcome.end_reason == "suspended"
    assert inner.attempts == 3
    retries = [m for m in events if m.kind == "provider_retry"]
    assert [m.data["attempt"] for m in retries] == [1, 2], "max_attempts=3 → 恰两次退避重试"
    kinds = [m.kind for m in events]
    assert kinds.index("provider_retry") < kinds.index("turn_suspended")


@pytest.mark.asyncio
async def test_plain_client_without_retry_wrapper_emits_nothing(skills_dir: Path) -> None:
    """未套 RetryingModelClient 的裸 client：无 set_retry_observer → 静默跳过，不报错。"""
    events: list[Any] = []
    runner = await _make_runner(skills_dir, model_client=_FlakyClient(fail_times=0), events=events)
    outcome = await runner.run()
    assert outcome.success
    assert not any(m.kind == "provider_retry" for m in events)
