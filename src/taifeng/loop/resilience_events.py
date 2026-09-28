"""provider 韧性事件组装：断路器状态转换 + 网络层退避重试（R3，ADR 0042 / 0037）。

从 ``turn_sample.py`` 下沉的纯函数：只把 llm 层的纯数据（``CircuitTransition`` /
``RetryAttempt``）翻译为 loop 层事件，发送仍由调用方经 ``_emit`` 完成。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.loop.event import (
    ProviderCircuitClosed,
    ProviderCircuitHalfOpen,
    ProviderCircuitOpened,
    ProviderRetry,
)

if TYPE_CHECKING:
    from taifeng.llm.breaker import CircuitTransition
    from taifeng.llm.retrying import RetryAttempt

def circuit_transition_event(
    iteration: int, transition: CircuitTransition,
) -> ProviderCircuitOpened | ProviderCircuitHalfOpen | ProviderCircuitClosed:
    """断路器状态转换 → 三个事件之一。

    三态各占一个事件 kind（而非共用一个带 to_state 字段的事件）：运维按 kind 订阅
    「跳闸」告警，不必再解析 data；与 ``denial_circuit_open`` 的粒度也一致。

    Raises:
        ValueError: 未知状态（CircuitState 扩了新态却漏了接线——不静默吞掉）。
    """
    data = {
        "from_state": transition.from_state,
        "to_state": transition.to_state,
        "consecutive_failures": transition.consecutive_failures,
        "cooldown_seconds": transition.cooldown_seconds,
        "last_failure_class": transition.last_failure_class,
        "last_error_kind": transition.last_error_kind,
        "iteration": iteration,
    }
    if transition.to_state == "open":
        return ProviderCircuitOpened(data=data)
    if transition.to_state == "half_open":
        return ProviderCircuitHalfOpen(data=data)
    if transition.to_state == "closed":
        return ProviderCircuitClosed(data=data)
    raise ValueError(f"未知断路器状态: {transition.to_state}")


def provider_retry_event(iteration: int, attempt: RetryAttempt) -> ProviderRetry:
    """网络层退避重试 → ``provider_retry``（与 overflow 自愈共用事件，靠 ``reason`` 区分）。"""
    return ProviderRetry(data={
        "reason": attempt.reason,
        "iteration": iteration,
        "attempt": attempt.attempt,
        "max_attempts": attempt.max_attempts,
        "delay_seconds": attempt.delay_seconds,
        "failure_class": attempt.failure_class,
        "error_kind": attempt.error_kind,
        "transport_phase": attempt.transport_phase,
        "retry_after_seconds": attempt.retry_after_seconds,
    })
