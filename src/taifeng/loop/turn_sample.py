"""turn 采样：prompt 指纹 / 结构性中断判定 / 单次采样与 ResponseEvent 分派

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__sample_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncio
from taifeng.context.budget import estimate_history_bytes
from taifeng.conversation.models import assistant_message, function_call, reasoning
from taifeng.conversation.store import AtomicBatchMessageStore
from taifeng.llm.client import model_capabilities
from taifeng.llm.errors import InvalidResponseError, LLMError, RequestTooLargeError, UnsupportedPersistenceCapabilityError
from taifeng.llm.image_input import redact_sensitive_request_data
from taifeng.llm.providers.openai._shared import MAX_REQUEST_BYTES_METADATA_KEY
from taifeng.loop.audit_llm import commit_audited_llm_response, model_session_for_turn, record_model_cache_read
from taifeng.loop.audit_tool import audited_tool_batch
from taifeng.loop.event import AssistantReasoning, AssistantText, CacheBreakDetected, ContextBudgetExceeded, LlmRequestRecorded, ProviderRetry, RewindCheckpointRecorded, ToolBatchDispatched, ToolCallStarted
from taifeng.loop.failure_policy import DEFAULT_FAILURE_POLICY, FailureDisposition
from taifeng.loop.prompt import build_api_request
from taifeng.loop.rewind import count_turns
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch, parse_tool_arguments
from taifeng.loop.turn_helpers import _llm_failure_context, _responses_conversation_items, _responses_sample_id, _sha1_short
from taifeng.suspend.signal import SuspendSignal
from taifeng.tool.spec import ToolContext
from typing import Any

if TYPE_CHECKING:
    from taifeng.loop.turn import TurnRunner


@dataclass(frozen=True)
class _SamplePrep:
    """一次采样的**请求构建阶段**产出，供后续流消费与工具派发阶段读取。

    存在的理由：`sample_once` 曾是 532 行的单函数（全仓最长）。按「请求构建 →
    流事件分派 → 工具批派发」三段切开后，只有这 6 个局部量真正跨越第一道切点
    （其余如 policy / name 在切点后都被重新赋值），故用一个不可变载体逐字传递，
    切分前后语义完全一致。
    """

    request: Any
    tools: list[Any]
    sent_history_len: int
    structural_break_reason: str | None
    is_responses: bool
    iteration_history_len: int


class TurnSample:
    """turn 采样协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__sample_owner = owner

    def compute_prompt_fingerprint(self, tools: list[Any]) -> dict[str, str]:
        """计算 prompt 结构指纹 —— 用于归因 cache 失效的结构性原因（G-CACHE）。

        仅指纹影响 cached prefix 的三类结构：可见 skill 列表 / tool 集合 /
        system 段（entry skill id + body + 注入指令文本）。history 增长属正常
        tail，不入指纹（否则每轮都判为变更）。
        """
        snapshot_key = ",".join(
            sorted(self.__sample_owner.snapshot.reachable_from(self.__sample_owner.entry_skill.id))
        )
        tools_key = ",".join(sorted(getattr(t, "name", "") for t in tools))
        instr_text = "\x01".join(getattr(i, "text", "") for i in self.__sample_owner.instructions)
        system_src = (
            f"{self.__sample_owner.entry_skill.id}\x00{self.__sample_owner.entry_skill.body}\x00{instr_text}"
        )
        return {
            "snapshot": _sha1_short(snapshot_key),
            "tools": _sha1_short(tools_key),
            "system": _sha1_short(system_src),
        }

    def detect_structural_break_reason(
        self, current: dict[str, str]
    ) -> str | None:
        """对比上一轮指纹，判定本轮 cache 失效的结构性原因（无变更 → None）。"""
        prev = self.__sample_owner.last_prompt_fingerprint
        if prev is None:
            return None
        if current.get("snapshot") != prev.get("snapshot"):
            return "skill_snapshot_changed"
        if current.get("tools") != prev.get("tools"):
            return "tool_spec_changed"
        if current.get("system") != prev.get("system"):
            return "system_prompt_changed"
        return None

    async def _prepare_request(self, iteration: int) -> _SamplePrep:
        """采样第 1 段：回访节点登记 → 工具集与 prompt 构建 → 体积/预算预检。

        原为 ``sample_once`` 的前半段，行为逐字不变；抽出后只经 `_SamplePrep`
        向后传递 6 个真正跨段的局部量。
        """

        # turn-rewind：记本圈 iteration 回访节点(采样前的 history 长度 = re_reason 截点)。
        # 同一长度供本圈所有 dispatch 节点复用为 re_reason 切点(assistant 消息原子)。
        # 仅 root turn 入表；子 turn 节点 v1 不可寻址。
        iteration_history_len = len(self.__sample_owner.history_buffer)
        if self.__sample_owner._is_root:
            cp = self.__sample_owner.rewind_log.record_iteration(
                turn_index=count_turns(self.__sample_owner.history_buffer),
                iteration_index=iteration,
                history_len=iteration_history_len,
                cache_anchor=self.__sample_owner.cache_anchor_index,
            )
            await self.__sample_owner._emit(RewindCheckpointRecorded(data={
                "node_id": cp.node_id, "kind": cp.kind,
                "iteration_index": cp.iteration_index,
                "history_len": cp.history_len, "target_id": None,
            }))

        # 取可用 tool 集合：声明层可见集（单一真相，含 scripts 自动并入 run_script，
        # 见 SkillDefinition.visible_tool_names）∩ registry 已注册（未注册静默不可见，现状保留）
        tools = []
        for name in sorted(self.__sample_owner.entry_skill.visible_tool_names()):
            spec = self.__sample_owner.tool_runtime._registry.get(name)  # noqa: SLF001
            if spec is not None:
                tools.append(spec.to_ref())

        # T6 C3 per-turn 工具裁剪：search_skills 是全局注册的（pool.create），
        # 但只在 deferred 模式下对本 entry 暴露——inline entry（小白名单 / 显式
        # inline）不暴露搜索工具（向后兼容：原本就没有 search_skills）。判定走
        # effective_child_recall（与 system prompt 文本同一真相，保证一致）。
        if self.__sample_owner._deferred_exposure_active():
            search_spec = self.__sample_owner.tool_runtime._registry.get(  # noqa: SLF001
                "search_skills"
            )
            # M1 去重：若作者在 SKILL.md tool_names 已显式声明 search_skills，
            # 上面的可见工具循环已把它加进来，这里不能再无条件 append（否则同名
            # 工具在 per-turn 清单里出现两次）。按已加入的工具名集合去重。
            already_added = {ref.name for ref in tools}
            if search_spec is not None and "search_skills" not in already_added:
                tools.append(search_spec.to_ref())

        # G-CACHE：算本轮 prompt 结构指纹 + 归因结构性 cache 失效原因，再更新指纹
        prompt_fingerprint = self.__sample_owner._compute_prompt_fingerprint(tools)
        structural_break_reason = self.__sample_owner._detect_structural_break_reason(
            prompt_fingerprint
        )
        self.__sample_owner.last_prompt_fingerprint = prompt_fingerprint

        input_capabilities = model_capabilities(self.__sample_owner.model_client)
        is_responses = input_capabilities.protocol == "responses"
        # cache-anchor:记发出时 history 长度——流成功完成后 anchor 推进到此处的末项
        sent_history_len = len(self.__sample_owner.history_buffer)
        request = build_api_request(
            entry=self.__sample_owner.entry_skill,
            snapshot=self.__sample_owner.snapshot,
            history=self.__sample_owner.history_buffer,
            tools=tools,
            # 空字符串 → 让 provider 用其自身配置的 default_model（避免业务覆盖）
            model=self.__sample_owner.entry_skill.model or "",
            cache_anchor_index=self.__sample_owner.cache_anchor_index,
            # T3: 已 resolve 的指令；空 list 时 render 不出现 <system_instructions>
            instructions=self.__sample_owner.instructions if self.__sample_owner.instructions else None,
            # G4a: 运行时能力快照（None → 不做资格过滤）
            capabilities=self.__sample_owner.capabilities,
            # K3: page-in 的长期记忆（注入 prompt 尾部，cache-aware）
            prefetched_memory=self.__sample_owner._prefetched_memory or None,
            # reasoning-content-passback:thinking 模型 reasoning 回传开关
            reasoning_passback=self.__sample_owner.reasoning_passback,
            # T6: deferred 暴露阈值（驱动 child 列表 inline / deferred 文本）
            recall_threshold=self.__sample_owner.recall_threshold,
            # 是否有召回后端：无后端恒 inline（与工具裁剪同口径）
            has_recall_backend=self.__sample_owner.has_recall_backend,
            image_input_policy=self.__sample_owner.image_input_policy,
            model_input_capabilities=input_capabilities,
        )

        max_bytes = self.__sample_owner.budget.max_request_bytes
        if max_bytes is not None:
            request.metadata[MAX_REQUEST_BYTES_METADATA_KEY] = max_bytes

        # 审计可观测 层1:request 全文留痕。注入点选在「build 之后、发送 provider
        # 之前」——即便 provider 超时/失败,request 仍已留痕;mid-turn 压缩重建会走到
        # 新一轮 _run_sample 再次 build → 再 emit 一条,故「每次实发各一条」自然成立。
        # 默认关(enable_request_capture=False),零泄漏面 + 零行为变化。
        if self.__sample_owner.enable_request_capture:
            await self.__sample_owner._emit(
                LlmRequestRecorded(
                    data=redact_sensitive_request_data(
                        request.model_dump(mode="json")
                    )
                )
            )

        # G2b：发送前预检 —— 即便经过压缩，估算 token 仍超 hard limit 时 emit
        # 非阻塞告警（估算偏粗，不据此拒发；供业务侧主动限流 / 排查 provider 400）
        preflight_tokens = self.__sample_owner._history_token_estimate()
        if self.__sample_owner.budget.is_hard_exceeded(preflight_tokens):
            await self.__sample_owner._emit(
                ContextBudgetExceeded(
                    data={
                        "token_estimate": preflight_tokens,
                        "hard_limit": self.__sample_owner.budget.hard_limit,
                        "context_window": self.__sample_owner.budget.context_window,
                    }
                )
            )

        # G2b body-size 硬护栏：max_request_bytes 启用时，请求体超限在发送前
        # 直接抛 RequestTooLargeError（确定性字节数，无误判；比等 provider 4xx 快）。
        if max_bytes is not None:
            request_bytes = estimate_history_bytes(self.__sample_owner.history_buffer)
            if request_bytes > max_bytes:
                err = RequestTooLargeError(
                    f"request body ~{request_bytes}B 超出上限 {max_bytes}B",
                    estimated_bytes=request_bytes,
                    max_bytes=max_bytes,
                )
                # limit 类失败进 policy(resource-limit-retry-semantics):SUSPEND →
                # SYSTEM_RETRY 挂起(业务 CompactNow / 改参后 retry 可过);
                # Conservative 对确定性失败仍 TERMINAL → 原样抛,零行为变化
                policy = self.__sample_owner.failure_policy or DEFAULT_FAILURE_POLICY
                if policy.decide(_llm_failure_context(
                        err, is_root=self.__sample_owner._is_root, iteration=iteration,
                )) is FailureDisposition.SUSPEND:
                    raise SuspendSignal(self.__sample_owner._system_retry_pending(err))
                raise err
        return _SamplePrep(
            request=request,
            tools=tools,
            sent_history_len=sent_history_len,
            structural_break_reason=structural_break_reason,
            is_responses=is_responses,
            iteration_history_len=iteration_history_len,
        )

    async def sample_once(self, iteration: int) -> tuple[str, bool]:
        """一次 LLM 采样 + 工具调度，返回 (本轮 assistant text, 是否有 tool call)。"""

        # 第 1 段（请求构建 + 预检）已抽出；下面逐字还原跨段局部量，
        # 使其后约 400 行保持与切分前完全相同的文本与语义。
        prep = await self._prepare_request(iteration)
        request = prep.request
        tools = prep.tools
        sent_history_len = prep.sent_history_len
        structural_break_reason = prep.structural_break_reason
        is_responses = prep.is_responses
        iteration_history_len = prep.iteration_history_len

        sess = model_session_for_turn(self.__sample_owner, iteration)
        assistant_text = ""
        # 取消时落 partial assistant 用（ADR 0029 / R5）：本轮已流出的文本
        self.__sample_owner._streamed_text = ""
        # 累积本轮 reasoning 全文(thinking 模型;非 thinking 恒为空)
        reasoning_text = ""
        # 累积 tool calls
        tool_calls: list[dict[str, Any]] = []
        normalized_items: list[dict[str, Any]] | None = None
        responses_completed = False
        # retry 由**外层** `RetryingModelClient` 兜底：只在本次 attempt **零产出**时
        # 重发（已 yield 过内容再重发会重复投递，ADR 0037）；走到这里的 LLMError
        # 即重试已耗尽或本就不可重试。
        # 可恢复 / 等外部介入类 → 转 SYSTEM_RETRY 挂起(等业务侧 resume 重跑同次 sample);
        # 确定性失败照旧上抛硬失败。CancelledError 走 asyncio 路径,不在此 except 内。
        try:
            async with sess as s:
                async for ev in s.stream(request):
                    self.__sample_owner.cancel.raise_if_cancelled()
                    if is_responses and responses_completed:
                        raise InvalidResponseError(
                            "Responses emitted an event after completed"
                        )
                    if ev.kind == "text_delta":
                        delta = ev.data.get("text", "")
                        assistant_text += delta
                        self.__sample_owner._streamed_text = assistant_text
                        await self.__sample_owner._emit(AssistantText(data={"delta": delta}))
                    elif ev.kind == "reasoning_delta":
                        r_delta = ev.data.get("delta", "")
                        # 累积落史(reasoning-content-passback):thinking 模型要求
                        # 带 tool_calls 的 assistant 消息续传时回传 reasoning_content
                        reasoning_text += r_delta
                        await self.__sample_owner._emit(AssistantReasoning(data={"delta": r_delta}))
                    elif ev.kind == "tool_call_done":
                        if not is_responses:
                            tool_calls.append(
                                {
                                    "call_id": ev.data["call_id"],
                                    "name": ev.data["name"],
                                    "arguments": ev.data.get("arguments", "{}"),
                                    **(
                                        {"extra_content": ev.data["extra_content"]}
                                        if "extra_content" in ev.data
                                        else {}
                                    ),
                                }
                            )
                    elif ev.kind == "normalized_output":
                        if (
                            not is_responses
                            or normalized_items is not None
                            or responses_completed
                        ):
                            raise InvalidResponseError(
                                "unexpected or duplicate normalized output"
                            )
                        raw_items = ev.data.get("items")
                        if not isinstance(raw_items, list):
                            raise InvalidResponseError(
                                "normalized output items must be a list"
                            )
                        normalized_items = raw_items
                    elif ev.kind == "completed":
                        if is_responses:
                            if normalized_items is None:
                                raise InvalidResponseError(
                                    "Responses completed before normalized output"
                                )
                            responses_completed = True
                        usage_dict = ev.data.get("usage") or {}
                        self.__sample_owner._accumulate_usage(usage_dict)
                        # G3：回流的服务端 request-id（供失败 / telemetry 关联）
                        rid = ev.data.get("request_id")
                        if rid:
                            self.__sample_owner._last_request_id = rid
                        # provider 原生终止原因：原样记下，供 turn_completed 透出
                        self.__sample_owner.last_stop_reason = ev.data.get("stop_reason")
                    elif ev.kind == "prompt_cache":
                        cache_read = int(ev.data.get("cache_read_input_tokens") or 0)
                        cache_creation = int(ev.data.get("cache_creation_input_tokens") or 0)
                        expected = self.__sample_owner._next_cache_break_expected
                        expected_reason = self.__sample_owner._next_cache_break_reason
                        # 消费一次预期标记
                        self.__sample_owner._next_cache_break_expected = False
                        self.__sample_owner._next_cache_break_reason = None
                        # G-CACHE：压缩未标记预期，但结构（snapshot/tool/system）变了 →
                        # 这是可解释的预期失效，归因到具体结构原因，避免误记 unknown_drop
                        if not expected and structural_break_reason is not None:
                            expected = True
                            expected_reason = structural_break_reason
                        break_event = self.__sample_owner.cache_stats.record_turn(
                            cache_read=cache_read,
                            cache_creation=cache_creation,
                            anchor_expected=expected,
                            anchor_expected_reason=expected_reason,  # type: ignore[arg-type]
                        )
                        # 同步给 client 用于下一轮 previous_cache_read 比较
                        record_model_cache_read(self.__sample_owner.model_client, cache_read)
                        if break_event is not None:
                            await self.__sample_owner._emit(
                                CacheBreakDetected(
                                    data={
                                        "unexpected": break_event.unexpected,
                                        "token_drop": break_event.token_drop,
                                        "reason": break_event.reason,
                                        "previous": break_event.previous_cache_read_input_tokens,
                                        "current": break_event.current_cache_read_input_tokens,
                                    }
                                )
                            )
        except LLMError as e:
            # A1 reactive-compaction-recovery：provider 判「上下文超长」→ 有界自愈
            # （强制压缩一次 + 重采样一次）。本 turn 至多一次；无压缩器则不浪费重采样，
            # 直接落到下方硬失败。二次 overflow 说明压缩无效（确定性超长），快速暴露。
            from taifeng.llm.errors import ContextOverflowError

            if (
                isinstance(e, ContextOverflowError)
                and not self.__sample_owner._overflow_recovered
                and self.__sample_owner.compressors is not None
            ):
                self.__sample_owner._overflow_recovered = True
                await self.__sample_owner._emit(
                    ProviderRetry(
                        data={"reason": "context_overflow", "iteration": iteration}
                    )
                )
                # 两档自愈(reactive-compaction-recovery):第一档只动 anchor 后的 tail
                # (DO_NOT_INJECT 保 cache);未应用(无策略 / 策略失败 / 边界过窄 / 完整性
                # 回滚)且确有已缓存前缀时进第二档,允许动 head——请求已被 provider 拒绝,
                # 活下来比保 cache 重要;随之的 cache break 由压缩成功分支标 expected
                applied = await self.__sample_owner._maybe_compress(
                    phase="overflow", force=True, bypass_trigger=True
                )
                if not applied and self.__sample_owner.cache_anchor_index >= 0:
                    await self.__sample_owner._maybe_compress(
                        phase="overflow", force=True, bypass_trigger=True,
                        allow_head=True,
                    )
                # 单次重采样：递归深度恒为 1（标志已置 True，二次必走硬失败）
                return await self.__sample_owner._sample_once(iteration)
            # 取消不是失败:按 ModelClient 协议字面实现的 provider 会抛
            # llm.errors.CancelledError(LLMError 子类)——直接上抛走取消链,
            # 不咨询 policy(否则 SuspendByDefault 会把用户取消转成挂起)。
            if getattr(e, "failure_class", None) == "cancelled":
                raise
            # failure-suspension-policy:裁决权交注入的 policy(None → 保守默认,
            # 零行为变化)。policy 只回答挂还是不挂;真正的裁决人是 Resume 提交者。
            policy = self.__sample_owner.failure_policy or DEFAULT_FAILURE_POLICY
            disposition = policy.decide(_llm_failure_context(
                e, is_root=self.__sample_owner._is_root, iteration=iteration,
            ))
            if disposition is FailureDisposition.SUSPEND:
                # 裁决挂起且重试已耗尽 → 转 SYSTEM_RETRY 挂起,等业务侧 resume 重跑同次 sample。
                # 该 SuspendSignal 穿透回 run_turn 的 except SuspendSignal(Task 7 已加),落盘挂起。
                raise SuspendSignal(self.__sample_owner._system_retry_pending(e)) from e
            raise  # 确定性失败:照旧上抛硬失败(走 run_turn 宽 except → TurnFailed)

        # cache-anchor:流正常完成 → provider 已缓存本次发出的前缀,anchor 推进到发出时
        # 末项下标(含语义)。本轮产出(assistant / fc)尚未进缓存,不计入;LLMError /
        # overflow / 取消路径不经此处,不推进
        self.__sample_owner.cache_anchor_index = sent_history_len - 1

        if is_responses:
            if normalized_items is None or not responses_completed:
                raise InvalidResponseError(
                    "Responses requires one normalized output and one completed event"
                )
            sample_id = _responses_sample_id(
                thread_id=self.__sample_owner.thread_id,
                submission_id=self.__sample_owner.sample_scope_id or self.__sample_owner.submission_id,
                turn_index=self.__sample_owner.turn_index,
                iteration=iteration,
            )
            response_items, assistant_text, tool_calls = _responses_conversation_items(
                normalized_items,
                thread_id=self.__sample_owner.thread_id,
                model=self.__sample_owner.entry_skill.model or "auto",
                sample_id=sample_id,
            )
        else:
            sample_id = None
            response_items = []

        # 组装本轮 provider 顺序的会话项：reasoning → assistant →（audit 模式下）
        # function_call*。reasoning-content-passback:本轮有 reasoning 且有产出时,先落
        # reasoning item(紧邻配对 assistant message 之前,与 provider 产出顺序一致;
        # 无产出轮不落——没有可关联的 assistant 消息,回传无意义)。
        if not is_responses:
            if reasoning_text and (assistant_text or tool_calls):
                response_items.append(reasoning(reasoning_text, thread_id=self.__sample_owner.thread_id))
            # assistant message（即使为空也记下，因为 tool calls 也挂在这条消息上）
            if assistant_text or tool_calls:
                response_items.append(assistant_message(
                    assistant_text,
                    thread_id=self.__sample_owner.thread_id,
                    model=self.__sample_owner.entry_skill.model or "auto",
                ))
        # 本轮文本即将随 response_items 正常落史：取消兜底不再需要
        self.__sample_owner._streamed_text = ""

        # 第 3 段（工具批派发）已抽出；其内含全部终态 return，故直接回传。
        return await self._dispatch_tool_batch(
            iteration,
            assistant_text=assistant_text,
            is_responses=is_responses,
            iteration_history_len=iteration_history_len,
            response_items=response_items,
            sample_id=sample_id,
            sess=sess,
            tool_calls=tool_calls,
            tools=tools,
        )

    async def _dispatch_tool_batch(
        self,
        iteration: int,
        *,
        assistant_text: Any,
        is_responses: Any,
        iteration_history_len: Any,
        response_items: Any,
        sample_id: Any,
        sess: Any,
        tool_calls: Any,
        tools: Any,
    ) -> tuple[str, bool]:
        """采样第 3 段：audit 同批落库 → 工具批并发派发 → 结算与续圈判定。

        原为 ``sample_once`` 的后半段，行为逐字不变。跨段局部量由调用方按名
        传入（这 8 个是 AST 核出的真实跨界集合，不多不少）。
        """
        audit_state = self.__sample_owner.audit_state
        if audit_state is not None:
            # audit：function_call 会话项必须与最终响应同批 durable（先于任何 Tool
            # intent/effect）；随后 dispatch 阶段跳过 legacy 逐 call 的 fc 落库。
            if not is_responses:
                for tc in tool_calls:
                    response_items.append(function_call(
                        call_id=tc["call_id"],
                        name=tc["name"],
                        arguments=tc["arguments"],
                        thread_id=self.__sample_owner.thread_id,
                        extra_content=tc.get("extra_content"),
                    ))
            # 观测 session 在 checkpoint definite ack 后暴露 lineage；缺失即 attempt
            # 未收敛，fail closed 冻结（不得在无 durable 最终响应时继续产生效果）。
            checkpoint = getattr(sess, "last_attempt_checkpoint", None)
            if checkpoint is None:
                raise audit_state.coordinator.freeze(
                    RuntimeError("audited turn produced no attempt checkpoint")
                ) from None
            await commit_audited_llm_response(
                state=audit_state,
                submission_id=self.__sample_owner.submission_id,
                turn_index=self.__sample_owner.turn_index,
                iteration=iteration,
                checkpoint=checkpoint,
                items=response_items,
                cancel=self.__sample_owner.cancel,
            )
            # 仅在 durable ack + projection 推进后才把逐字一致的会话项应用到 hot history
            self.__sample_owner.history_buffer.extend(response_items)
        elif is_responses:
            if not isinstance(self.__sample_owner.store, AtomicBatchMessageStore):
                raise UnsupportedPersistenceCapabilityError(
                    "Responses requires atomic terminal output persistence"
                )
            await self.__sample_owner.store.append_atomic_batch(response_items, batch_id=sample_id)
            self.__sample_owner.history_buffer.extend(response_items)
        else:
            for item in response_items:
                self.__sample_owner.history_buffer.append(item)
                await self.__sample_owner.store.append(item)

        if not tool_calls:
            return assistant_text, False

        # === 阶段 1：按发起序 emit Started + 解析参数 + 建请求（暂不写历史）===
        requests: list[ToolCallRequest] = []
        origin_samples = {
            tc["call_id"]: tc["origin_sample_id"]
            for tc in tool_calls
            if tc.get("origin_sample_id")
        }
        for idx, tc in enumerate(tool_calls):
            call_id = tc["call_id"]
            name = tc["name"]
            arguments_str = tc["arguments"]
            await self.__sample_owner._emit(
                ToolCallStarted(
                    data={"call_id": call_id, "name": name, "arguments": arguments_str}
                )
            )
            # 解析参数(单一入口):坏 JSON / 非对象不再退化为 {} 执行,错误随请求
            # 带到派发层,由 dispatch_batch 以 invalid_arguments 核销(hook 之前)
            arguments, arguments_error = parse_tool_arguments(arguments_str)
            tool_spec = self.__sample_owner.tool_runtime._registry.get(name)  # noqa: SLF001
            parallel_safe = bool(tool_spec.parallel_safe) if tool_spec else False
            requests.append(
                ToolCallRequest(
                    index=idx,
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    arguments_raw=arguments_str,
                    parallel_safe=parallel_safe,
                    extra_content=tc.get("extra_content"),
                    arguments_error=arguments_error,
                )
            )

        # === 阶段 2：并发派发（RwLock 兜底；call_skill 跳锁真并行）===
        # max_parallel_tool_calls=1 → Semaphore(1) → 退化为严格串行（零回归）
        await self.__sample_owner._emit(
            ToolBatchDispatched(
                data={"count": len(requests), "max_parallel": self.__sample_owner.max_parallel_tool_calls}
            )
        )
        semaphore = asyncio.Semaphore(self.__sample_owner.max_parallel_tool_calls)

        # ctx 工厂闭包：捕获本轮 iteration（用于 turn_index 兜底，保持与历史等价）
        def _ctx_for(call_id: str) -> ToolContext:
            return self.__sample_owner._build_tool_context(call_id, iteration)

        # tool-whitelist：与本轮请求严格同源的名集（registry 过滤后）——可见才可执行
        visible_tools = frozenset(t.name for t in tools)

        def _run_dispatch() -> Any:
            """派发本批 tool call（audit 与 legacy 复用同一并发派发器）。"""
            return dispatch_batch(
                requests,
                runtime=self.__sample_owner.tool_runtime,
                ctx_for=_ctx_for,
                hooks=self.__sample_owner.hooks,
                emit=self.__sample_owner._emit,
                semaphore=semaphore,
                thread_id=self.__sample_owner.thread_id,
                submission_id=self.__sample_owner.submission_id,
                entry_skill_id=self.__sample_owner.entry_skill.id,
                visible_tools=visible_tools,
            )

        # audit：整批意图先于派发 durable，取消无关地把每个意图收敛为唯一终态
        # + 唯一 function_call_output 会话项；任一 UNKNOWN 记录后冻结。fc 会话项已在
        # §7.6 最终响应批中 durable，此处只补 fco。
        if self.__sample_owner.audit_state is not None:
            fco_items = await audited_tool_batch(
                state=self.__sample_owner.audit_state,
                submission_id=self.__sample_owner.submission_id,
                turn_index=self.__sample_owner.turn_index,
                iteration=iteration,
                requests=requests,
                registry=self.__sample_owner.tool_runtime._registry,  # noqa: SLF001
                run_dispatch=_run_dispatch,
                cancel=self.__sample_owner.cancel,
                finalization_timeout=(
                    self.__sample_owner.audit_state.coordinator.finalization_timeout
                ),
                origin_sample_ids=origin_samples,
            )
            self.__sample_owner.history_buffer.extend(fco_items)
            return assistant_text, True

        outcomes = await _run_dispatch()

        # === 阶段 3：按发起序以 (call, output) 配对写历史 ===
        # 配对追加复现今天的交错 transcript 结构（history_to_api_messages 1:1 保序），
        # 并发度=1 时与历史字节级一致。
        # 挂起的 outcome 只落 function_call（留 history-gap、无 output），收集 pending 上抛。
        suspended_pending: list[Any] = []
        for req, outcome in zip(requests, outcomes, strict=True):
            fc_item = function_call(
                call_id=req.call_id,
                name=req.name,
                arguments=req.arguments_raw,
                thread_id=self.__sample_owner.thread_id,
                extra_content=req.extra_content,
            )
            # audit：function_call 已在最终响应批中 durable + 应用到 hot history，
            # 此处不得重复入史 / 直写 projection store（transcript 唯一写者是
            # projector）。legacy：保持逐 call 配对写 fc。
            if self.__sample_owner.audit_state is None and not is_responses:
                self.__sample_owner.history_buffer.append(fc_item)
                await self.__sample_owner.store.append(fc_item)
            # turn-rewind：记本次派发的 dispatch 回访节点（仅 root turn）。
            # inner_history_len = fc 追加后长度 = retry_tool 切点(fc 与 fco 之间);
            # history_len 复用本圈 iteration 采样前长度 = re_reason 切点。
            if self.__sample_owner._is_root:
                dcp = self.__sample_owner.rewind_log.record_dispatch(
                    turn_index=count_turns(self.__sample_owner.history_buffer),
                    iteration_index=iteration,
                    iteration_history_len=iteration_history_len,
                    cache_anchor=self.__sample_owner.cache_anchor_index,
                    call_id=req.call_id,
                    target_id=req.name,
                    inner_history_len=len(self.__sample_owner.history_buffer),
                    args_digest=req.arguments_raw[:200],
                )
                await self.__sample_owner._emit(RewindCheckpointRecorded(data={
                    "node_id": dcp.node_id, "kind": dcp.kind,
                    "iteration_index": dcp.iteration_index,
                    "history_len": dcp.history_len, "target_id": dcp.target_id,
                }))
            if outcome.suspend is not None:
                # 挂起点：留无 output 的 function_call；不回填，收集 pending。
                # resume 时由 SuspensionRecord 重导，再补回对应 function_call_output。
                suspended_pending.append(outcome.suspend)
                continue
            fco_item = self.__sample_owner._settle_tool_output(req.call_id, outcome.result)
            origin_sample_id = origin_samples.get(req.call_id)
            if origin_sample_id:
                fco_item = fco_item.model_copy(
                    update={
                        "metadata": {
                            **fco_item.metadata,
                            "origin_llm_sample_id": origin_sample_id,
                        }
                    }
                )
            self.__sample_owner.history_buffer.append(fco_item)
            # audit：function_call_output 的 durable + projection 归 §8 Tool 收敛；
            # 此处只入 hot history，不直写 projection store（避免与 projector 竞写）。
            if self.__sample_owner.audit_state is None:
                await self.__sample_owner.store.append(fco_item)
            # turn-resource-guards：单点观察 deny/success 记账 + refunds_iteration 退还
            await self.__sample_owner._note_tool_outcome(req.name, outcome.result, req.arguments_raw)

        if suspended_pending:
            # 整批挂起 pending 上抛给 run()/run_turn 聚合落一条 SuspensionRecord。
            # _BatchSuspend 定义在 turn.py（被拆的宿主模块），惰性取避免成环
            from taifeng.loop import turn as _turn_mod

            raise _turn_mod._BatchSuspend(tuple(suspended_pending))
        return assistant_text, True


    # -----------------------------------------------------------------
    # 实现已下沉 turn_tooling.py（Wave 4）。以下为薄委托：TurnRunner 是唯一白盒
    # 寻址面，兄弟模块与测试按这些原名调用/打桩，签名逐字保留。
    # -----------------------------------------------------------------
