"""turn 上下文压缩触发：预算判定 / 策略编排 / cache 影响记账

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__compaction_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context.budget import POST_COMPACTION_TOKENS_KEY, recompaction_blocked
from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.conversation.journal.context_records import ContextCompactedV1
from taifeng.conversation.origin import lost_taint, tag_origin
from taifeng.loop.audit_compaction import (
    AuditedCompactionModel,
    commit_audited_compaction,
    superseded_item_ids,
)
from taifeng.loop.event import (
    CompactionCompleted,
    CompactionDeferred,
    CompactionDegradationWarning,
    CompactionIntegrityRolledBack,
    CompactionStarted,
    PreCompactHookSkipped,
)
from taifeng.loop.turn_helpers import _history_orphan_call_ids

if TYPE_CHECKING:
    from taifeng.context.compressor import CompressionResult
    from taifeng.conversation.origin import InputOrigin
    from taifeng.loop.turn import TurnRunner


class TurnCompaction:
    """turn 上下文压缩触发协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__compaction_owner = owner
        # 审计模式的落账计数（ADR 0094）：本 turn 已完成的压缩数、已发起的压缩 LLM 调用数
        self._audit_compactions = 0
        self._audit_llm_calls = 0
        # 本 turn 被压缩折叠掉的条目 id：回写 engine history 时据此不把它们并回去
        self.superseded_ids: set[str] = set()

    async def maybe_compress(
        self,
        *,
        phase: str,
        force: bool = False,
        bypass_trigger: bool = False,
        allow_head: bool = False,
    ) -> bool:
        """触发压缩判断。

        Args:
            phase: pre_turn / mid_turn / manual / overflow
            force: 跳过 budget 阈值预检（manual / overflow 路径为 True）
            bypass_trigger: 走 orchestrator.force_compress 绕过各策略 should_trigger
                （A1 overflow 自愈：本地估算偏低、provider 已判超长，should_trigger
                必返回 None，必须强制压缩）。
            allow_head: 把注入语义切到 BEFORE_LAST_USER_MESSAGE（允许动 anchor 之前的
                head）。overflow 自愈第二档专用；pre_turn / manual 本就允许，mid_turn
                不该传 True。

        Returns:
            本轮是否**应用**了压缩结果（history 已改写）。无压缩器 / 阈值未达 / hook
            拒绝 / 策略失败 / 完整性回滚均为 False——overflow 自愈据此决定是否进第二档。
        """
        compressors = self.__compaction_owner.compressors
        if compressors is None or not compressors.strategies:
            return False
        audit_state = self.__compaction_owner.audit_state
        if audit_state is not None and phase not in ("pre_turn", "mid_turn"):
            # 审计模式只在采样之间压缩（ADR 0094）：手动压缩没有对应的已落账 Op；溢出自愈要
            # 对同一次 LLM 调用重采样，而审计下每个 LLM operation 只允许发生一次
            return False
        # 注入了 ContextEngine 时，阈值判定看的是视图的占用（ADR 0093）
        await self.__compaction_owner._ctxload.view.refresh()  # noqa: SLF001
        tokens = self.__compaction_owner._history_token_estimate()
        # 生效预算：输出预留已按 entry skill 的 max_output_tokens 放大（ADR 0071），
        # 阈值判定与交给策略的 CompressionContext 用同一份
        budget = self.__compaction_owner.effective_budget
        if not force and phase in ("pre_turn", "mid_turn"):
            if not budget.is_soft_exceeded(tokens):
                return False
            # 压缩增量基线（ADR 0083）：上次压缩后没长多少就不再压；到硬阈值不设闸
            blocked = recompaction_blocked(
                self.__compaction_owner.history_buffer, tokens, budget
            )
            if blocked is not None:
                await self.__compaction_owner._emit(CompactionDeferred(data={
                    "phase": phase,
                    "reason": "below_growth_baseline",
                    "token_estimate": tokens,
                    "baseline_tokens": blocked.baseline_tokens,
                    "required_tokens": blocked.required_tokens,
                }))
                return False

        # === pre_compact hook ===
        # 业务侧拦截点：在 strategy 执行前可拒绝本轮压缩。
        # spec hooks/Requirement "pre_compact hook 调用点" 强约束：
        #   - deny → 跳过本轮压缩，history / cache_anchor / _next_cache_break_expected 不动
        #   - allow → 进入 CompactionStarted 既有路径
        #   - manual (force=True) 路径也走 hook，业务可拒绝管理员触发
        if self.__compaction_owner.hooks is not None:
            from taifeng.hooks.types import HookContext, PreCompactHook
            pre_decision = await self.__compaction_owner.hooks.run(
                "pre_compact",
                PreCompactHook(
                    phase=phase,  # type: ignore[arg-type]
                    token_estimate=tokens,
                    history_length=len(self.__compaction_owner.history_buffer),
                ),
                HookContext(
                    thread_id=self.__compaction_owner.thread_id,
                    submission_id=self.__compaction_owner.submission_id,
                    entry_skill_id=self.__compaction_owner.entry_skill.id,
                ),
            )
            if not pre_decision.allow:
                await self.__compaction_owner._emit(PreCompactHookSkipped(data={
                    "phase": phase,
                    "reason": pre_decision.reason or "",
                    "token_estimate": tokens,
                    "history_length": len(self.__compaction_owner.history_buffer),
                }))
                return False

        # 注入语义：pre_turn / manual 允许动 head；mid_turn / overflow 默认只动 anchor 后
        # 的 tail（DO_NOT_INJECT 保 cache）；overflow 第二档以 allow_head=True 切到可动 head
        injection = (
            InitialContextInjection.BEFORE_LAST_USER_MESSAGE
            if allow_head or phase in ("pre_turn", "manual")
            else InitialContextInjection.DO_NOT_INJECT
        )

        audited_model = self._audited_model()
        ctx = CompressionContext(
            history=list(self.__compaction_owner.history_buffer),
            token_estimate=tokens,
            budget=budget,
            cache_anchor_index=self.__compaction_owner.cache_anchor_index,
            phase=phase,  # type: ignore[arg-type]
            available_injections=frozenset({injection}),
            model_session=None if audited_model is None else audited_model.session,
        )
        await self.__compaction_owner._emit(
            CompactionStarted(
                data={"phase": phase, "strategy": "auto", "token_estimate": tokens}
            )
        )
        # bypass_trigger：overflow 自愈走强制路径（绕过 should_trigger）
        if bypass_trigger:
            result = await compressors.force_compress(ctx, injection)
        else:
            result = await compressors.maybe_compress(ctx, injection)
        llm_record_ids: tuple[str, ...] = ()
        if audited_model is not None:
            # 压缩发起的 LLM 调用无论压缩成败都是事实：先补齐它们的最终响应记录
            llm_record_ids = await audited_model.commit_responses()
            self._audit_llm_calls = audited_model.next_call_ordinal
        if result is None:
            return False
        if result.success and audited_model is not None:
            return await self._apply_audited(ctx, result, llm_record_ids)
        # G1b：压缩成功，但若产物相对原 history 引入了新的 tool 配对孤儿 → 回滚
        # （不应用），保留原 history。保留历史优于把损坏会话喂给 provider。
        if result.success:
            new_orphans = _history_orphan_call_ids(
                result.new_history
            ) - _history_orphan_call_ids(ctx.history)
            if new_orphans:
                await self.__compaction_owner._emit(
                    CompactionIntegrityRolledBack(
                        data={"issues": sorted(new_orphans), "phase": phase}
                    )
                )
                await self.__compaction_owner._emit(
                    CompactionCompleted(
                        data={
                            "success": False,
                            "cache_invalidated": False,
                            "removed_count": 0,
                            "reason": "integrity_rolled_back",
                            "detail": {},
                        }
                    )
                )
                return False
        # 应用压缩结果
        if result.success:
            # K3 on_pre_evict（swap-out 抢救）：把将被换出的 items 交给 memory
            # 持久化，取回「必须留在上下文」的 digest，作为 system_injection 折进
            # 保留段（紧随 summary）。digest 在 anchor 之后，不影响 cache anchor。
            new_history = await self.__compaction_owner._apply_pre_evict_salvage(
                ctx.history, result.new_history, result.summary_item_id
            )
            # postcompact re-injection：pinned 状态钉回 tail（K3 salvage 之后、
            # 写回 buffer 之前——任何成功压缩路径含 overflow 自愈均覆盖）。
            new_history = await self.__compaction_owner._reinject_pinned_state(new_history, phase)
            self.__compaction_owner.history_buffer[:] = new_history
            self.__compaction_owner.cache_anchor_index = result.anchor_preserved_until
            # token-accounting-calibration：压缩改写了实测锚点之前的前缀（破 cache 或
            # 保留点退到锚点之前）→ 锚点失效，只保留 overhead 继续修正粗估
            cal = self.__compaction_owner.token_calibration
            if cal is not None and cal.anchor_valid and (
                result.cache_invalidated
                or result.anchor_preserved_until < cal.anchor_len - 1
            ):
                self.__compaction_owner.token_calibration = cal.invalidated()
            # 持久化新的 compacted item，并在其上记下压缩刚结束时的估算（增量基线，
            # ADR 0083）。基线随条目落 transcript，冷加载后仍可读；是否设闸由预算决定
            if result.summary_item_id:
                await self._persist_summary_with_baseline(
                    result.summary_item_id, lost_taint(ctx.history, new_history)
                )
            # 如果压缩破坏了 cache，标记下一次 LLM 调用的 break 为预期内
            if result.cache_invalidated:
                self.__compaction_owner._next_cache_break_expected = True
                # overflow 第二档蓄意动 head → compaction_overflow（预期内，不计 unexpected）
                self.__compaction_owner._next_cache_break_reason = (
                    "compaction_pre_turn"
                    if phase == "pre_turn"
                    else "compaction_manual"
                    if phase == "manual"
                    else "compaction_overflow"
                    if phase == "overflow"
                    else "compaction_mid_turn_anchor_lost"
                )
            # G1c：成功压缩计数；达阈值后每次压缩 emit 降级告警
            self.__compaction_owner.compaction_count += 1
            if self.__compaction_owner.compaction_count >= self.__compaction_owner.compaction_degradation_threshold:
                await self.__compaction_owner._emit(
                    CompactionDegradationWarning(
                        data={
                            "compaction_count": self.__compaction_owner.compaction_count,
                            "threshold": self.__compaction_owner.compaction_degradation_threshold,
                        }
                    )
                )
        await self.__compaction_owner._emit(
            CompactionCompleted(
                data={
                    "success": result.success,
                    "cache_invalidated": result.cache_invalidated,
                    "removed_count": result.removed_item_count,
                    "reason": result.reason,
                    # 策略自报明细（如 surgical_trim 的 deduped/soft/hard 计数）；
                    # 既有策略为空 dict —— R3 机读透传，不编码进 reason
                    "detail": result.detail,
                }
            )
        )
        return result.success

    def _audited_model(self) -> AuditedCompactionModel | None:
        """审计模式下压缩用的受审计 LLM 会话来源；非审计模式为 None。"""
        owner = self.__compaction_owner
        if owner.audit_state is None:
            return None
        return AuditedCompactionModel(
            state=owner.audit_state,
            model_client=owner.model_client,
            submission_id=owner.submission_id,
            turn_index=owner.turn_index,
            first_call_ordinal=self._audit_llm_calls,
            cancel=owner.cancel,
        )

    async def _apply_audited(
        self,
        ctx: CompressionContext,
        result: CompressionResult,
        llm_record_ids: tuple[str, ...],
    ) -> bool:
        """审计模式下应用一次成功的压缩：先落账，ack 后才改 hot history（ADR 0094）。"""
        owner = self.__compaction_owner
        assert owner.audit_state is not None
        summary = next(
            (item for item in result.new_history if item.id == result.summary_item_id), None
        )
        if summary is None or summary.kind != "compacted":
            # 声明了折叠式却没给出摘要条目：不应用，history 原样保留
            await owner._emit(CompactionCompleted(data={
                "success": False, "cache_invalidated": False, "removed_count": 0,
                "reason": "audit_requires_summary_item", "detail": {},
            }))
            return False
        new_history = list(result.new_history)
        tokens_after = owner._ctxload.estimate_items(new_history)  # noqa: SLF001
        stamped = tag_origin(
            summary.model_copy(update={
                "metadata": {**summary.metadata, POST_COMPACTION_TOKENS_KEY: tokens_after},
            }),
            lost_taint(ctx.history, new_history),
        )
        new_history[new_history.index(summary)] = stamped
        start, end = summary.payload["replaced_range"]
        await commit_audited_compaction(
            state=owner.audit_state,
            submission_id=owner.submission_id,
            turn_index=owner.turn_index,
            payload=ContextCompactedV1(
                phase=ctx.phase,  # type: ignore[arg-type]
                strategy=result.strategy,
                ordinal=self._audit_compactions,
                tokens_before=ctx.token_estimate,
                tokens_after=tokens_after,
                replaced_range=(start, end),
                removed_item_count=result.removed_item_count,
                summary_item_id=stamped.id,
                cache_invalidated=result.cache_invalidated,
                anchor_preserved_until=result.anchor_preserved_until,
                quality_warnings=result.quality_warnings,
                detail=dict(result.detail),
                llm_request_record_ids=llm_record_ids,
            ),
            summary_item=stamped,
            cancel=owner.cancel,
        )
        self._audit_compactions += 1
        self.superseded_ids |= superseded_item_ids(ctx.history, new_history)
        owner.history_buffer[:] = new_history
        owner.cache_anchor_index = result.anchor_preserved_until
        calibration = owner.token_calibration
        if calibration is not None and calibration.anchor_valid and (
            result.cache_invalidated
            or result.anchor_preserved_until < calibration.anchor_len - 1
        ):
            owner.token_calibration = calibration.invalidated()
        if result.cache_invalidated:
            owner._next_cache_break_expected = True  # noqa: SLF001
            owner._next_cache_break_reason = (  # noqa: SLF001
                "compaction_pre_turn" if ctx.phase == "pre_turn"
                else "compaction_mid_turn_anchor_lost"
            )
        owner.compaction_count += 1
        await owner._emit(CompactionCompleted(data={
            "success": True,
            "cache_invalidated": result.cache_invalidated,
            "removed_count": result.removed_item_count,
            "reason": result.reason,
            "detail": result.detail,
        }))
        return True

    async def _persist_summary_with_baseline(
        self, summary_item_id: str, inherited: InputOrigin | None,
    ) -> None:
        """给压缩条目记上基线与继承的来源标记并落 store；history 里的同一条目同步替换。

        ``inherited``：被折叠的内容里有不可信条目时，摘要带派生标记——上下文不因压缩而
        变干净（input-origin，ADR 0085）。
        """
        owner = self.__compaction_owner
        baseline = owner._history_token_estimate()
        for index, item in enumerate(owner.history_buffer):
            if item.id != summary_item_id:
                continue
            stamped = tag_origin(
                item.model_copy(update={
                    "metadata": {**item.metadata, POST_COMPACTION_TOKENS_KEY: baseline},
                }),
                inherited,
            )
            owner.history_buffer[index] = stamped
            await owner.store.append(stamped)
            return

    # ---- dispatcher 接口（供 call_skill tool 调用）----
