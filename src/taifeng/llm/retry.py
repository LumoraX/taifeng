"""指数退避 + 服务端 hint 的重试封装。

参照：codex codex-rs/core/src/client.rs::stream_max_retries
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from taifeng.llm.errors import LLMError, RateLimitError
from taifeng.loop.cancellation import CancellationToken

T = TypeVar("T")


@dataclass(frozen=True)
class RetryConfig:
    """重试策略配置。

    ``retryable_kinds`` 是显式白名单，匹配 ``LLMError.kind``。默认四类均满足「零产出即可安全重发」：
    限流 / 瞬时网络 / provider 5xx，以及 ``unreliable_finish``——接入方声明端点
    ``trust_finish_reason=False`` 时，网关错标成 ``content_filter`` 的零产出终止（实测多为上游瞬时
    抖动，重跑即过）。此前它
    ``retryable=True`` 却不在默认集合，形成两套「可重试」真相（ADR 0039 记录、ADR 0041 并入）。
    """

    max_attempts: int = 3
    min_delay_ms: int = 500
    max_delay_ms: int = 30_000
    backoff_multiplier: float = 2.0
    jitter: float = 0.2
    respect_server_hint: bool = True
    retryable_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"rate_limit", "transient_network", "server_error", "unreliable_finish"}
        )
    )


def compute_backoff_delay(attempt: int, config: RetryConfig) -> float:
    """指数退避 + 抖动，单位：秒。

    非流式的 ``retry_async`` 与流式的 ``RetryingModelClient`` 共用同一份退避算法
    （单一真相：两条重试路径的时序行为必须一致）。
    """
    base = config.min_delay_ms * (config.backoff_multiplier ** attempt)
    capped = min(base, config.max_delay_ms)
    jitter_range = capped * config.jitter
    delay_ms = capped + random.uniform(-jitter_range, jitter_range)
    return max(delay_ms, 0) / 1000.0


async def retry_async(
    func: Callable[[], Awaitable[T]],
    config: RetryConfig,
    *,
    cancel: CancellationToken | None = None,
) -> T:
    """带退避 + 取消感知的重试。

    Raises:
        LLMError: 最后一次尝试仍失败时抛出（含 kind 信息）
        asyncio.CancelledError: cancel 被触发
    """
    last_exc: BaseException | None = None
    for attempt in range(config.max_attempts):
        if cancel is not None:
            cancel.raise_if_cancelled()
        try:
            return await func()
        except LLMError as exc:
            last_exc = exc
            if exc.kind not in config.retryable_kinds:
                raise
            if attempt == config.max_attempts - 1:
                raise

            # 优先用 server hint
            if (
                config.respect_server_hint
                and isinstance(exc, RateLimitError)
                and exc.retry_after_seconds is not None
            ):
                delay = exc.retry_after_seconds
            else:
                delay = compute_backoff_delay(attempt, config)

            if cancel is not None:
                try:
                    await asyncio.wait_for(cancel.wait_cancelled(), timeout=delay)
                    # cancel triggered during wait
                    cancel.raise_if_cancelled()
                except TimeoutError:
                    pass  # 正常超时 = 完成等待
            else:
                await asyncio.sleep(delay)

    assert last_exc is not None  # for type checker
    raise last_exc
