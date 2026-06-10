"""失败处置裁决 policy —— 「失败默认挂起还是终态」从内核常量升格为可注入策略。

设计背景(openspec change ``failure-suspension-policy``):失败处置在不同部署形态下
答案相反——有人值守(HITL 服务端 / CLI)希望失败挂起等人裁决;无人值守(批处理 /
自动流水线)默认挂起等于死锁。这是业务策略,不是内核常量(R1),故 policy 化:

- 内核只提供两种**能力**(挂起 / 终态),「失败默认落哪边」由注入的 policy 决定;
- 真正的裁决人是 Resume 的提交者(人或业务自动决策器)——policy 是同步纯函数,
  只回答「挂还是不挂」,不做 IO、不做异步等待。

两个内置实现:

- :class:`ConservativeFailurePolicy`(**默认**):复刻原 ``turn._should_suspend_on_error``
  判据——可恢复 LLM 错误(retryable / 等外部介入类)挂起,其余失败与全部护栏触顶
  维持终态。不注入 policy 时全量行为与历史零变化。
- :class:`SuspendByDefaultPolicy`:一切失败一律挂起(「失败默认非终态」哲学的官方
  形态)。仅适合有人值守 / 有自动决策器的部署——无人裁决的挂起会永久滞留
  (TTL 兜底见 change ``suspension-ttl-auto-adjudication``)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

# 等外部介入即可恢复的 failure_class(鉴权 / 配额 / 余额)——与原
# ``_should_suspend_on_error`` 判据一致;quota / balance 命名保留以兼容其他 provider。
_AWAIT_EXTERNAL_CLASSES = frozenset(
    {"provider_auth", "provider_quota", "provider_balance"}
)


class FailureDisposition(StrEnum):
    """失败处置裁决结果。"""

    SUSPEND = "suspend"    # 落挂起,等 Resume 裁决(retry / abort)
    TERMINAL = "terminal"  # 维持现状终态(error / 护栏 end_reason)


@dataclass(frozen=True)
class FailureContext:
    """一次失败的裁决上下文(纯数据,无业务概念——R1)。

    Attributes:
        origin: 失败来源——``llm_error``(采样阶段 LLMError 重试耗尽)或
            ``guard_trip``(资源护栏触顶:max_iterations / resource_limit_exceeded /
            denial_circuit_open)。
        failure_class: origin=llm_error 时为 LLMError 的稳定分类(如
            ``provider_rate_limit`` / ``content_filter``);guard_trip 时为 None。
        end_reason: origin=guard_trip 时为护栏 end_reason;llm_error 时为 None。
        error_kind: 异常类名(可观测 / 审计);guard_trip 时为 None。
        retryable: origin=llm_error 时取 LLMError.retryable;guard_trip 恒 False。
        is_root: 是否 root turn(子 turn 失败的裁决可能与根不同,留给业务判断)。
            注意:detached spawn 子 thread 是独立根 turn(call_stack 为空),
            其失败 is_root 恒为 True —— 该字段只能区分 call_skill 子链,
            无法识别 spawn 子线程。
        iteration: 失败发生时的迭代序号(1-based)。
    """

    origin: Literal["llm_error", "guard_trip"]
    failure_class: str | None
    end_reason: str | None
    error_kind: str | None
    retryable: bool
    is_root: bool
    iteration: int


class FailureDispositionPolicy(Protocol):
    """失败处置裁决协议——业务侧注入,同步纯函数,禁 IO / 阻塞。"""

    def decide(self, ctx: FailureContext) -> FailureDisposition:
        """对一次失败给出处置裁决。

        Args:
            ctx: 失败上下文(来源 / 分类 / 迭代位置)。

        Returns:
            SUSPEND → turn 落挂起等 Resume;TERMINAL → 维持既有终态路径。
        """
        ...


class ConservativeFailurePolicy:
    """保守裁决(内核默认):复刻原 ``_should_suspend_on_error`` 判据,零行为变化。

    - llm_error 且(retryable 或 failure_class 属等外部介入类)→ SUSPEND
      (限流 / 瞬时网络 / 5xx / 鉴权 / 配额 / 余额——外部条件清除后重跑可过);
    - 其余 llm_error(content_filter / invalid_request 等确定性失败)→ TERMINAL
      (resume 也救不回,只会无限挂);
    - 全部 guard_trip → TERMINAL(历史行为:护栏触顶直接以 end_reason 终结)。
    """

    def decide(self, ctx: FailureContext) -> FailureDisposition:
        """按保守判据裁决;详见类 docstring。"""
        if ctx.origin == "llm_error" and (
            ctx.retryable or ctx.failure_class in _AWAIT_EXTERNAL_CLASSES
        ):
            return FailureDisposition.SUSPEND
        return FailureDisposition.TERMINAL


class SuspendByDefaultPolicy:
    """失败默认挂起:「失败默认非终态,人裁决终态」哲学的官方形态。

    **作用域**:policy 辖四类判定点——采样 LLMError 重试耗尽、护栏触顶
    (max_iterations / denial 断路器 / 会话 token)、RequestTooLargeError 预检、
    引擎级 K2 拒新 turn(后两类 SUSPEND 分别落 SYSTEM_RETRY / RESOURCE_LIMIT)。
    不在裁决范围:cancelled(取消非失败,直接走取消链)、非 LLM 异常
    (store / hook / 工具内部崩溃)——仍走既有终态路径。

    判定点内任意失败 → SUSPEND,由 Resume 的提交者决定 retry / abort。确定性
    失败原样 retry 必然再失败——业务 UI 应据挂起 detail 中的 failure_class
    (配合 G3 recovery 配方)引导用户走 abort 或改参后重试。
    """

    def decide(self, ctx: FailureContext) -> FailureDisposition:
        """一切失败 → SUSPEND;详见类 docstring。"""
        return FailureDisposition.SUSPEND


# 模块级默认单例:TurnRunner.failure_policy 为 None 时使用(零行为变化)。
DEFAULT_FAILURE_POLICY = ConservativeFailurePolicy()
