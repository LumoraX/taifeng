"""turn 子 skill 派发：call_skill dispatcher 接口与子 runner 构造

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__dispatch_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json
from taifeng.loop.audit_skill import AuditedSkillDispatch
from taifeng.loop.event import SkillReturned, SubagentPolicyOverridden
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.dispatch import CallStack
from taifeng.tool.spec import ToolContext, ToolResult
from typing import Any

if TYPE_CHECKING:
    from taifeng.loop.turn import TurnRunner


class TurnDispatch:
    """turn 子 skill 派发协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__dispatch_owner = owner

    async def spawn_sub_runner(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
        audit_dispatch: AuditedSkillDispatch | None = None,
    ) -> ToolResult:
        """实际派发子 TurnRunner（已通过 K1 spawn 准入）。"""
        # G3 subagent-isolation-policy: 根据 dispatch_policy.subagent_approval_mode
        # 决定子 TurnRunner 收到的 permission_policy。inherit + 父=None → None；
        # auto_* + 父=None → 仍 None（无 policy 可包装，不引入额外门控）
        sub_permission_policy = self.__dispatch_owner.permission_policy
        mode = self.__dispatch_owner.dispatch_policy.subagent_approval_mode
        if mode != "inherit" and self.__dispatch_owner.permission_policy is not None:
            from taifeng.skill.dispatch import _SubagentAutoDecisionPolicy

            sub_permission_policy = _SubagentAutoDecisionPolicy(
                inner=self.__dispatch_owner.permission_policy,
                fallback="deny" if mode == "auto_deny" else "allow",
            )
            await self.__dispatch_owner._emit(
                SubagentPolicyOverridden(
                    data={
                        "target_skill_id": target.id,
                        "mode": mode,
                        "depth": parent_stack.depth + 1,
                    }
                )
            )

        # 子 turn：把 sub_args 序列化为 user_message 作为种子输入。
        # audit：child thread/seed 走 Journal 原子 started 批（thread_created/bound/
        # child-seed）+ child projection；child_state 让子 turn 递归走同一审计路径。
        # legacy：K7 把谱系持久化进 ThreadMetadata.extra，seed 直写 store。
        child_ctx = None
        child_audit_state = None
        if audit_dispatch is not None:
            child_ctx = await audit_dispatch.start_child(
                target=target,
                arguments=arguments,
                call_stack_path=tuple(parent_stack.path()),
            )
            sub_thread_id = child_ctx.child_thread_id
            seed = child_ctx.seed_item
            child_audit_state = child_ctx.child_state
        else:
            sub_thread_id = await self.__dispatch_owner.store.create_thread(
                cwd=None,
                entry_skill_id=target.id,
                source=f"subskill:{self.__dispatch_owner.entry_skill.id}",
                extra={
                    "parent_thread_id": self.__dispatch_owner.thread_id,
                    "spawn_depth": parent_stack.depth,
                    "stack_path": parent_stack.path(),
                },
            )
            from taifeng.conversation.models import user_message
            seed = user_message(
                json.dumps(arguments, ensure_ascii=False),
                thread_id=sub_thread_id,
            )
            await self.__dispatch_owner.store.append(seed)

        # 经 turn 模块惰性解析：既避开 turn ↔ turn_dispatch 的循环 import，
        # 也保住「打 turn.TurnRunner 这个模块级符号」的白盒注入点。
        from taifeng.loop import turn as _turn_mod

        sub_runner = _turn_mod.TurnRunner(
            entry_skill=target,
            snapshot=self.__dispatch_owner.snapshot,
            model_client=self.__dispatch_owner.model_client,
            tool_runtime=self.__dispatch_owner.tool_runtime,
            store=self.__dispatch_owner.store,
            compressors=self.__dispatch_owner.compressors,
            dispatch_policy=self.__dispatch_owner.dispatch_policy,
            budget=self.__dispatch_owner.budget,
            thread_id=sub_thread_id,
            submission_id=self.__dispatch_owner.submission_id,
            emit=self.__dispatch_owner.emit,
            cancel=ctx.cancel,
            image_input_policy=self.__dispatch_owner.image_input_policy,
            input_cost_estimator=self.__dispatch_owner.input_cost_estimator,
            hooks=self.__dispatch_owner.hooks,
            # G3: 透传或包装后的 permission_policy（auto_deny/auto_allow 时已包装）
            permission_policy=sub_permission_policy,
            request_metadata=self.__dispatch_owner.request_metadata,
            turn_index=self.__dispatch_owner.turn_index,
            script_executors=self.__dispatch_owner.script_executors,
            max_iterations=self.__dispatch_owner.max_iterations,
            # turn-resource-guards：子 turn 独立迭代预算（默认 cap=父初始 cap，
            # 不回写父——hermes 对标的有意语义）+ 断路器配置继承（子自建实例）
            iteration_budget=(
                self.__dispatch_owner.iteration_budget.child()
                if self.__dispatch_owner.iteration_budget is not None
                else None
            ),
            denial_breaker_config=self.__dispatch_owner.denial_breaker_config,
            # failure-suspension-policy: 子 turn 继承父的失败处置裁决 policy
            failure_policy=self.__dispatch_owner.failure_policy,
            failure_suspend_ttl_seconds=self.__dispatch_owner.failure_suspend_ttl_seconds,
            failure_suspend_on_expire=self.__dispatch_owner.failure_suspend_on_expire,
            auto_retry_count=self.__dispatch_owner.auto_retry_count,
            max_parallel_tool_calls=self.__dispatch_owner.max_parallel_tool_calls,
            # reasoning-content-passback: 子 turn 继承回传开关
            reasoning_passback=self.__dispatch_owner.reasoning_passback,
            # G4a: 子 turn 继承同一运行时能力快照
            capabilities=self.__dispatch_owner.capabilities,
            # K1: 子 turn 共享同一 spawn registry（配额贯穿整棵 turn 树）
            spawn_registry=self.__dispatch_owner.spawn_registry,
            # K2: 子 turn 继承会话 token 上限（基线 = 父基线 + 父本 turn 已用）
            session_tokens_used=self.__dispatch_owner.session_tokens_used
            + self.__dispatch_owner.total_usage.total_tokens,
            max_session_tokens=self.__dispatch_owner.max_session_tokens,
            # K3: 子 turn 共享同一 memory store
            memory_store=self.__dispatch_owner.memory_store,
            # 认知回路 ⑦：子 turn 继承同一战绩判定器（嵌套派发也按统一判据沉淀）
            outcome_judge=self.__dispatch_owner.outcome_judge,
            # T6: 子 turn 继承同一 deferred 暴露阈值（整棵 turn 树一致）
            recall_threshold=self.__dispatch_owner.recall_threshold,
            # 子 turn 继承同一召回后端存在性（整棵 turn 树一致）
            has_recall_backend=self.__dispatch_owner.has_recall_backend,
            call_stack=parent_stack,
            history_buffer=[seed],
            # audit：子 turn 携带 child audit_state（共享根 coordinator/lease、同一
            # projector 的 child thread）→ 子的 LLM/Tool/Skill 效果递归走同一审计路径。
            audit_state=child_audit_state,
        )
        outcome = await sub_runner.run()
        # audit：strict 能力面不接受挂起（skill_suspension/HITL/failure_policy 均已被
        # 静态拒）；子 turn 若仍挂起属 capability 契约违约 → fail closed 冻结。
        if child_ctx is not None and outcome.end_reason == "suspended":
            assert self.__dispatch_owner.audit_state is not None
            raise self.__dispatch_owner.audit_state.coordinator.freeze(
                RuntimeError("audited child skill suspended (capability violation)")
            ) from None
        # 子 turn 挂起：子 thread 内已落 SuspensionRecord 并 emit turn_suspended。
        # 父的 call_skill 必须随之挂起（而非把挂起误当成功/失败回填），否则父 turn
        # 会带着占位结果继续/完成，子挂起永远无法回传父调用栈（item d 的根因）。
        # 抛 SuspendSignal(reason=CHILD_SKILL) → 父 _dispatch_one 捕获为 outcome.suspend
        # → 父 _BatchSuspend → 父落自己的 SuspensionRecord（pending 携带 sub_thread_id），
        # 逐层上抛至根 → 根 emit turn_suspended。resume 时 engine 续跑链据 sub_thread_id
        # 先续跑子 thread 拿结果，再回填本 call_skill 的 function_call_output。
        if outcome.end_reason == "suspended":
            from taifeng.suspend.reason import PendingRequest, SuspendReason
            from taifeng.suspend.signal import SuspendSignal as _SuspendSignal

            raise _SuspendSignal(
                PendingRequest(
                    request_id=self.__dispatch_owner._suspend_id_factory(),
                    reason=SuspendReason.CHILD_SKILL,
                    # 父 resume 时据 related_call_id 定位要回填 output 的 call_skill；
                    # 据 detail.sub_thread_id 定位要先续跑的子 thread。
                    # 必须用【父 call_skill 的 call_id】（call_skill builtin 经 extras 透传，
                    # = LLM 给的 tool_call id，父 function_call 落盘用它），而非 ctx.call_id
                    # （= 子帧 sub_call_id "sk_*"）。否则回填 output 与父 function_call 失配 →
                    # OpenAI-compat orphan tool → 400。extras 缺失时退回 ctx.call_id（仅
                    # 防御性：call_skill 派发路径必设此 key）。
                    related_call_id=ctx.extras.get(
                        "parent_call_skill_call_id"
                    ) or ctx.call_id,
                    detail={"sub_thread_id": sub_thread_id, "skill_id": target.id},
                )
            )

        await self.__dispatch_owner._emit(
            SkillReturned(
                data={
                    "skill_id": target.id,
                    "call_id": ctx.call_id,
                    "success": outcome.success,
                    "summary": outcome.final_text[:200],
                }
            )
        )

        # —— 认知回路 ⑦ 沉淀：判战绩 → 落旁路记录 → emit 事件 ——
        # 注：suspended 已在上方提前 raise SuspendSignal 返回，到此 outcome 必为终态。
        from taifeng.conversation.models import skill_outcome_item
        from taifeng.loop.event import SkillOutcomeRecorded
        from taifeng.skill.outcome import (
            SelectionOrigin,
            SkillExecutionContext,
            SkillExecutionRecord,
        )

        _verdict = self.__dispatch_owner._outcome_judge.judge(
            SkillExecutionContext(
                success=outcome.success,
                end_reason=outcome.end_reason,
                error=outcome.error,
            )
        )
        # parent_stack.current 是【目标自身】的栈帧（call_skill 已 push target）；
        # 其 .call_id == ctx.call_id（本次执行），其 .parent_call_id 才是【调用方】的
        # call_id（root 派发时为 None）——这正是记录要的 parent_call_id。
        _self_frame = parent_stack.current
        # T7 召回溯源：若本 turn 内 target 曾被 search_skills 召回选中（命中 turn 内
        # 溯源映射）→ 记 discovered + 该候选 confidence；否则保持 v1 行为 whitelist/None。
        # C5：映射是 turn 内态、不持久化，跨 turn / 冷 resume 后退化为 whitelist/None。
        _traced = self.__dispatch_owner._selection_trace.get(target.id)
        _selection_origin: SelectionOrigin = (
            _traced[0] if _traced is not None else "whitelist"
        )
        _selection_confidence = _traced[1] if _traced is not None else None
        _record = SkillExecutionRecord(
            skill_id=target.id,
            call_id=ctx.call_id,
            parent_call_id=_self_frame.parent_call_id if _self_frame else None,
            depth=parent_stack.depth,
            source=target.source,
            trust_tier=None,  # v1 留空；来源信任分层在后续相位填
            # 经 search_skills 召回派发 → discovered + confidence；否则 v1 的 whitelist/None
            selection_origin=_selection_origin,
            selection_confidence=_selection_confidence,
            outcome=_verdict.status,
            outcome_signal_source=_verdict.signal_source,
            end_reason=outcome.end_reason,
            error_detail=(outcome.error[:200] if outcome.error else None),
            cost_tokens=outcome.usage.total_tokens,
            cost_duration_ms=outcome.duration_ms,
            cost_iterations=outcome.iterations,
            ts_unix=self.__dispatch_owner._now_factory(),
        )
        if child_ctx is not None and audit_dispatch is not None:
            # audit：原子 finished + thread_terminal + skill_outcome（先于外层 Tool
            # outcome）；skill_outcome 会话项由 finish_child 落 durable + 投影。
            from taifeng.conversation.journal.records import (
                SkillStatus as _SkillStatus,
            )
            from taifeng.conversation.journal.records import (
                StableErrorV1 as _StableErrorV1,
            )
            if outcome.end_reason == "cancelled":
                _status = _SkillStatus.CANCELLED
            elif outcome.success:
                _status = _SkillStatus.SUCCESS
            else:
                _status = _SkillStatus.ERROR
            _child_error = (
                None
                if _status is _SkillStatus.SUCCESS
                else _StableErrorV1(
                    code="skill_child_terminal_error",
                    class_name="ChildSkillError",
                    failure_class="skill_error",
                    retryable=False,
                )
            )
            await audit_dispatch.finish_child(
                child=child_ctx,
                status=_status,
                end_reason=outcome.end_reason,
                final_text=outcome.final_text,
                outcome_payload=_record.as_payload(),
                error=_child_error,
            )
        else:
            await self.__dispatch_owner.store.append(
                skill_outcome_item(_record.as_payload(), thread_id=sub_thread_id)
            )
        await self.__dispatch_owner._emit(SkillOutcomeRecorded(data=_record.as_payload()))

        if outcome.success:
            return ToolResult.ok(outcome.final_text, sub_thread_id=sub_thread_id)
        return ToolResult.error(
            f"sub_skill_failed: {outcome.error or outcome.end_reason}",
            sub_thread_id=sub_thread_id,
        )
