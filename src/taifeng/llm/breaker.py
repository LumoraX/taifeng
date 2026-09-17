"""``CircuitBreakingModelClient`` —— provider 级客户端断路器装饰器。

重试解决的是「这一次调用抖了一下」，断路器解决的是「上游整体挂了」：没有跨 turn 的
失败记忆时，中转宕 10 分钟意味着每个 turn 各自烧满 ``RetryConfig.max_attempts`` + 退避
再挂起，N 路并发 = 3N 次注定失败的请求，且业务侧拿不到「上游已降级」的信号。

设计要点（ADR 0042）：

- **状态挂装饰器实例**：一个 endpoint 一个断路器，进程内所有 engine / turn 共享同一份
  健康度。per-engine 会让 N 个 engine 各自独立学一遍「上游挂了」，与目标相反。
- **只计最终结局**：断路器构造时保证 inner 已套有界重试（``with_default_retry``，幂等），
  因此它看到的每次 ``stream`` 失败都是「重试已耗尽」，单次 attempt 失败不进计数。
- **open 态不触网**：立即抛 :class:`CircuitOpenError`（``retryable=True``）→ 保守失败策略落
  SUSPEND，挂起 detail 可区分「上游降级」与「本次调用失败」。
- **状态不持久化**：进程内运行态，重启即闭合；多 worker 各自学习（见 proposal Non-goals）。

参照：claw-code ``denial_breaker`` 的三态骨架（差异：那边计的是权限拒绝、turn 内生效，
这里计的是 provider 最终失败、跨 turn 生效）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from taifeng.llm.errors import CircuitOpenError, FailureClass, LLMError
from taifeng.llm.retrying import with_default_retry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.llm.client import ModelClient, ModelClientSession
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.retry import RetryConfig
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    """断路器三态。StrEnum 保证 JSON / 事件 data 里序列化成稳定字符串。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerConfig:
    """断路器阈值。默认值只在业务侧**显式包装**时生效——内核不默认套断路器。

    Attributes:
        trip_after: 连续多少次「最终失败」跳闸。
        cooldown_seconds: 首次跳闸后的冷却时长（秒）。
        cooldown_multiplier: 探测再失败时冷却的增长倍数。
        max_cooldown_seconds: 冷却上限，防止长时间故障把冷却放大到不可恢复。
    """

    trip_after: int = 3
    cooldown_seconds: float = 30.0
    cooldown_multiplier: float = 2.0
    max_cooldown_seconds: float = 300.0


@dataclass(frozen=True)
class CircuitTransition:
    """一次状态转换的事实——供宿主上 R3 可观测总线（纯数据，不依赖 loop 层事件类型）。

    Attributes:
        from_state: 转换前状态。
        to_state: 转换后状态。
        consecutive_failures: 转换时的连续最终失败计数。
        cooldown_seconds: 转换后生效的冷却时长（``half_open`` / ``closed`` 时为剩余配置值）。
        last_failure_class: 最近一次计入的失败的稳定 failure_class；从未失败则 None。
        last_error_kind: 最近一次计入的失败的异常类名；从未失败则 None。
    """

    from_state: str
    to_state: str
    consecutive_failures: int
    cooldown_seconds: float
    last_failure_class: str | None
    last_error_kind: str | None


# 状态转换观察者：宿主经 ``set_circuit_observer`` 注入，每次转换被 await 一次
# （与 ADR 0039 的 ``RetryObserver`` 同形）。
CircuitObserver = Callable[[CircuitTransition], Awaitable[None]]


class _Circuit:
    """断路器的可变状态机。挂在 client 实例上，被该 endpoint 的所有 session 共享。

    所有方法都是**同步**的：判定与状态写入之间不留 await 让出点，asyncio 单线程下
    即可保证「半开只放行一个探测」不被并发击穿（若改成 async 需另加锁）。
    """

    def __init__(self, config: BreakerConfig, clock: Callable[[], float]) -> None:
        self._config = config
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._cooldown_seconds = config.cooldown_seconds
        self._open_until = 0.0
        self._probe_in_flight = False
        self._last_failure_class: FailureClass | None = None
        self._last_error_kind: str | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown_seconds

    def admit(self) -> CircuitTransition | None:
        """判定本次 ``stream`` 能否放行。

        Returns:
            放行时返回伴随的状态转换（``open → half_open``）或 None。

        Raises:
            CircuitOpenError: 拒绝放行——调用方据此**不触网**直接失败。
        """
        now = self._clock()
        if self._state is CircuitState.CLOSED:
            return None
        if self._state is CircuitState.OPEN:
            if now < self._open_until:
                raise self._open_error(now)
            # 冷却到期：转半开，并把唯一的探测名额交给本次调用
            return self._transition(CircuitState.HALF_OPEN, probe_in_flight=True)
        # half_open：已有探测在途时，其余并发请求继续快速失败
        if self._probe_in_flight:
            raise self._open_error(now)
        self._probe_in_flight = True
        return None

    def record_success(self) -> CircuitTransition | None:
        """一次 ``stream`` 正常收尾：清零计数；半开态的探测成功则闭合。"""
        self._probe_in_flight = False
        if self._state is CircuitState.HALF_OPEN:
            self._consecutive_failures = 0
            self._cooldown_seconds = self._config.cooldown_seconds
            return self._transition(CircuitState.CLOSED)
        self._consecutive_failures = 0
        return None

    def record_failure(self, exc: BaseException) -> CircuitTransition | None:
        """一次 ``stream`` 以异常收尾：只有「可重试类 LLMError」才计入。

        取消是用户意图、不可重试类是确定性终态（鉴权 / 请求非法），两者都不代表上游健康度，
        计入只会误跳闸。非 LLMError（编程错误等）同样不计——断路器不该替代 bug 修复。
        """
        self._probe_in_flight = False
        if not (isinstance(exc, LLMError) and exc.retryable):
            return None
        self._last_failure_class = exc.failure_class
        self._last_error_kind = type(exc).__name__
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN:
            # 探测失败：冷却按 multiplier 增长并封顶，避免长故障下反复空探测
            self._cooldown_seconds = min(
                self._cooldown_seconds * self._config.cooldown_multiplier,
                self._config.max_cooldown_seconds,
            )
            return self._transition(CircuitState.OPEN)
        if (
            self._state is CircuitState.CLOSED
            and self._consecutive_failures >= self._config.trip_after
        ):
            return self._transition(CircuitState.OPEN)
        # 已 open（并发请求在跳闸前就被放行）：计数照记，状态无需再转
        return None

    def release_probe(self) -> None:
        """释放探测名额而不改变状态（取消路径：既不算成功也不算失败）。"""
        self._probe_in_flight = False

    def _transition(
        self, to_state: CircuitState, *, probe_in_flight: bool = False
    ) -> CircuitTransition:
        """执行状态转换并折出事实对象（唯一写 ``_state`` 的地方）。"""
        from_state = self._state
        self._state = to_state
        self._probe_in_flight = probe_in_flight
        if to_state is CircuitState.OPEN:
            self._open_until = self._clock() + self._cooldown_seconds
        logger.info(
            "provider circuit %s → %s: consecutive=%d cooldown=%.1fs",
            from_state.value, to_state.value,
            self._consecutive_failures, self._cooldown_seconds,
        )
        return CircuitTransition(
            from_state=from_state.value,
            to_state=to_state.value,
            consecutive_failures=self._consecutive_failures,
            cooldown_seconds=self._cooldown_seconds,
            last_failure_class=self._last_failure_class,
            last_error_kind=self._last_error_kind,
        )

    def _open_error(self, now: float) -> CircuitOpenError:
        """构造快速失败异常，带上剩余冷却（业务侧可展示「N 秒后自动恢复」）。"""
        remaining = max(0.0, self._open_until - now)
        return CircuitOpenError(
            f"provider circuit open: {self._consecutive_failures} consecutive failures, "
            f"{remaining:.1f}s remaining",
            failure_class=self._last_failure_class or "unknown",
            retry_after_seconds=remaining,
        )


class _BreakerSession:
    """把一次 ``stream`` 的最终结局喂给共享状态机的 turn 级 session。"""

    def __init__(self, inner_session: ModelClientSession, circuit: _Circuit) -> None:
        self._inner_session = inner_session
        self._circuit = circuit
        self._observer: CircuitObserver | None = None

    async def __aenter__(self) -> _BreakerSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def set_circuit_observer(self, observer: CircuitObserver | None) -> None:
        """注入状态转换观察者（可选协议，R3）。

        宿主用 ``getattr(session, "set_circuit_observer", None)`` 探测——没套断路器的
        session 无此方法即静默跳过，与 ``set_retry_observer`` 同一探测风格。
        """
        self._observer = observer

    def __getattr__(self, name: str) -> Any:
        """未知属性转发到 inner session —— 保住 ``set_retry_observer`` /
        ``last_attempt_checkpoint`` 等可选协议（内核用 getattr 探测，装饰器不该挡掉）。"""
        return getattr(self._inner_session, name)

    async def _notify(self, transition: CircuitTransition | None) -> None:
        """有转换且有观察者时上报（无转换是常态，不产生噪声事件）。"""
        if transition is not None and self._observer is not None:
            await self._observer(transition)

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """先过准入闸门，再原样透传 inner 的事件流；结局回喂状态机。"""
        # 拒绝时在此抛 CircuitOpenError —— 注意这发生在任何网络动作之前
        await self._notify(self._circuit.admit())
        try:
            async with self._inner_session as active:
                async for event in active.stream(request):
                    yield event
        except asyncio.CancelledError:
            # R4：取消不代表上游不健康，只归还探测名额
            self._circuit.release_probe()
            raise
        except Exception as exc:  # noqa: BLE001 —— 计不计由 record_failure 分类判定
            await self._notify(self._circuit.record_failure(exc))
            raise
        await self._notify(self._circuit.record_success())


class CircuitBreakingModelClient:
    """给任意 ``ModelClient`` 套上 provider 级断路器的装饰器。

    Args:
        inner: 被包装的 client。**若它尚未套有界重试**，构造时自动经
            ``with_default_retry``（幂等）补上——断路器必须看到「重试耗尽后的最终结局」，
            否则内核默认重试会落在断路器外层，计数就退化成单次 attempt。
        config: 阈值配置，默认 ``BreakerConfig()``。
        retry_config: 自动补重试时用的配置；inner 已套重试时忽略。
        clock: 单调时钟，默认 ``time.monotonic``（测试注入假时钟验证冷却窗口）。

    推荐叠放：``CircuitBreaking(Retrying(native))``。断路器不声明
    ``OneNetworkAttemptModelClient``——它包的就是可能发多次 attempt 的重试层。
    """

    def __init__(
        self,
        inner: ModelClient,
        *,
        config: BreakerConfig | None = None,
        retry_config: RetryConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._inner = with_default_retry(inner, config=retry_config)
        self._config = config or BreakerConfig()
        self._circuit = _Circuit(self._config, clock or time.monotonic)

    @property
    def inner(self) -> ModelClient:
        """被包装的 client（已含自动补上的重试层）。"""
        return self._inner

    @property
    def state(self) -> CircuitState:
        """当前断路器状态（运维 / 测试观察用）。"""
        return self._circuit.state

    @property
    def cooldown_seconds(self) -> float:
        """当前生效的冷却时长（探测失败后按 multiplier 增长）。"""
        return self._circuit.cooldown_seconds

    @property
    def breaker_config(self) -> BreakerConfig:
        """本装饰器的阈值配置——同时是「已套断路器」的探测标记。"""
        return self._config

    def session(
        self, *, cancel: CancellationToken, model: str | None = None,
    ) -> ModelClientSession:
        """创建断路器 session（无 IO；准入判定与真实 dispatch 都在 ``stream``）。"""
        session: ModelClientSession = _BreakerSession(
            self._inner.session(cancel=cancel, model=model), self._circuit,
        )
        return session

    def __getattr__(self, name: str) -> Any:
        """未知属性转发到 inner —— 保留 ``capabilities`` / ``bounded_retry`` 等可选协议。

        转发 ``bounded_retry`` 尤其关键：内核默认重试据此判定「已套有界重试」而跳过外层包装，
        断路器才能稳居最外层（ADR 0041 幂等入口）。
        """
        return getattr(self._inner, name)
