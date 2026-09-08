"""turn 上下文压缩触发：预算判定 / 策略编排 / cache 影响记账

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__compaction_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.loop.event import (
    CompactionCompleted,
    CompactionDegradationWarning,
    CompactionIntegrityRolledBack,
    CompactionStarted,
    PreCompactHookSkipped,
)
from taifeng.loop.turn_helpers import _history_orphan_call_ids

if TYPE_CHECKING:
    from taifeng.loop.turn import TurnRunner


class TurnCompaction:
    """turn 上下文压缩触发协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__compaction_owner = owner

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
        if self.__compaction_owner.compressors is None:
            return False
        tokens = self.__compaction_owner._history_token_estimate()
        if not force:
            if phase == "pre_turn" and not self.__compaction_owner.budget.is_soft_exceeded(tokens):
                return False
            if phase == "mid_turn" and not self.__compaction_owner.budget.is_soft_exceeded(tokens):
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

        ctx = CompressionContext(
            history=list(self.__compaction_owner.history_buffer),
            token_estimate=tokens,
            budget=self.__compaction_owner.budget,
            cache_anchor_index=self.__compaction_owner.cache_anchor_index,
            phase=phase,  # type: ignore[arg-type]
            available_injections=frozenset({injection}),
        )
        await self.__compaction_owner._emit(
            CompactionStarted(
                data={"phase": phase, "strategy": "auto", "token_estimate": tokens}
            )
        )
        # bypass_trigger：overflow 自愈走强制路径（绕过 should_trigger）
        if bypass_trigger:
            result = await self.__compaction_owner.compressors.force_compress(ctx, injection)
        else:
            result = await self.__compaction_owner.compressors.maybe_compress(ctx, injection)
        if result is None:
            return False
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
            # 持久化新的 compacted item
            if result.summary_item_id:
                for it in new_history:
                    if it.id == result.summary_item_id:
                        await self.__compaction_owner.store.append(it)
                        break
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

    # ---- dispatcher 接口（供 call_skill tool 调用）----
