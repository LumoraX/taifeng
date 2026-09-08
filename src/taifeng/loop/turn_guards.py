"""turn 守卫：延迟暴露判定 / SYSTEM_RETRY 挂起判定 / 资源守卫触顶转挂起

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__guards_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from taifeng.loop.failure_policy import (
    DEFAULT_FAILURE_POLICY,
    FailureContext,
    FailureDisposition,
)
from taifeng.suspend.reason import PendingRequest
from taifeng.suspend.signal import SuspendSignal

if TYPE_CHECKING:
    from taifeng.loop.turn import TurnRunner


class TurnGuards:
    """turn 守卫协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__guards_owner = owner

    def deferred_exposure_active(self) -> bool:
        """本 entry 是否处于 deferred 召回模式（决定是否暴露 search_skills 工具）。

        与 ``render_system_prompt`` 共用 ``effective_child_recall`` 同一判定（同源），
        保证「prompt 文本是否列 child」与「per-turn 是否给 search_skills」严格一致。
        可见 child 数经 ``visible_child_skills`` G4 过滤后统计（与 prompt 同口径）。

        Returns:
            True → deferred（保留 search_skills 工具）；False → inline（剔除）。
        """
        from taifeng.skill.visibility import (
            effective_child_recall,
            visible_child_skills,
        )

        visible = visible_child_skills(
            self.__guards_owner.entry_skill, self.__guards_owner.snapshot, self.__guards_owner.capabilities
        )
        mode = effective_child_recall(
            self.__guards_owner.entry_skill,
            child_count=len(visible),
            threshold=self.__guards_owner.recall_threshold,
            has_recall_backend=self.__guards_owner.has_recall_backend,
        )
        return mode == "deferred"

    def system_retry_pending(self, e: Exception) -> Any:
        """构造 LLM 失败挂起的 SYSTEM_RETRY PendingRequest(policy 裁决 SUSPEND 后用)。

        detail 携带 failure_class / retry_after / 异常类名(业务 UI 引导裁决用);
        到期自动 retry 谱系计数(auto_retry_count)非零时一并落入,fire 时与
        failure_suspend_max_auto_retries 比对熔断。
        """
        from taifeng.suspend.reason import SuspendReason

        detail: dict[str, Any] = {
            "failure_class": getattr(e, "failure_class", None),
            "retry_after_seconds": getattr(e, "retry_after_seconds", None),
            "kind": type(e).__name__,
        }
        if self.__guards_owner.auto_retry_count:
            detail["auto_retry_count"] = self.__guards_owner.auto_retry_count
        return PendingRequest(
            request_id=self.__guards_owner._suspend_id_factory(),
            reason=SuspendReason.SYSTEM_RETRY,
            # suspension-ttl:内核挂起按构造期声明的存活期(默认永不过期)
            ttl_seconds=self.__guards_owner.failure_suspend_ttl_seconds,
            on_expire=self.__guards_owner.failure_suspend_on_expire,
            payload_schema={
                "type": "object",
                "properties": {"action": {"enum": ["retry", "abort"]}},
            },
            related_call_id=None,
            detail=detail,
        )

    def maybe_suspend_on_guard_trip(
        self, end_reason: str, guard_snapshot: dict[str, Any] | None = None
    ) -> None:
        """护栏触顶时问失败处置 policy:SUSPEND → 抛 RESOURCE_LIMIT 挂起;TERMINAL → 返回。

        三个护栏(max_iterations / resource_limit_exceeded / denial_circuit_open)
        的 break 点都在迭代边界——当轮 fc/output 已配对(K5),此处抛 SuspendSignal
        不产生孤儿;信号被 run() 的 ``except SuspendSignal`` 捕获后落盘挂起。
        resume retry = 重建 runner 续跑采样循环(预算 / 断路器随重建按原 cap 重置);
        abort = 在挂起点落失败终态。默认 policy(保守)对 guard_trip 恒 TERMINAL,
        本方法直接返回 → 调用方走既有 break,零行为变化。

        Args:
            end_reason: 触顶的护栏 end_reason(进 FailureContext 与挂起 detail)。
            guard_snapshot: 护栏快照(如断路器 consecutive/recent,R3 可观测);
                None 时 detail 只携带 end_reason。

        Raises:
            SuspendSignal: policy 裁决 SUSPEND 时,携带 RESOURCE_LIMIT pending。
        """
        from taifeng.suspend.reason import SuspendReason

        policy = self.__guards_owner.failure_policy or DEFAULT_FAILURE_POLICY
        disposition = policy.decide(FailureContext(
            origin="guard_trip",
            failure_class=None,
            end_reason=end_reason,
            error_kind=None,
            retryable=False,
            is_root=self.__guards_owner._is_root,
            iteration=self.__guards_owner._current_iteration,
        ))
        if disposition is not FailureDisposition.SUSPEND:
            return
        detail: dict[str, Any] = {"end_reason": end_reason}
        if guard_snapshot:
            detail["guard_snapshot"] = guard_snapshot
        if self.__guards_owner.auto_retry_count:
            detail["auto_retry_count"] = self.__guards_owner.auto_retry_count
        # K2(session_tokens)到期自动 retry 无人携带预算增额、必然无效 →
        # 恒 abort(覆写 failure_suspend_on_expire 配置);其余护栏照配置
        on_expire: Literal["abort", "retry"] = (
            "abort" if end_reason == "resource_limit_exceeded"
            else self.__guards_owner.failure_suspend_on_expire)
        raise SuspendSignal(PendingRequest(
            request_id=self.__guards_owner._suspend_id_factory(),
            reason=SuspendReason.RESOURCE_LIMIT,
            # suspension-ttl:内核挂起按构造期声明的存活期(默认永不过期)
            ttl_seconds=self.__guards_owner.failure_suspend_ttl_seconds,
            on_expire=on_expire,
            payload_schema={
                "type": "object",
                "properties": {"action": {"enum": ["retry", "abort"]}},
            },
            related_call_id=None,
            detail=detail,
        ))

    # -----------------------------------------------------------------
    # 实现已下沉 turn_context.py（Wave 4）。以下为薄委托：TurnRunner 是唯一白盒
    # 寻址面，兄弟模块与测试按这些原名调用/打桩，签名逐字保留。
    # -----------------------------------------------------------------
