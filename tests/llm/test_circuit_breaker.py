"""复现：provider 级断路器缺失 —— 上游持续故障时内核没有跨 turn 记忆。

对应 openspec change ``provider-circuit-breaker``。改动前 ``llm/breaker.py`` 不存在：
`denial_breaker.py` 只管权限拒绝，`llm/` `loop/` 内无任何 provider 失败计数 / 健康度，
中转挂掉时每个 turn 各自烧满 ``RetryConfig.max_attempts`` 再挂起（N 路并发 = 3N 次
注定失败的请求），既不快速失败，也没有「上游已降级」信号可供业务侧降级 / 告警。

断路器只计一次 ``stream`` 的**最终结局**：它构造时保证 inner 已套有界重试
（``with_default_retry``，幂等），故单次 attempt 失败被重试层吸收，不进失败计数。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from taifeng.llm.errors import (
    AuthenticationError,
    ServerError,
    TransientNetworkError,
)
from taifeng.llm.events import completed, created, text_delta
from taifeng.llm.retry import RetryConfig
from taifeng.llm.types import ApiMessage, ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# 断路器测试一律关掉内层重试（max_attempts=1）——除非用例本身就在验「重试与计数的分工」。
_NO_RETRY = RetryConfig(max_attempts=1)
# 需要真重试的用例：退避压到毫秒级，测试不真睡。
_FAST_RETRY = RetryConfig(max_attempts=3, min_delay_ms=1, max_delay_ms=2)


def _req() -> ApiRequest:
    return ApiRequest(model="m", messages=[ApiMessage(role="user", content="hi")])


class _Clock:
    """可推进的假时钟——冷却窗口不能靠真 sleep 验证（慢且抖）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedSession:
    """一次 attempt：先 yield 事件，再按剧本抛错或正常收尾。"""

    def __init__(self, fail: Exception | None) -> None:
        self._fail = fail

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, _request: ApiRequest) -> AsyncIterator[Any]:
        yield created()
        if self._fail is not None:
            raise self._fail
        yield text_delta("ok")
        yield completed(
            response_id="r",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            end_turn=True,
        )


class _ScriptedClient:
    """每次 session() 取剧本下一项（用尽后重复最后一项）；``attempts`` 计真实触网次数。"""

    def __init__(self, script: list[Exception | None]) -> None:
        self._script = script
        self.attempts = 0

    def session(
        self, *, cancel: CancellationToken, model: str | None = None,
    ) -> _ScriptedSession:
        fail = self._script[min(self.attempts, len(self._script) - 1)]
        self.attempts += 1
        return _ScriptedSession(fail)


async def _drain(client: Any, *, cancel: CancellationToken | None = None) -> list[Any]:
    """跑完一次 stream；失败时异常原样上抛。"""
    tok = cancel or CancellationToken(name="t")
    async with client.session(cancel=tok) as s:
        return [ev async for ev in s.stream(_req())]


def _breaker(inner: Any, **kw: Any) -> Any:
    """造断路器；默认关掉内层重试并钉死假时钟。"""
    from taifeng.llm.breaker import BreakerConfig, CircuitBreakingModelClient

    clock = kw.pop("clock", None) or _Clock()
    retry_config = kw.pop("retry_config", _NO_RETRY)
    cfg_kw: dict[str, Any] = {"trip_after": 2, "cooldown_seconds": 30.0}
    cfg_kw.update(kw)
    return CircuitBreakingModelClient(
        inner,
        config=BreakerConfig(**cfg_kw),
        retry_config=retry_config,
        clock=clock,
    )


# --- a) 连续失败跳闸 + open 态不触网 ---


async def test_trips_open_after_consecutive_failures() -> None:
    """连续 trip_after 次「最终失败」→ open；此后 stream 不触网即抛 CircuitOpenError。"""
    from taifeng.llm.breaker import CircuitState
    from taifeng.llm.errors import CircuitOpenError

    inner = _ScriptedClient([TransientNetworkError("上游挂了")])
    breaker = _breaker(inner, trip_after=2)

    for _ in range(2):
        with pytest.raises(TransientNetworkError):
            await _drain(breaker)
    assert inner.attempts == 2
    assert breaker.state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError) as excinfo:
        await _drain(breaker)
    assert inner.attempts == 2, "open 态必须快速失败，不得再发网络请求"
    err = excinfo.value
    assert err.kind == "circuit_open"
    assert err.retryable is True
    assert err.failure_class == "provider_transport", "应继承触发时的 failure_class"
    assert err.retry_after_seconds == pytest.approx(30.0), "剩余冷却应作为服务端 hint 暴露"


# --- b) 成功清零 ---


async def test_success_resets_consecutive_failures() -> None:
    """失败—成功—失败：计数被成功清零，不到阈值不跳闸。"""
    from taifeng.llm.breaker import CircuitState

    inner = _ScriptedClient([TransientNetworkError("抖动"), None, TransientNetworkError("再抖")])
    breaker = _breaker(inner, trip_after=2)

    with pytest.raises(TransientNetworkError):
        await _drain(breaker)
    await _drain(breaker)
    with pytest.raises(TransientNetworkError):
        await _drain(breaker)

    assert breaker.state is CircuitState.CLOSED, "成功已清零计数，两次不连续的失败不该跳闸"


# --- c) 不可重试类 / 取消不计 ---


async def test_non_retryable_failures_not_counted() -> None:
    """鉴权失败是确定性终态，重试与断路都无意义 → 不进计数。"""
    from taifeng.llm.breaker import CircuitState

    inner = _ScriptedClient([AuthenticationError("key 无效")])
    breaker = _breaker(inner, trip_after=2)

    for _ in range(3):
        with pytest.raises(AuthenticationError):
            await _drain(breaker)
    assert breaker.state is CircuitState.CLOSED
    assert inner.attempts == 3, "不可重试类不跳闸，每次都该照常触网"


async def test_cancellation_not_counted() -> None:
    """取消是用户意图，不是上游故障 → 不进计数。"""
    from taifeng.llm.breaker import CircuitState

    inner = _ScriptedClient([asyncio.CancelledError()])
    breaker = _breaker(inner, trip_after=2)

    for _ in range(3):
        with pytest.raises(asyncio.CancelledError):
            await _drain(breaker)
    assert breaker.state is CircuitState.CLOSED


# --- d) 半开只放行一个探测 ---


class _GatedClient:
    """stream 中途停在 gate 上，用于制造「探测在途」的并发窗口。"""

    def __init__(self, *, fail_until: int = 0, fail: Exception | None = None) -> None:
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.attempts = 0
        self.gated_attempts = 0
        self._fail_until = fail_until
        self._fail = fail

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> Any:
        self.attempts += 1
        return self

    async def __aenter__(self) -> _GatedClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, _request: ApiRequest) -> AsyncIterator[Any]:
        yield created()
        # 前 fail_until 次直接失败（用来把断路器打到 open），之后才进入 gate
        if self.attempts <= self._fail_until:
            raise TransientNetworkError("挂")
        self.gated_attempts += 1
        self.entered.set()
        await self.gate.wait()
        if self._fail is not None:
            raise self._fail
        yield completed(
            response_id="r",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            end_turn=True,
        )


async def _trip_open(breaker: Any, inner: Any, times: int = 2) -> None:
    """把断路器打到 open（inner 必须是必失败剧本）。"""
    for _ in range(times):
        with pytest.raises(TransientNetworkError):
            await _drain(breaker)


async def test_half_open_admits_exactly_one_probe() -> None:
    """冷却到期后转 half_open：只放行 1 个探测，并发的其余请求仍快速失败。"""
    from taifeng.llm.breaker import BreakerConfig, CircuitBreakingModelClient, CircuitState
    from taifeng.llm.errors import CircuitOpenError

    clock = _Clock()
    # 同一个 inner：前 2 次直接失败把断路器打到 open，第 3 次（探测）卡在 gate 上
    inner = _GatedClient(fail_until=2)
    breaker = CircuitBreakingModelClient(
        inner,
        config=BreakerConfig(trip_after=2, cooldown_seconds=10.0),
        retry_config=_NO_RETRY,
        clock=clock,
    )
    await _trip_open(breaker, inner)
    assert breaker.state is CircuitState.OPEN

    clock.advance(10.0)
    probe = asyncio.create_task(_drain(breaker))
    await asyncio.wait_for(inner.entered.wait(), timeout=2)
    assert breaker.state is CircuitState.HALF_OPEN

    with pytest.raises(CircuitOpenError):
        await _drain(breaker)
    assert inner.gated_attempts == 1, "半开期间只能有一个探测在途"

    inner.gate.set()
    await asyncio.wait_for(probe, timeout=2)
    assert breaker.state is CircuitState.CLOSED, "探测成功 → 闭合"


# --- e) 探测失败重开 + 冷却翻倍封顶 ---


async def test_probe_failure_reopens_with_capped_backoff() -> None:
    """探测失败 → 重回 open，冷却按 multiplier 增长且不超过 max_cooldown_seconds。"""
    from taifeng.llm.breaker import BreakerConfig, CircuitBreakingModelClient, CircuitState

    clock = _Clock()
    inner = _ScriptedClient([TransientNetworkError("一直挂")])
    breaker = CircuitBreakingModelClient(
        inner,
        config=BreakerConfig(
            trip_after=2,
            cooldown_seconds=10.0,
            cooldown_multiplier=2.0,
            max_cooldown_seconds=25.0,
        ),
        retry_config=_NO_RETRY,
        clock=clock,
    )
    await _trip_open(breaker, inner)
    assert breaker.cooldown_seconds == pytest.approx(10.0)

    # 第 1 次探测失败 → 冷却 20
    clock.advance(10.0)
    with pytest.raises(TransientNetworkError):
        await _drain(breaker)
    assert breaker.state is CircuitState.OPEN
    assert breaker.cooldown_seconds == pytest.approx(20.0)

    # 第 2 次探测失败 → 40 被 25 封顶
    clock.advance(20.0)
    with pytest.raises(TransientNetworkError):
        await _drain(breaker)
    assert breaker.cooldown_seconds == pytest.approx(25.0), "冷却必须封顶"


# --- f) 只计最终结局，不计单次 attempt ---


async def test_retry_absorbed_attempts_do_not_count() -> None:
    """内层重试吸收掉的 attempt 失败不进计数：一次 stream 最多记一次失败。"""
    from taifeng.llm.breaker import CircuitState

    # 前两次 attempt 失败、第三次成功 —— 对断路器而言这是**一次成功**
    inner = _ScriptedClient([ServerError("502"), ServerError("502"), None])
    breaker = _breaker(inner, trip_after=2, retry_config=_FAST_RETRY)

    await _drain(breaker)

    assert inner.attempts == 3, "重试层该照常发 3 次"
    assert breaker.state is CircuitState.CLOSED


async def test_exhausted_retry_counts_once_per_stream() -> None:
    """重试耗尽只记 1 次失败：trip_after=2 需要两次 stream 才跳闸。"""
    from taifeng.llm.breaker import CircuitState

    inner = _ScriptedClient([ServerError("502")])
    breaker = _breaker(inner, trip_after=2, retry_config=_FAST_RETRY)

    with pytest.raises(ServerError):
        await _drain(breaker)
    assert breaker.state is CircuitState.CLOSED, "3 次 attempt 失败只算 1 次最终失败"
    assert inner.attempts == 3

    with pytest.raises(ServerError):
        await _drain(breaker)
    assert breaker.state is CircuitState.OPEN


# --- 组合性：默认重试与可选协议转发 ---


async def test_wraps_bare_client_with_default_retry() -> None:
    """裸 client 传进来会被自动套有界重试（ADR 0041 幂等入口），并透出 bounded_retry 标记。

    否则内核默认重试会落在断路器**外层**，断路器计的就成了单次 attempt。
    """
    from taifeng.llm.breaker import CircuitBreakingModelClient

    inner = _ScriptedClient([None])
    breaker = CircuitBreakingModelClient(inner, retry_config=_FAST_RETRY)

    assert breaker.bounded_retry == _FAST_RETRY
    await _drain(breaker)
    assert inner.attempts == 1


async def test_forwards_optional_session_protocols() -> None:
    """断路器 session 必须转发 set_retry_observer —— 否则网络重试在总线上再次隐身。"""
    from taifeng.llm.breaker import CircuitBreakingModelClient

    seen: list[Any] = []

    async def observer(attempt: Any) -> None:
        seen.append(attempt)

    inner = _ScriptedClient([ServerError("502"), None])
    breaker = CircuitBreakingModelClient(inner, retry_config=_FAST_RETRY)

    tok = CancellationToken(name="t")
    sess = breaker.session(cancel=tok)
    attach = getattr(sess, "set_retry_observer", None)
    assert callable(attach), "可选协议必须穿透断路器"
    attach(observer)
    async with sess as s:
        [ev async for ev in s.stream(_req())]

    assert [a.reason for a in seen] == ["server_error"]


async def test_forwards_client_capabilities() -> None:
    """``capabilities`` 必须穿透断路器——挡掉它会让带图请求静默降级成 text-only。"""
    from taifeng.llm.breaker import CircuitBreakingModelClient
    from taifeng.llm.client import ModelCapabilities, model_capabilities

    caps = ModelCapabilities(
        input_modalities=frozenset({"text", "image"}), provider="p", protocol="chat",
    )

    class _CapableClient(_ScriptedClient):
        capabilities = caps

    breaker = CircuitBreakingModelClient(_CapableClient([None]), retry_config=_NO_RETRY)
    assert model_capabilities(breaker) is caps


async def test_circuit_observer_sees_all_transitions() -> None:
    """三态转换都要交给观察者（R3：宿主据此上事件总线）。"""
    from taifeng.llm.breaker import BreakerConfig, CircuitBreakingModelClient

    clock = _Clock()
    inner = _ScriptedClient([TransientNetworkError("挂"), TransientNetworkError("挂"), None])
    breaker = CircuitBreakingModelClient(
        inner,
        config=BreakerConfig(trip_after=2, cooldown_seconds=5.0),
        retry_config=_NO_RETRY,
        clock=clock,
    )
    seen: list[Any] = []

    async def run_once() -> None:
        tok = CancellationToken(name="t")
        sess = breaker.session(cancel=tok)
        sess.set_circuit_observer(lambda t: _record(seen, t))
        async with sess as s:
            [ev async for ev in s.stream(_req())]

    for _ in range(2):
        with pytest.raises(TransientNetworkError):
            await run_once()
    clock.advance(5.0)
    await run_once()

    assert [(t.from_state, t.to_state) for t in seen] == [
        ("closed", "open"),
        ("open", "half_open"),
        ("half_open", "closed"),
    ]
    opened = seen[0]
    assert opened.consecutive_failures == 2
    assert opened.cooldown_seconds == pytest.approx(5.0)
    assert opened.last_failure_class == "provider_transport"
    assert opened.last_error_kind == "TransientNetworkError"


async def _record(sink: list[Any], transition: Any) -> None:
    """观察者是 async callable —— 与 ADR 0039 的 RetryObserver 同形。"""
    sink.append(transition)
