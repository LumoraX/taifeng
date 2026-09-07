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
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

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

    async def __aenter__(self) -> _RetryingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _should_retry(self, exc: BaseException, *, produced: bool, attempt: int) -> bool:
        """判定本次失败是否还能重试（三个独立闸门，任一不过即放弃）。"""
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
    """

    def __init__(self, inner: ModelClient, *, config: RetryConfig | None = None) -> None:
        self._inner = inner
        self._config = config or RetryConfig()

    @property
    def inner(self) -> ModelClient:
        """被包装的原始 client（业务侧取 provider 专属属性用）。"""
        return self._inner

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
