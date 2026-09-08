"""turn 工具结算：outcome 记账 / 选择追踪 / doom-loop 提示 / ToolContext 构造 / seed 补全

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__tooling_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import json
import logging
from taifeng.conversation.models import ResponseItem, function_call_output
from taifeng.llm.errors import AttachmentTooLargeError, ImageCountExceededError, InvalidImageError, UnsupportedModalityError
from taifeng.llm.image_input import admit_tool_attachments
from taifeng.loop.event import DenialCircuitOpen, DoomLoopCircuitOpen, DoomLoopWarned
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch, parse_tool_arguments
from taifeng.loop.turn_helpers import _latest_user_text
from taifeng.tool.spec import ToolContext, ToolResult
from typing import Any

if TYPE_CHECKING:
    from taifeng.loop.turn import TurnRunner

logger = logging.getLogger(__name__)


class TurnTooling:
    """turn 工具结算协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__tooling_owner = owner

    async def note_tool_outcome(
        self, name: str, result: Any, arguments_raw: str = ""
    ) -> None:
        """配对回填后的单点记账（turn-resource-guards）。

        - DenialBreaker：``ToolResult.data["reason"] ∈ {hook_denied,
          permission_denied}`` 计 deny（含 HITL ask 超时产生的 deny —— 它就是
          deny 结果）；成功结果重置 consecutive。其他 error 中性（不计任何一边）。
          恰好越阈值那次 emit ``denial_circuit_open``（单次闩锁）。
        - DoomLoopDetector：仅在**成功**结果上记 (tool, arguments_raw)。连续 N 次
          同签名 → ``warn``（注中性事实让模型自改 + emit）；警后到 2N → ``open``
          （emit + 置闩锁，迭代边界终止）。deny/error 不计（交 DenialBreaker）。
        - refund：spec 静态声明 ``refunds_iteration`` 且本次**成功** → 外层迭代
          预算退还一步（失败轮照常计费；不暴露为 LLM 可触发语义）。
        """
        # T7 召回溯源：search_skills 成功完成 → 把返回候选登记进 turn 内溯源映射，
        # 供后续 call_skill 派发时判定 discovered。只对 search_skills 做，别误伤其他工具。
        if name == "search_skills" and not result.is_error:
            self.__tooling_owner._register_selection_trace(result.output)
        deny_reason = result.data.get("reason") if result.is_error else None
        if self.__tooling_owner._denial_breaker is not None:
            if deny_reason in ("hook_denied", "permission_denied"):
                if self.__tooling_owner._denial_breaker.record_denial(name):
                    await self.__tooling_owner._emit(
                        DenialCircuitOpen(data=self.__tooling_owner._denial_breaker.snapshot())
                    )
            elif not result.is_error:
                self.__tooling_owner._denial_breaker.record_success()
        # doom-loop：只观察成功调用的 (tool, args) 重复空转
        if self.__tooling_owner._doom_loop is not None and not result.is_error:
            action = self.__tooling_owner._doom_loop.record(name, arguments_raw)
            if action == "warn":
                await self.__tooling_owner._inject_doom_loop_notice(self.__tooling_owner._doom_loop.snapshot())
                await self.__tooling_owner._emit(DoomLoopWarned(data=self.__tooling_owner._doom_loop.snapshot()))
            elif action == "open":
                await self.__tooling_owner._emit(
                    DoomLoopCircuitOpen(data=self.__tooling_owner._doom_loop.snapshot())
                )
        if not result.is_error and self.__tooling_owner.iteration_budget is not None:
            spec = self.__tooling_owner.tool_runtime.spec_for(name)
            if spec is not None and spec.refunds_iteration:
                self.__tooling_owner.iteration_budget.refund(1)

    def register_selection_trace(self, search_output: str) -> None:
        """解析 search_skills 返回的候选 JSON，登记进 turn 内召回溯源映射（T7）。

        把每个候选 ``(skill_id → ("discovered", confidence))`` 写入
        ``self.__tooling_owner._selection_trace``，供 ``_spawn_sub_runner`` 据派发目标 id 判定
        ``selection_origin``/``selection_confidence``。

        覆盖语义：同一 skill_id 后到的召回**覆盖**先到的——最近一次召回的 confidence
        与「LLM 当前据以决定 call_skill」最相关，故取最新。

        容错：search_skills handler 产出的是 ``json.dumps(payload)``（list[dict]，每项
        含 ``skill_id``/``confidence``）。这里只在结构符合预期时登记；结构异常（坏 JSON /
        非 list / 缺字段）跳过该项即可——溯源是 best-effort 增益，缺失只是退化为
        whitelist/None，不影响派发主流程（非 silent fallback：不伪造默认 confidence，
        缺字段就不登记，让其走 v1 行为）。

        Args:
            search_output: search_skills 工具的成功 ``ToolResult.output``（候选 JSON 串）。
        """
        try:
            candidates = json.loads(search_output)
        except json.JSONDecodeError:
            # 坏 JSON：search_skills 正常路径不会产生，但工具被业务替换 / mock 时可能；
            # 溯源跳过即可（退化 whitelist），不阻断派发
            logger.warning("search_skills output not valid JSON, skip trace")
            return
        if not isinstance(candidates, list):
            return
        for cand in candidates:
            # 候选项必须是含 skill_id(str) + confidence(数值) 的 dict，否则跳过该项
            if not isinstance(cand, dict):
                continue
            skill_id = cand.get("skill_id")
            confidence = cand.get("confidence")
            if not isinstance(skill_id, str) or not isinstance(
                confidence, (int, float)
            ):
                continue
            # 同 skill_id 覆盖：最近一次召回的 confidence 更贴合 LLM 当前决策
            self.__tooling_owner._selection_trace[skill_id] = ("discovered", float(confidence))

    async def inject_doom_loop_notice(self, snap: dict[str, Any]) -> None:
        """doom-loop 先警：往 history 尾追一条**中性事实**（不含产品意见，R1）。

        只陈述「同工具同参数已连续 N 次、结果一致」的客观事实，是否换路交模型自判。
        尾追加（cache anchor 之后，R2 无额外 break）+ 持久化（R5）。
        """
        from taifeng.conversation.models import system_injection

        note = system_injection(
            f"Notice: tool {snap.get('tool')!r} has been called "
            f"{snap.get('consecutive')} times in a row with identical arguments, "
            f"each returning an identical result.",
            thread_id=self.__tooling_owner.thread_id, source="doom_loop")
        self.__tooling_owner.history_buffer.append(note)
        await self.__tooling_owner.store.append(note)

    def build_tool_context(self, call_id: str, iteration: int) -> ToolContext:
        """为单条 tool call 构造 ToolContext（独立 cancel.child）。

        供 ``tool_batch.dispatch_batch`` 在并发派发时按 call_id 取上下文。extras 内容
        与历史串行实现完全一致（call_skill 仍能通过 dispatcher 找到本 runner），包括
        ``iteration`` 与 ``turn_index`` 的 ``self.__tooling_owner.turn_index or iteration`` 兜底语义。
        """
        return ToolContext(
            call_id=call_id,
            cancel=self.__tooling_owner.cancel.child(f"tool:{call_id}"),
            thread_id=self.__tooling_owner.thread_id,
            extras={
                "skill_snapshot": self.__tooling_owner.snapshot,
                "visible_skills": self.__tooling_owner.snapshot.reachable_from(self.__tooling_owner.entry_skill.id),
                # search_skills 据此施加 G4a requires 过滤（与 inline 列表同源同过滤）
                "capabilities": self.__tooling_owner.capabilities,
                # 当前 turn 的原始用户任务：供 search_skills 验证门判输入适配（详情五）。
                # 召回用关键词 query（词面匹配），验证用原始任务（含输入上下文），两者不共用。
                "current_task": _latest_user_text(self.__tooling_owner.history_buffer),
                "dispatch_policy": self.__tooling_owner.dispatch_policy,
                "call_stack": self.__tooling_owner.call_stack,
                "current_skill": self.__tooling_owner.entry_skill,
                "dispatcher": self.__tooling_owner,  # 让 call_skill 找到自己
                "iteration": iteration,
                "submission_id": self.__tooling_owner.submission_id,
                "entry_skill_id": self.__tooling_owner.entry_skill.id,
                # === call_skill 走 PermissionPolicy + Hook 所需的上下文 ===
                "permission_policy": self.__tooling_owner.permission_policy,
                "hook_runner": self.__tooling_owner.hooks,
                "request_metadata": self.__tooling_owner.request_metadata,
                "turn_index": self.__tooling_owner.turn_index or iteration,
                # === run_script 工具按 language 查 executor ===
                "script_executors": self.__tooling_owner.script_executors,
                # === detached-spawn 四工具据此拿到 engine 的 spawn API ===
                "spawn_coordinator": self.__tooling_owner.spawn_coordinator,
            },
        )

    async def complete_seed_call(self, call_id: str) -> None:
        """retry_tool：补跑一个悬空 function_call(history 末尾留 fc、无 fco)→ 追加 fco。

        复用 ``dispatch_batch`` + ``_build_tool_context``(含 ``dispatcher``),故
        ``call_skill`` 子 skill 也能正确重跑。args 取 history 中该 fc 的当前 arguments
        (engine 已按 new_args 改写过)。若重跑又挂起(子 skill HITL),照常上抛 ``_BatchSuspend``。

        Raises:
            RuntimeError: history 中找不到该 call_id 的 function_call(断点不一致)。
        """
        # 找末条匹配的 function_call(即被保留的悬空 fc)
        fc = None
        for item in self.__tooling_owner.history_buffer:
            if item.kind == "function_call" and item.payload.get("call_id") == call_id:
                fc = item
        if fc is None:
            raise RuntimeError(f"seed_call_not_found: {call_id}")
        name = fc.payload["name"]
        raw = fc.payload.get("arguments") or "{}"
        # 与主派发同一解析入口:坏参数不退化为 {} 补跑,由 dispatch_batch 以
        # invalid_arguments 核销(retry_tool 重跑的是同一条 fc,规则不能更宽)
        args, args_error = parse_tool_arguments(raw)
        tool_spec = self.__tooling_owner.tool_runtime._registry.get(name)  # noqa: SLF001
        parallel_safe = bool(tool_spec.parallel_safe) if tool_spec else False
        req = ToolCallRequest(
            index=0, call_id=call_id, name=name,
            arguments=args, arguments_raw=raw, parallel_safe=parallel_safe,
            arguments_error=args_error,
        )
        outcomes = await dispatch_batch(
            [req], runtime=self.__tooling_owner.tool_runtime,
            ctx_for=lambda cid: self.__tooling_owner._build_tool_context(cid, 0),
            hooks=self.__tooling_owner.hooks, emit=self.__tooling_owner._emit,
            semaphore=asyncio.Semaphore(1),
            thread_id=self.__tooling_owner.thread_id, submission_id=self.__tooling_owner.submission_id,
            entry_skill_id=self.__tooling_owner.entry_skill.id,
            # retry 重跑仍受声明层可见集约束（原始派发已过校验；热重载移除声明则如实拒）
            visible_tools=self.__tooling_owner.entry_skill.visible_tool_names(),
        )
        outcome = outcomes[0]
        if outcome.suspend is not None:
            from taifeng.loop import turn as _turn_mod

            raise _turn_mod._BatchSuspend((outcome.suspend,))
        fco = self.__tooling_owner._settle_tool_output(call_id, outcome.result)
        self.__tooling_owner.history_buffer.append(fco)
        await self.__tooling_owner.store.append(fco)

    def settle_tool_output(self, call_id: str, result: ToolResult) -> ResponseItem:
        """把 ToolResult 结算成 function_call_output item（两处结算点共用）。

        图片附件在此完成 **durable append 之前**的 admission。违规**不上抛出批**：
        抛出会让本次派发只剩一条无 output 的悬空 ``function_call``，fc/fco 配对
        断裂 → OpenAI-compat 直接 400。转成该次调用的错误结果，既保住配对，又把
        原因如实送进模型视野让它自行纠正。

        Args:
            call_id: 与 function_call 配对的调用 id。
            result: 工具返回的 ``ToolResult``。

        Returns:
            可直接入史的 ``function_call_output`` item。
        """
        try:
            attachments = admit_tool_attachments(
                result.attachments, self.__tooling_owner.image_input_policy
            )
        except (
            AttachmentTooLargeError,
            ImageCountExceededError,
            InvalidImageError,
            UnsupportedModalityError,
        ) as exc:
            return function_call_output(
                call_id=call_id,
                output=f"tool_attachment_rejected: {exc}",
                thread_id=self.__tooling_owner.thread_id,
                is_error=True,
            )
        return function_call_output(
            call_id=call_id,
            output=result.output,
            thread_id=self.__tooling_owner.thread_id,
            is_error=result.is_error,
            attachments=attachments,
        )
