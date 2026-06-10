"""Sim 脚本单元 —— conformance 模拟器的单 turn 剧本定义。

与旧 ``MockTurn`` 字段名完全兼容（text / tool_calls / usage / delay_seconds /
structured / cache_read / cache_creation / request_id），存量测试机械替换类名即可迁移。
在其之上新增 conformance 专属指令：

- ``finish``：显式 finish 语义（废除「无工具调用 = end_turn」的 mock 自造规则，
  None 时沿用该默认推导以兼容存量脚本）；
- ``expect``：本 turn 的请求侧声明式断言（闭环校验，违反由 contract 层抛
  ``SimContractViolation``）；
- ``fault``：故障注入（限速 / 服务端错误 / 畸形参数 / 截断流）；
- ``await_signal`` / ``emit_signal``：确定性并发时序编排（由 ``SimCoordinator`` 协调）。

脚本耗尽一律抛 ``SimScriptExhausted``——绝不静默吐空 turn（多采样一次 LLM 必须立刻暴露）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    from collections.abc import Callable

    from taifeng.llm.types import ApiRequest


class SimScriptExhausted(Exception):  # noqa: N818 —— 信号语义命名，仓库先例见 SuspendSignal
    """脚本列表耗尽后仍被采样。

    典型暴露场景：engine 死循环、重放复读、多采样一次 LLM。
    旧 MockClient 在此静默返回空 turn（违反「禁 silent fallback」红线），sim 一律抛错。
    """


# finish 语义的合法取值（与主流 provider 的 finish_reason 对齐的最小子集）
SimFinish = Literal["end_turn", "tool_use", "length", "content_filter"]


@dataclass(frozen=True)
class SimFault:
    """单 turn 的故障注入指令（四种互斥变体，经工厂方法构造）。

    - ``rate_limit``：抛 ``RateLimitError``（带 retry_after hint，测重试退避）；
    - ``server_error``：抛 ``ServerError``（可重试 5xx）；
    - ``malformed_arguments``：tool_call arguments 吐非法 JSON（测坏参处置）；
    - ``truncate_stream``：产出 N 个事件后中断流且不发 completed（测半途崩溃恢复）。
    """

    kind: Literal["rate_limit", "server_error", "malformed_arguments", "truncate_stream"]
    retry_after_seconds: float | None = None
    """仅 rate_limit 变体使用：服务端建议的重试间隔。"""
    after_events: int | None = None
    """仅 truncate_stream 变体使用：产出多少个事件后截断。"""

    @classmethod
    def rate_limit(cls, retry_after_seconds: float | None = None) -> SimFault:
        """构造限速故障：本 turn 采样直接抛 ``RateLimitError``。"""
        return cls(kind="rate_limit", retry_after_seconds=retry_after_seconds)

    @classmethod
    def server_error(cls) -> SimFault:
        """构造服务端错误故障：本 turn 采样直接抛 ``ServerError``。"""
        return cls(kind="server_error")

    @classmethod
    def malformed_arguments(cls) -> SimFault:
        """构造畸形参数故障：tool_call 的 arguments 替换为非法 JSON 片段。"""
        return cls(kind="malformed_arguments")

    @classmethod
    def truncate_stream(cls, after_events: int) -> SimFault:
        """构造截断流故障：产出 ``after_events`` 个事件后流终止、无 completed。"""
        if after_events < 0:
            raise ValueError(f"after_events 必须 >= 0，得到 {after_events}")
        return cls(kind="truncate_stream", after_events=after_events)


@dataclass(frozen=True)
class SimExpect:
    """本 turn 的请求侧声明式断言（闭环校验面）。

    任一断言不满足时由 contract 层抛 ``SimContractViolation`` 并记入
    ``RequestLedger.violations``（双保险：异常被引擎兜底吞掉时 fixture 收尾仍能红）。
    """

    must_contain: tuple[str, ...] = ()
    """请求全文（system_prompt + messages 规范化串）必须包含的子串列表。"""
    must_include_output_for: tuple[str, ...] = ()
    """请求 messages 中必须存在这些 call_id 的工具结果（验证工具结果确实回传）。"""
    min_messages: int | None = None
    """messages 数量下界（含）。"""
    max_messages: int | None = None
    """messages 数量上界（含）。"""
    predicate: Callable[[ApiRequest], bool] | None = None
    """自定义谓词：返回 False 视为违规（复杂断言兜底口）。"""


@dataclass
class SimTurn:
    """单个 turn 的剧本（字段名兼容旧 ``MockTurn``）。

    回放语义：text 按片流式吐出 → tool_calls（默认 delta 分片 + done 收尾）→
    structured_output（仅请求带 response_format 时）→ prompt_cache（账本自动计算，
    ``cache_read`` 显式赋值时覆写）→ completed。
    """

    text: str = ""
    reasoning: str = ""
    """thinking 模型 reasoning 回放（reasoning-content-passback）：非空时在 text
    之前 emit ``reasoning_delta``（与真实 provider 产出顺序一致）；默认空=零变化。"""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """每项形如 ``{"id": ..., "name": ..., "arguments": "..."}``（与 MockTurn 同构）。"""
    usage: TokenUsage = field(
        default_factory=lambda: TokenUsage(input_tokens=100, output_tokens=50)
    )
    delay_seconds: float = 0.0
    structured: dict[str, Any] | list[Any] | None = None
    """structured_output 回放数据 —— 仅当本 turn 对应的 ApiRequest.response_format
    非 None 时 emit；否则忽略本字段。"""
    cache_read: int | None = None
    """prompt_cache 覆写：None → 由前缀账本自动计算；显式赋值 → 直接采用（兼容旧脚本手填值）。"""
    cache_creation: int = 0
    """prompt_cache 覆写：cache_creation_input_tokens（仅 cache_read 显式赋值时随同采用）。"""
    request_id: str | None = None
    """completed 事件回放的服务端 request-id（G3 工单关联）。"""
    finish: SimFinish | None = None
    """显式 finish 语义；None 时沿用「有 tool_calls → 不 end_turn」的默认推导。"""
    expect: SimExpect | None = None
    """本 turn 的请求侧断言；None 表示不加逐 turn 断言（通用合同校验仍然生效）。"""
    fault: SimFault | None = None
    """故障注入指令；None 表示正常回放。"""
    await_signal: str | None = None
    """开始产出前等待的信号名（确定性并发时序编排）。"""
    emit_signal: str | None = None
    """completed 前点亮的信号名（确定性并发时序编排）。"""
