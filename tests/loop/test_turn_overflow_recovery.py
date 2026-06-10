"""A1 reactive-compaction-recovery：overflow 触发强制压缩 + 单次重采样自愈。

参照 openclaw pi-embedded-subscribe.ts 的 pendingCompactionRetry：provider 判
「上下文超长」时不硬失败，而是强制压缩一次 + 重采样一次（有界），仍失败才硬失败。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionOrchestrator
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.conversation.models import user_message
from taifeng.llm.client import ModelClient
from taifeng.llm.errors import ContextOverflowError
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    server_model,
    text_delta,
)
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.loop.event import EventMsg


class _FakeStore:
    """最小内存 store。"""

    def __init__(self) -> None:
        self.items: list[object] = []

    async def append(self, item: object) -> None:
        self.items.append(item)

    async def create_thread(self, **_: object) -> str:
        return "sub-thread"


class _OverflowSession:
    """单次采样会话：fail=True 时在 stream 首步抛 ContextOverflowError。"""

    def __init__(self, *, fail: bool, cancel: CancellationToken) -> None:
        self._fail = fail
        self._cancel = cancel

    async def __aenter__(self) -> _OverflowSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        self._cancel.raise_if_cancelled()
        if self._fail:
            raise ContextOverflowError("provider: context too long")
        yield created()
        yield server_model("mock-model")
        yield text_delta("recovered ok")
        yield completed(
            response_id="r",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            end_turn=True,
        )


class _OverflowThenOkClient(ModelClient):
    """前 ``fail_times`` 次采样抛 ContextOverflowError，之后正常返回。"""

    def __init__(self, *, fail_times: int = 1) -> None:
        self._calls = 0
        self._fail_times = fail_times

    @property
    def calls(self) -> int:
        return self._calls

    def session(
        self, *, cancel: CancellationToken, model: str | None = None
    ) -> _OverflowSession:
        self._calls += 1
        return _OverflowSession(fail=self._calls <= self._fail_times, cancel=cancel)


async def _make_runner(
    skills_dir: Path,
    *,
    model_client: ModelClient,
    compressors: CompressionOrchestrator | None = None,
    history: list | None = None,
    events: list | None = None,
    **extra: object,
) -> TurnRunner:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: EventMsg) -> None:
        if events is not None:
            events.append(ev.msg)

    return TurnRunner(
        entry_skill=entry,
        snapshot=registry.snapshot(),
        model_client=model_client,
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(),
        compressors=compressors,
        dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=history if history is not None else [],
        **extra,
    )


def _summary_compressor() -> CompressionOrchestrator:
    """handoff 压缩器，摘要由独立 MockClient 提供（不影响被测 client 的计数）。"""
    summary_client = MockClient(
        turns=[
            MockTurn(
                text="## 进度\n摘要内容",
                usage=TokenUsage(input_tokens=400, output_tokens=20),
            )
        ]
    )
    return CompressionOrchestrator(
        [HandoffCompactionStrategy(model_client=summary_client, model="mock-model")]
    )


@pytest.mark.asyncio
async def test_overflow_triggers_compaction_and_retry(skills_dir: Path) -> None:
    """首次 overflow → 强制压缩(phase=overflow) + emit provider_retry + 重采样成功。"""
    events: list = []
    history = [user_message(f"m{i} {'x' * 120}", thread_id="t") for i in range(8)]
    client = _OverflowThenOkClient(fail_times=1)
    runner = await _make_runner(
        skills_dir,
        model_client=client,
        compressors=_summary_compressor(),
        history=history,
        events=events,
    )
    outcome = await runner.run()

    assert outcome.success
    assert outcome.end_reason == "completed"
    kinds = [m.kind for m in events]
    assert "provider_retry" in kinds
    # overflow 自愈压缩走 phase=overflow（区别于 pre_turn / mid_turn）
    started = [m for m in events if m.kind == "compaction_started"]
    assert any(m.data.get("phase") == "overflow" for m in started)
    # 一次 overflow + 一次重采样 = 两次 session
    assert client.calls == 2


@pytest.mark.asyncio
async def test_overflow_recovery_bounded_once(skills_dir: Path) -> None:
    """连续 overflow → 自愈恰一次后硬失败（context_window），不无限重试。"""
    events: list = []
    history = [user_message(f"m{i} {'x' * 120}", thread_id="t") for i in range(8)]
    client = _OverflowThenOkClient(fail_times=99)  # 永远 overflow
    runner = await _make_runner(
        skills_dir,
        model_client=client,
        compressors=_summary_compressor(),
        history=history,
        events=events,
    )
    outcome = await runner.run()

    assert not outcome.success
    failed = [m for m in events if m.kind == "turn_failed"]
    assert failed and failed[0].data.get("failure_class") == "context_window"
    # 有界：provider_retry 恰一次；原始 + 一次重采样 = 两次 session，不再继续
    assert len([m for m in events if m.kind == "provider_retry"]) == 1
    assert client.calls == 2


@pytest.mark.asyncio
async def test_overflow_no_compressor_hard_fails(skills_dir: Path) -> None:
    """无压缩器 → overflow 直接硬失败，不浪费重采样、不发 provider_retry。"""
    events: list = []
    history = [user_message("hi", thread_id="t")]
    client = _OverflowThenOkClient(fail_times=1)
    runner = await _make_runner(
        skills_dir,
        model_client=client,
        compressors=None,
        history=history,
        events=events,
    )
    outcome = await runner.run()

    assert not outcome.success
    assert not any(m.kind == "provider_retry" for m in events)
    assert client.calls == 1  # 未重采样


@pytest.mark.asyncio
async def test_overflow_recovery_is_cache_aware(skills_dir: Path) -> None:
    """自愈压缩走 mid-turn 语义(DO_NOT_INJECT)：R2 不破 cache anchor。"""
    events: list = []
    history = [user_message(f"m{i} {'x' * 120}", thread_id="t") for i in range(10)]
    client = _OverflowThenOkClient(fail_times=1)
    runner = await _make_runner(
        skills_dir,
        model_client=client,
        compressors=_summary_compressor(),
        history=history,
        events=events,
        cache_anchor_index=2,
    )
    outcome = await runner.run()

    assert outcome.success
    # R2 契约：自愈压缩如实标注 cache_invalidated；mid-turn/overflow 语义不破 anchor
    completed_evs = [m for m in events if m.kind == "compaction_completed"]
    assert completed_evs
    assert all(m.data.get("cache_invalidated") is False for m in completed_evs)


@pytest.mark.asyncio
async def test_compaction_completed_carries_strategy_detail(skills_dir: Path) -> None:
    """3.1 接线：surgical_trim 的 detail 计数经 compaction_completed.data 透传（R3）。

    overflow 自愈 force_compress 走 SurgicalTrimStrategy（最高优先级唯一策略）：
    history 含两份相同大 tool output → dedup 1 条 + hard-clear 1 条，事件 detail 可机读。
    """
    from taifeng.context.strategies.surgical_trim import SurgicalTrimStrategy
    from taifeng.conversation.models import function_call, function_call_output

    events: list = []
    big = "Z" * 4_000
    history = [user_message("分析", thread_id="t")]
    for cid in ("c1", "c2"):
        history.append(function_call(cid, "read_file", "{}", thread_id="t"))
        history.append(
            function_call_output(call_id=cid, output=big, thread_id="t")
        )
    history += [user_message(f"pad{i}", thread_id="t") for i in range(4)]

    # hard 档必触发（默认 budget=200k 下 ratio 由 estimate 决定 → 直接调低阈值）
    strat = SurgicalTrimStrategy(
        soft_trim_ratio=0.0, hard_clear_ratio=0.0, min_dedup_chars=64,
        protect_tail_messages=2,
    )
    client = _OverflowThenOkClient(fail_times=1)
    runner = await _make_runner(
        skills_dir,
        model_client=client,
        compressors=CompressionOrchestrator([strat]),
        history=history,
        events=events,
    )
    outcome = await runner.run()

    assert outcome.success
    done = [m for m in events if m.kind == "compaction_completed"]
    assert done, "未发出 compaction_completed"
    detail = done[0].data.get("detail")
    assert detail == {"deduped": 1, "soft_trimmed": 0, "hard_cleared": 1}
