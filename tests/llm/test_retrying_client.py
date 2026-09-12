"""Wave 3 复现:``RetryingModelClient`` —— retry_async 的唯一接线点。

对应 openspec change ``wave3-provider-path-alignment``。改动前 `RetryingModelClient`
根本不存在（`retry_async` 是死代码：全仓除 __init__ 导出与文档/注释外零调用点，
而 turn.py 注释却写着「retry 已由 provider 内 retry_async 兜底」）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from taifeng.llm.errors import AuthenticationError, TransientNetworkError
from taifeng.llm.events import completed, created, server_model, text_delta
from taifeng.llm.retry import RetryConfig
from taifeng.llm.types import ApiMessage, ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _req() -> ApiRequest:
    return ApiRequest(model="m", messages=[ApiMessage(role="user", content="hi")])


def _fast_config(**kw: Any) -> RetryConfig:
    """退避压到毫秒级,测试不真睡。"""
    defaults: dict[str, Any] = {"max_attempts": 3, "min_delay_ms": 1, "max_delay_ms": 2}
    defaults.update(kw)
    return RetryConfig(**defaults)


class _ScriptedSession:
    """按剧本回放一次 attempt:先 yield 若干事件,再按需抛错。"""

    def __init__(self, *, fail: Exception | None, emit_text: bool) -> None:
        self._fail = fail
        self._emit_text = emit_text

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, _request: ApiRequest) -> AsyncIterator[Any]:
        yield created()
        yield server_model("mock-model")
        if self._emit_text:
            yield text_delta("已产出")
        if self._fail is not None:
            raise self._fail
        yield completed(
            response_id="r",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            end_turn=True,
        )


class _ScriptedClient:
    """每次 session() 取剧本下一项;记录 attempt 次数。"""

    def __init__(self, script: list[tuple[Exception | None, bool]]) -> None:
        self._script = script
        self.attempts = 0

    def session(
        self, *, cancel: CancellationToken, model: str | None = None,
    ) -> _ScriptedSession:
        idx = min(self.attempts, len(self._script) - 1)
        fail, emit_text = self._script[idx]
        self.attempts += 1
        return _ScriptedSession(fail=fail, emit_text=emit_text)


async def _drain(client: Any, *, cancel: CancellationToken | None = None) -> list[Any]:
    tok = cancel or CancellationToken(name="t")
    async with client.session(cancel=tok) as s:
        return [ev async for ev in s.stream(_req())]


async def test_retries_transient_network_and_succeeds() -> None:
    """a) 首次零产出撞 transient_network → 退避重试后成功,调用方看到完整一条流。"""
    from taifeng.llm.retrying import RetryingModelClient

    inner = _ScriptedClient([
        (TransientNetworkError("boom"), False),
        (None, True),
    ])
    events = await _drain(RetryingModelClient(inner, config=_fast_config()))

    assert inner.attempts == 2
    assert events[-1].kind == "completed"
    assert [ev.kind for ev in events].count("created") == 1, "重试不得重复元信息事件"
    assert [ev.kind for ev in events].count("server_model") == 1


async def test_no_retry_after_content_produced() -> None:
    """b) 已 yield 文本后失败 → 原样抛出,绝不重发(否则调用方收到重复内容)。"""
    from taifeng.llm.retrying import RetryingModelClient

    inner = _ScriptedClient([(TransientNetworkError("late"), True)])
    with pytest.raises(TransientNetworkError):
        await _drain(RetryingModelClient(inner, config=_fast_config()))
    assert inner.attempts == 1


async def test_non_retryable_kind_raises_immediately() -> None:
    """c) kind 不在 retryable_kinds 内 → 立即抛,不退避不重试。"""
    from taifeng.llm.retrying import RetryingModelClient

    inner = _ScriptedClient([(AuthenticationError("bad key"), False)])
    with pytest.raises(AuthenticationError):
        await _drain(RetryingModelClient(inner, config=_fast_config()))
    assert inner.attempts == 1


async def test_cancel_during_backoff_stops_retrying() -> None:
    """d) 退避期间取消 → 以取消结束,不再发起新 attempt。"""
    from taifeng.llm.retrying import RetryingModelClient

    inner = _ScriptedClient([(TransientNetworkError("boom"), False)])
    cancel = CancellationToken(name="t")
    client = RetryingModelClient(
        inner,
        config=_fast_config(
            max_attempts=5, min_delay_ms=5_000, max_delay_ms=10_000, jitter=0.0,
        ),
    )

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel.cancel()

    task = asyncio.create_task(_cancel_soon())
    with pytest.raises((asyncio.CancelledError, TransientNetworkError)):
        await _drain(client, cancel=cancel)
    await task
    assert inner.attempts <= 2


async def test_not_one_network_attempt_client() -> None:
    """e) 刻意不声明 OneNetworkAttemptModelClient —— 与 strict audit 互斥是设计。"""
    from taifeng.llm.client import OneNetworkAttemptModelClient
    from taifeng.llm.retrying import RetryingModelClient

    client = RetryingModelClient(_ScriptedClient([(None, True)]), config=_fast_config())
    assert not isinstance(client, OneNetworkAttemptModelClient)


def test_exported_from_package() -> None:
    """公共 API 一览必须能拿到它（否则业务侧无从接线）。"""
    import taifeng.llm as llm_pkg

    assert "RetryingModelClient" in llm_pkg.__all__


# ───────────────────────── ADR 0039：重试可观测（观察者协议） ─────────────────────────


class _CapturingObserver:
    """记录每次退避重试事实的观察者。"""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def __call__(self, attempt: Any) -> None:
        self.seen.append(attempt)


async def _drain_observed(client: Any, observer: Any) -> list[Any]:
    """接上观察者再 drain（模拟 TurnRunner 的 getattr 探测 + set_retry_observer）。"""
    async with client.session(cancel=CancellationToken(name="t")) as s:
        attach = getattr(s, "set_retry_observer", None)
        assert callable(attach), "重试型 session 必须暴露 set_retry_observer"
        attach(observer)
        return [ev async for ev in s.stream(_req())]


async def test_retry_observer_sees_each_backoff_with_facts() -> None:
    """f) 两次零产出失败后成功 → 观察者恰收到 2 条，序号/原因/分类/相位/上限齐全。"""
    from taifeng.llm.retrying import RetryAttempt, RetryingModelClient

    inner = _ScriptedClient([
        (TransientNetworkError("dns", transport_phase="connect"), False),
        (TransientNetworkError("eof"), False),
        (None, True),
    ])
    obs = _CapturingObserver()
    events = await _drain_observed(RetryingModelClient(inner, config=_fast_config()), obs)

    assert events[-1].kind == "completed" and inner.attempts == 3
    assert [a.attempt for a in obs.seen] == [1, 2]
    first, second = obs.seen
    assert isinstance(first, RetryAttempt)
    assert first.max_attempts == 3
    assert first.reason == "transient_network"
    assert first.failure_class == "provider_transport"
    assert first.error_kind == "TransientNetworkError"
    assert first.transport_phase == "connect"
    assert second.transport_phase == "stream", "未显式分类的传输错默认 stream 相位"
    assert first.retry_after_seconds is None
    assert first.delay_seconds >= 0.0


async def test_retry_observer_silent_when_no_retry_happens() -> None:
    """g) 首发即成功 → 观察者一次都不被调用（无重试就无事件）。"""
    from taifeng.llm.retrying import RetryingModelClient

    obs = _CapturingObserver()
    client = RetryingModelClient(_ScriptedClient([(None, True)]), config=_fast_config())
    await _drain_observed(client, obs)
    assert obs.seen == []


async def test_retry_observer_not_called_when_content_produced() -> None:
    """h) 已产出后失败 → 不重试，观察者也不得被调用（否则事件流谎报「重试中」）。"""
    from taifeng.llm.retrying import RetryingModelClient

    obs = _CapturingObserver()
    with pytest.raises(TransientNetworkError):
        await _drain_observed(
            RetryingModelClient(
                _ScriptedClient([(TransientNetworkError("late"), True)]), config=_fast_config(),
            ),
            obs,
        )
    assert obs.seen == []


async def test_retry_observer_carries_server_hint() -> None:
    """i) 限流带 retry_after → 观察者拿到 hint 原值，且退避不短于 hint。"""
    from taifeng.llm.errors import RateLimitError
    from taifeng.llm.retrying import RetryingModelClient

    err = RateLimitError("slow down")
    err.retry_after_seconds = 0.003
    obs = _CapturingObserver()
    await _drain_observed(
        RetryingModelClient(_ScriptedClient([(err, False), (None, True)]), config=_fast_config()),
        obs,
    )
    (only,) = obs.seen
    assert only.reason == "rate_limit" and only.failure_class == "provider_rate_limit"
    assert only.retry_after_seconds == 0.003
    assert only.delay_seconds >= 0.003


async def test_retry_observer_runs_before_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """j) 观察者在退避睡眠**之前**被调用：事件先于等待上总线，不是事后补记。"""
    from taifeng.llm import retrying
    from taifeng.llm.retrying import RetryingModelClient

    order: list[str] = []

    async def _fake_sleep(self: Any, delay: float) -> None:
        order.append("sleep")

    monkeypatch.setattr(retrying._RetryingSession, "_sleep_or_cancel", _fake_sleep)

    class _Obs:
        async def __call__(self, attempt: Any) -> None:
            order.append(f"observe#{attempt.attempt}")

    inner = _ScriptedClient([(TransientNetworkError("a"), False), (None, True)])
    await _drain_observed(RetryingModelClient(inner, config=_fast_config()), _Obs())
    assert order == ["observe#1", "sleep"]


async def test_retry_observer_reachable_through_forwarding_wrapper() -> None:
    """k) 透明包装 session（如台账录制 session 的 __getattr__ 转发）也能接上观察者。"""
    from taifeng.llm.retrying import RetryingModelClient

    class _Forwarding:
        """只实现 stream + __getattr__ 转发的透明包装（模拟 _RecordingSession）。"""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def __aenter__(self) -> _Forwarding:
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc: object) -> None:
            await self._inner.__aexit__(*exc)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def stream(self, request: ApiRequest) -> AsyncIterator[Any]:
            async for ev in self._inner.stream(request):
                yield ev

    inner = _ScriptedClient([(TransientNetworkError("a"), False), (None, True)])
    base = RetryingModelClient(inner, config=_fast_config())
    obs = _CapturingObserver()
    async with _Forwarding(base.session(cancel=CancellationToken(name="t"))) as s:
        s.set_retry_observer(obs)
        events = [ev async for ev in s.stream(_req())]
    assert events[-1].kind == "completed"
    assert [a.attempt for a in obs.seen] == [1]
