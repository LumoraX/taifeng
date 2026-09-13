"""``RetryingModelClient`` —— 流式采样的有界重试装饰器。

`retry_async` 只能重试「返回一个值的协程」，而采样是 **async generator**：一旦
开始 yield 事件，重发就意味着调用方收到重复文本 / 重复 tool call。本模块把重试
下沉到流层，并用一条硬约束保证正确性：**只在本次 attempt 零内容产出时才重试**。

为什么不做进 provider 内部：`OneNetworkAttemptModelClient` 契约要求「每次
``stream`` 恰有一个网络 attempt」，strict audit 的 checkpoint lineage 依赖它。
本装饰器一次 ``stream`` 可发生多个 attempt，故**刻意不声明**该能力——与 strict
audit 互斥是设计约束，不是缺陷（见 ADR 0037）。

参照：codex ``codex-rs/core/src/client.rs::stream_max_retries``（退避与放弃判据）。
差异：codex 在流未开始前重试，taifeng 用「零产出」作为等价且更宽的判据。

可观测（R3，ADR 0039）：每次退避重试在睡眠**之前**把 :class:`RetryAttempt` 交给宿主注入的
观察者（``set_retry_observer``），由 TurnRunner 上 ``provider_retry`` 事件；本模块只产纯数据，
不依赖 loop 层事件类型，也不往 ``ResponseEvent`` 流里塞新 kind（金样形状不受重试次数影响）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from taifeng.llm.errors import LLMError
from taifeng.llm.retry import RetryConfig, compute_backoff_delay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.llm.client import ModelClient, ModelClientSession
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken

logger = logging.getLogger(__name__)

# 「内容产出」事件：出现任一即说明模型已经开始交付结果，重试会造成重复投递。
# created / server_model / rate_limits / prompt_cache 是元信息，不算产出。
_CONTENT_KINDS = frozenset({
    "text_delta",
    "reasoning_delta",
    "tool_call_delta",
    "tool_call_done",
    "structured_output",
    "normalized_output",
})

# 元信息事件：重试时不重复投递（调用方在首次 attempt 已经收到过）。
_META_KINDS = frozenset({"created", "server_model", "rate_limits"})


@dataclass(frozen=True)
class RetryAttempt:
    """一次即将退避重试的事实——供宿主上 R3 可观测总线（纯数据，不依赖 loop 层事件类型）。

    Attributes:
        attempt: 刚失败的 attempt 序号（1-based）。
        max_attempts: 本次 ``stream`` 的 attempt 上限（``RetryConfig.max_attempts``）。
        delay_seconds: 本次退避时长（退避算法与服务端 hint 取较大者）。
        reason: 触发重试的 ``LLMError.kind``
            （``transient_network`` / ``rate_limit`` / ``server_error`` …）。
        failure_class: 该错误的稳定 ``failure_class``。
        error_kind: 异常类名（与 ``FailureContext.error_kind`` 同义）。
        transport_phase: ``TransientNetworkError`` 的传输相位（``connect`` / ``stream``）；
            其他错误为 None。
        retry_after_seconds: 服务端 ``retry_after`` 提示（秒）；无则 None。
    """

    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: str
    failure_class: str
    error_kind: str
    transport_phase: str | None
    retry_after_seconds: float | None


# 重试观察者：宿主经 ``set_retry_observer`` 注入，每次退避前被 await 一次。
RetryObserver = Callable[[RetryAttempt], Awaitable[None]]


class _RetryingSession:
    """按 ``RetryConfig`` 重试底层 session 的 ``stream``。"""

    def __init__(
        self,
        inner: ModelClient,
        *,
        config: RetryConfig,
        cancel: CancellationToken,
        model: str | None,
    ) -> None:
        """冻结重试配置与本 turn 的 cancel / model。"""
        self._inner = inner
        self._config = config
        self._cancel = cancel
        self._model = model
        self._observer: RetryObserver | None = None

    async def __aenter__(self) -> _RetryingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def set_retry_observer(self, observer: RetryObserver | None) -> None:
        """注入重试观察者（可选协议，R3）。

        宿主用 ``getattr(session, "set_retry_observer", None)`` 探测——非重试型 session
        没有此方法即静默跳过，与 ``last_attempt_checkpoint`` 同一探测风格；透明包装器
        （如台账录制 session）靠 ``__getattr__`` 转发即可接通。观察者在每次退避**之前**
        被 await，事件因此先于等待出现在总线上（运维看到「正在退避」，不是事后补记）。
        """
        self._observer = observer

    def _retry_attempt(self, exc: LLMError, attempt: int, delay: float) -> RetryAttempt:
        """把一次已判定可重试的失败折成 ``RetryAttempt``（``_should_retry`` 通过后调用）。"""
        hint = getattr(exc, "retry_after_seconds", None)
        return RetryAttempt(
            attempt=attempt + 1,
            max_attempts=self._config.max_attempts,
            delay_seconds=delay,
            reason=exc.kind,
            failure_class=exc.failure_class,
            error_kind=type(exc).__name__,
            transport_phase=getattr(exc, "transport_phase", None),
            retry_after_seconds=float(hint) if isinstance(hint, (int, float)) else None,
        )

    def _should_retry(
        self, exc: BaseException, *, produced: bool, attempt: int
    ) -> TypeGuard[LLMError]:
        """判定本次失败是否还能重试（三个独立闸门，任一不过即放弃）。

        返回 True 蕴含 ``exc`` 是 ``LLMError``（闸门 2），故以 ``TypeGuard`` 表达——
        调用方据此直接读 ``exc.kind`` / ``exc.failure_class``，无需二次 isinstance。
        """
        # 闸门 1：已产出内容 —— 重发会让调用方收到重复文本 / 重复 tool call
        if produced:
            return False
        # 闸门 2：只有分类为可重试的 LLMError 才重试（鉴权 / 请求非法等立即抛）
        if not isinstance(exc, LLMError) or exc.kind not in self._config.retryable_kinds:
            return False
        # 闸门 3：次数上限
        return attempt + 1 < self._config.max_attempts

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """有界重试地流式采样；事件语义与被包装 client 完全一致。"""
        emitted_meta: set[str] = set()
        last_exc: BaseException | None = None

        for attempt in range(self._config.max_attempts):
            # R4：每次 attempt 前显式检查取消（退避期间被取消也在此收敛）
            self._cancel.raise_if_cancelled()
            produced = False
            try:
                session = self._inner.session(cancel=self._cancel, model=self._model)
                async with session as active:
                    async for event in active.stream(request):
                        if event.kind in _META_KINDS:
                            # 元信息只投递一次：重试的新 attempt 会重发 created /
                            # server_model，调用方不该看到第二遍
                            if event.kind in emitted_meta:
                                continue
                            emitted_meta.add(event.kind)
                        elif event.kind in _CONTENT_KINDS:
                            produced = True
                        yield event
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 分类判定在 _should_retry 内
                if not self._should_retry(exc, produced=produced, attempt=attempt):
                    raise
                last_exc = exc
                delay = compute_backoff_delay(attempt, self._config)
                # 服务端 hint 优先（RateLimitError 携带 retry_after_seconds）
                hint = getattr(exc, "retry_after_seconds", None)
                if self._config.respect_server_hint and isinstance(hint, (int, float)):
                    delay = max(delay, float(hint))
                logger.info(
                    "retrying model stream: attempt=%d kind=%s delay=%.2fs",
                    attempt + 1, getattr(exc, "kind", "unknown"), delay,
                )
                # R3：退避前先把重试事实交给观察者（宿主据此 emit provider_retry）——
                # 只留 logger.info 等于事件总线上看不见任何一次网络重试（ADR 0039）
                if self._observer is not None:
                    await self._observer(self._retry_attempt(exc, attempt, delay))
                await self._sleep_or_cancel(delay)

        # 循环只能经 return（成功）或 raise（放弃）退出；走到这里说明判据自相矛盾
        raise AssertionError(f"retry loop exhausted without terminal: {last_exc!r}")

    async def _sleep_or_cancel(self, delay: float) -> None:
        """退避等待，期间被取消立即收敛（R4）。

        用 ``wait_cancelled`` 竞速而非裸 ``asyncio.sleep``：长退避（限流 hint 可达
        数十秒）里用户 CancelTurn 必须马上生效，不能等睡满。
        """
        try:
            await asyncio.wait_for(self._cancel.wait_cancelled(), timeout=delay)
        except TimeoutError:
            # 超时 = 退避正常睡满，未被取消 —— 这是期望路径，继续下一次 attempt
            return
        # wait_cancelled 返回说明 token 已取消：以取消路径收敛，不再重试
        self._cancel.raise_if_cancelled()


class RetryingModelClient:
    """给任意 ``ModelClient`` 套上有界重试的装饰器。

    Args:
        inner: 被包装的 client；其事件语义原样透出。
        config: 重试策略（次数 / 退避 / 可重试 kind 集合）。默认 ``RetryConfig()``。

    注意：**不声明** ``OneNetworkAttemptModelClient``——一次 ``stream`` 可能发生
    多个网络 attempt，strict audit 模式会如实拒绝本装饰器（设计约束，见 ADR 0037）。

    可观测：session 暴露 ``set_retry_observer``，TurnRunner 自动接入并把每次退避重试
    上 ``provider_retry`` 事件（ADR 0039）；业务侧无需额外接线。
    """

    def __init__(self, inner: ModelClient, *, config: RetryConfig | None = None) -> None:
        self._inner = inner
        self._config = config or RetryConfig()

    @property
    def inner(self) -> ModelClient:
        """被包装的原始 client（业务侧取 provider 专属属性用）。"""
        return self._inner

    @property
    def bounded_retry(self) -> RetryConfig:
        """本装饰器的重试配置——同时是「已套有界重试」的探测标记（ADR 0041）。

        引擎默认包装前 ``getattr(client, "bounded_retry", None)`` 探测：非 None 即已套，跳过
        （防双层包装把 attempt 上限乘起来）。透明包装器靠 ``__getattr__`` 转发即可穿透。
        """
        return self._config

    def session(
        self, *, cancel: CancellationToken, model: str | None = None,
    ) -> ModelClientSession:
        """创建重试型 session（无 IO；真实 dispatch 在 ``stream``）。"""
        session: ModelClientSession = _RetryingSession(
            self._inner, config=self._config, cancel=cancel, model=model,
        )
        return session

    def __getattr__(self, name: str) -> Any:
        """未知属性转发到 inner —— 保留 ``capabilities`` / ``record_cache_read``
        等可选协议（内核用 getattr 探测，装饰器不该把它们挡掉）。"""
        return getattr(self._inner, name)


def with_default_retry(
    client: ModelClient, *, config: RetryConfig | None = None, enabled: bool = True,
) -> ModelClient:
    """内核默认套有界重试的唯一入口（ADR 0041）；幂等，三种情况原样返回：

    - ``enabled=False``：接入方显式关闭（业务自管重试，或测试复现「重试已耗尽」）；
    - 已套过（``bounded_retry`` 标记，穿透 ``__getattr__`` 转发包装）：防双层包装；
    - strict audit 的 ``AttemptObservableModelClient``：一次 ``stream`` 恰一个 attempt 是它的契约
      （ADR 0037），套重试会破坏 checkpoint lineage——audit 与自动重试互斥，audit 优先。

    ``AgentEngine`` / ``AgentEnginePool`` / ``EnginePool.create`` 三处都经此包装，重复经过无副作用。
    """
    if not enabled:
        return client
    if getattr(client, "bounded_retry", None) is not None:
        return client
    # 局部导入：llm/audit 经 conversation.journal 反向依赖 llm.errors，模块顶导入会成环
    from taifeng.llm.audit import AttemptObservableModelClient

    if isinstance(client, AttemptObservableModelClient):
        return client
    return RetryingModelClient(client, config=config)
