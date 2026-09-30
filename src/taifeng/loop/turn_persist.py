"""turn 落盘与守卫：挂起记录持久化 / 会话上限判定 / usage 累加 / 半程 assistant 落史 / 注入排空

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。按 `turn-module-structure`
契约落为**协作者类**：自身无状态，运行态仍由 TurnRunner 唯一持有。

**兄弟调用一律经 ``self.__persist_owner._x(...)`` 回弹**——TurnRunner 是唯一白盒寻址面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.conversation.models import assistant_message
from taifeng.llm.types import TokenUsage, add_usage
from taifeng.loop.injection import injection_event

if TYPE_CHECKING:
    from taifeng.loop.turn import TurnRunner


class TurnPersist:
    """turn 落盘与守卫协作器（持 TurnRunner 引用，自身无状态）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 turn 运行态与共享依赖。
        """
        self.__persist_owner = owner
        # 审计模式（ADR 0097）：本 turn 已落账的挂起数；停下等人的调用 → 它们已落账的意图
        self._audit_suspensions = 0
        self.awaited_intents: dict[str, str] = {}

    async def persist_suspension(self, pending: tuple[Any, ...]) -> Any:
        """把本次挂起落 store 并返回 SuspensionRecord（R5 跨进程 resume 的真相）。

        record_id / created_at 由注入工厂提供（R1：src 内不取系统时钟 / 随机）；
        pending 为本次挂起的全部 PendingRequest。

        Args:
            pending: 本 turn 全部挂起点的 PendingRequest（不可变 tuple）。

        Returns:
            落盘后的 SuspensionRecord（同时已追加进 history_buffer 与 store）。
        """
        from taifeng.suspend.record import SuspensionRecord

        record = SuspensionRecord(
            record_id=self.__persist_owner._suspend_id_factory(),
            thread_id=self.__persist_owner.thread_id,
            submission_id=self.__persist_owner.submission_id,
            turn_index=self.__persist_owner._current_iteration,
            pending=pending,
            created_at=self.__persist_owner._now_factory(),
        )
        owner = self.__persist_owner
        if owner.audit_state is not None:
            # 审计模式：挂起记录与断点条目同批落账，ack 后才进 hot history（ADR 0097）
            from taifeng.loop.audit_suspension import commit_turn_suspended

            item = await commit_turn_suspended(
                state=owner.audit_state, submission_id=owner.submission_id,
                turn_index=owner.turn_index, ordinal=self._audit_suspensions,
                record=record, awaited_intents=self.awaited_intents, cancel=owner.cancel,
            )
            self._audit_suspensions += 1
            owner.history_buffer.append(item)
            return record
        item = record.to_item()
        self.__persist_owner.history_buffer.append(item)
        await self.__persist_owner.store.append(item)
        return record

    def session_limit_exceeded(self) -> bool:
        """K2：会话累计 token（基线 + 本 turn 已用）是否达到上限。

        None → 不强制（默认）。total_tokens 取累计；上限值由业务注入。
        """
        if self.__persist_owner.max_session_tokens is None:
            return False
        return self.session_tokens_now() >= self.__persist_owner.max_session_tokens

    def session_tokens_now(self) -> int:
        """会话累计 token（K2 口径）。

        注入了共享计量器 → 读其实时总量（已含本 turn 与整棵树所有子 turn 的采样，
        采样即入账）；否则退回「启动基线 + 本 turn 已用」的旧口径。
        """
        owner = self.__persist_owner
        if owner.usage_meter is not None:
            return owner.usage_meter.total_tokens
        return owner.session_tokens_used + owner.total_usage.total_tokens

    def accumulate_usage(self, usage_dict: dict[str, Any]) -> None:
        """入账一次采样的 usage：本 turn 累计、子树累计、会话共享计量器三处同步。"""
        u = TokenUsage(**usage_dict) if usage_dict else TokenUsage()
        owner = self.__persist_owner
        owner.total_usage = add_usage(owner.total_usage, u)
        owner.subtree_usage = add_usage(owner.subtree_usage, u)
        # usage-tree-accounting：实时入会话账并归因（而非等 turn 收尾由 engine 加根 turn）
        if owner.usage_meter is not None:
            owner.usage_meter.add(u, thread_id=owner.thread_id, skill_id=owner.entry_skill.id)

    async def persist_partial_assistant(self) -> None:
        """取消时把已流式输出、尚未落史的 assistant 文本以 truncated 标记落史（R5）。

        UI 已经把这段文本展示给了用户，transcript 若不记 → 冷 resume 后"这轮从没
        说过话"，与用户所见不一致（codex interrupt 落 partial message 同语义）。
        采样正常结束时 `_streamed_text` 已随 assistant_message 落史并清空，这里为空即返回。
        """
        text = self.__persist_owner._streamed_text
        if not text:
            return
        self.__persist_owner._streamed_text = ""
        item = assistant_message(
            text,
            thread_id=self.__persist_owner.thread_id,
            model=self.__persist_owner.entry_skill.model or "auto",
        ).model_copy(update={"metadata": {"truncated": True}})
        self.__persist_owner.history_buffer.append(item)
        await self.__persist_owner.store.append(item)

    async def drain_pending_input(self, *, residual: bool = False) -> None:
        """B1：把 pending_input 队列并入 history。

        迭代边界调用（``residual=False``）：取出全部 pending（保留提交顺序），逐条追加
        history_buffer + store.append + emit 对应注入事件（user_message →
        ``user_input_injected``、system_injection → ``system_message_injected``，
        delivered:true）；turn 已取消则不并入。

        turn 退出路径调用（``residual=True``，ADR 0029）：不看取消位，把残留全部落史，
        事件 delivered:false + reason="turn_ended"——文本未进入本 turn 的 prompt 但没丢。

        副作用：history_buffer / store 追加；pending_input 清空；emit 事件。
        """
        if not self.__persist_owner.pending_input:
            return
        if not residual and self.__persist_owner.cancel.is_cancelled:
            return
        # 同 event loop 协作式调度：取出 + 清空在无 await 的同步段完成，避免与
        # engine 主循环 append 竞态（无需锁）。
        drained = list(self.__persist_owner.pending_input)
        state = self.__persist_owner.audit_state
        if state is not None:
            if not state.coordinator.effect_gate_open:
                # Session 已不再接受写入：消息留在队列里，接管后从 Journal 回到队列
                return
            self.__persist_owner.pending_input.clear()
            # 审计：消息由本 thread 的写者写进对话（ADR 0100）
            from taifeng.loop.audit_peer import deliver_peer_items

            await deliver_peer_items(
                state, drained, submission_id=self.__persist_owner.submission_id,
                turn_index=self.__persist_owner.turn_index,
            )
        else:
            self.__persist_owner.pending_input.clear()
        for item in drained:
            self.__persist_owner.history_buffer.append(item)
            if state is None:
                await self.__persist_owner.store.append(item)
            await self.__persist_owner._emit(
                injection_event(
                    item, self.__persist_owner.submission_id,
                    delivered=not residual,
                    reason="turn_ended" if residual else None,
                )
            )

    # -----------------------------------------------------------------
    # 实现已下沉 turn_compaction.py（Wave 4）。以下为薄委托：TurnRunner 是唯一白盒
    # 寻址面，兄弟模块与测试按这些原名调用/打桩，签名逐字保留。
    # -----------------------------------------------------------------
