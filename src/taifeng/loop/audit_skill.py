"""audit-required Turn 的同步 call_skill 子谱系落账。

契约（spec「Synchronous call_skill records complete child lineage」）：同步
call_skill **复用根 Session 的 coordinator 与 lease**；在外层 Tool 意图之后先 durable
`skill_selected`（完整 definition/body 快照），随后按状态落：
- 配额拒绝：`skill_dispatch_finished(status=rejected, started_record_id=None)`，
  不创建 child thread / seed；
- 接受：原子 `skill_dispatch_started` + `thread_created` + `thread_bound` + child-seed
  会话项 → 子 runner 从 durable seed history 运行（child audit_state 共享根 coordinator、
  同一 projector 的 child thread）→ 原子 `skill_dispatch_finished` + `thread_terminal`
  + `skill_outcome` 会话项，先于外层 Tool outcome。

沿用 audit_llm / audit_tool 的「durable → projection → hot history」三段式；child
audit_state 让子 turn 的 LLM/Tool 效果递归走同一审计路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng.conversation.journal.canonical import canonical_hash
from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    SkillDispatchFinishedV1,
    SkillDispatchStartedV1,
    SkillSelectedV1,
    SkillStatus,
    StableErrorV1,
    ThreadBoundV1,
    ThreadCreatedV1,
    ThreadTerminalV1,
    conversation_item_record,
)
from taifeng.conversation.models import skill_outcome_item, user_message
from taifeng.loop.audit_bootstrap import AuditedSessionState

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.definition import SkillDefinition


def _stable_definition(target: SkillDefinition) -> dict[str, Any]:
    """构造稳定 definition 快照（body 以 hash 记，frozenset 归一为有序 list）。"""
    return {
        "id": target.id,
        "name": target.name,
        "description": target.description,
        "version": target.version,
        "type": target.type,
        "entry": target.entry,
        "child_skills": sorted(target.child_skills),
        "tool_names": sorted(target.tool_names),
        "max_call_depth": target.max_call_depth,
        "model": target.model,
        "body_sha256": canonical_hash(target.body),
    }


def _child_thread_id(skill_operation_id: str) -> str:
    """从 skill operation 确定性派生 delimiter-free 子 thread id（可 resume 复现）。"""
    return f"thrc{canonical_hash(skill_operation_id)[:32]}"


@dataclass(frozen=True, slots=True)
class ChildSkillContext:
    """一次已 started 的子 skill 派生上下文，供 finish 收敛复用。"""

    child_state: AuditedSessionState
    seed_item: ResponseItem
    skill_operation_id: str
    started_record_id: str
    child_thread_id: str
    call_id: str


class AuditedSkillDispatch:
    """父 runner 视角的同步 call_skill 子谱系落账器。"""

    def __init__(
        self,
        *,
        state: AuditedSessionState,
        parent_turn_index: int,
        parent_call_id: str,
        submission_id: str,
        cancel: CancellationToken,
    ) -> None:
        """冻结 skill operation 的 identity 派生器（挂在外层 Tool operation 之下）。"""
        self._state = state
        self._coordinator = state.coordinator
        self._cancel = cancel
        self._submission_id = submission_id
        self._identities = JournalIdentities(
            self._coordinator.session_id,
            state.thread_id,
            submission_id,
        )
        self._turn_id = self._identities.turn(parent_turn_index)
        self._parent_call_id = parent_call_id
        # skill 谱系挂在外层 call_skill Tool operation 之下
        self._tool_operation_id = self._identities.tool(
            self._turn_id, parent_call_id
        )
        self._factory = JournalRecordFactory(
            session_id=self._coordinator.session_id,
            actor=ActorRef(kind="system", source="skill"),
            identities=self._identities,
        )
        self._selected_record_id: str | None = None
        self._skill_operation_id: str | None = None

    async def commit_selected(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
    ) -> str:
        """在外层 Tool 意图之后 durable skill_selected；返回 skill_operation_id。"""
        await self._coordinator.ensure_effect_allowed()
        skill_operation_id = self._identities.skill(
            self._tool_operation_id, target.id
        )
        full_definition = _stable_definition(target)
        record = self._factory.build(
            operation_id=skill_operation_id,
            record_type="skill_selected",
            payload=SkillSelectedV1(
                call_id=self._parent_call_id,
                skill_id=target.id,
                version=target.version,
                definition_hash=canonical_hash(full_definition),
                body_hash=canonical_hash(target.body),
                full_definition=full_definition,
                arguments=dict(arguments),
                selection_origin="call_skill",
            ),
            submission_id=self._submission_id,
            thread_id=self._state.thread_id,
            turn_id=self._turn_id,
            causation_id=self._tool_operation_id + ":tool_intent_committed:none:0",
        )
        await self._coordinator.append(record, cancel=self._cancel)
        self._selected_record_id = record.record_id
        self._skill_operation_id = skill_operation_id
        return skill_operation_id

    async def reject(self, *, reason: str) -> None:
        """配额拒绝：durable skill_dispatch_finished(rejected)，不创建 child。"""
        assert self._skill_operation_id is not None
        assert self._selected_record_id is not None
        record = self._factory.build(
            operation_id=self._skill_operation_id,
            record_type="skill_dispatch_finished",
            payload=SkillDispatchFinishedV1(
                started_record_id=None,
                call_id=self._parent_call_id,
                child_thread_id=None,
                status=SkillStatus.REJECTED,
                end_reason=reason,
            ),
            submission_id=self._submission_id,
            thread_id=self._state.thread_id,
            turn_id=self._turn_id,
            causation_id=self._selected_record_id,
        )
        await self._coordinator.append(record)

    async def start_child(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        call_stack_path: tuple[str, ...],
    ) -> ChildSkillContext:
        """原子 started+thread_created+thread_bound+child_seed，并 bootstrap child projection。"""
        assert self._skill_operation_id is not None
        assert self._selected_record_id is not None
        op = self._skill_operation_id
        child_thread_id = _child_thread_id(op)
        # 先 bootstrap child thread 的 transcript projection target（seed 投影前置条件）
        await self._state.projector.bootstrap_thread(
            thread_id=child_thread_id,
            cwd=None,
            entry_skill_id=target.id,
            source=f"skill:{op}",
            extra={
                "audit_required": True,
                "journal_session_id": self._coordinator.session_id,
                "journal_schema_version": 1,
                "parent_thread_id": self._state.thread_id,
            },
        )
        import json

        seed_item = user_message(
            json.dumps(arguments, ensure_ascii=False),
            thread_id=child_thread_id,
        )
        started = self._factory.build(
            operation_id=op,
            record_type="skill_dispatch_started",
            payload=SkillDispatchStartedV1(
                selected_record_id=self._selected_record_id,
                call_id=self._parent_call_id,
                parent_call_id=None,
                child_thread_id=child_thread_id,
                call_stack=call_stack_path,
                arguments=dict(arguments),
            ),
            submission_id=self._submission_id,
            thread_id=self._state.thread_id,
            turn_id=self._turn_id,
            causation_id=self._selected_record_id,
        )
        thread_created = self._factory.build(
            operation_id=op,
            record_type="thread_created",
            payload=ThreadCreatedV1(
                entry_skill_id=target.id,
                source=f"skill:{op}",
                tags=(),
                extra={"parent_thread_id": self._state.thread_id},
                parent_thread_id=self._state.thread_id,
                call_id=self._parent_call_id,
            ),
            submission_id=self._submission_id,
            thread_id=child_thread_id,
            turn_id=self._turn_id,
            causation_id=started.record_id,
        )
        thread_bound = self._factory.build(
            operation_id=op,
            record_type="thread_bound",
            payload=ThreadBoundV1(
                session_id=self._coordinator.session_id,
                thread_id=child_thread_id,
                call_id=self._parent_call_id,
            ),
            submission_id=self._submission_id,
            thread_id=child_thread_id,
            turn_id=self._turn_id,
            causation_id=thread_created.record_id,
        )
        seed_record = conversation_item_record(
            self._factory,
            operation_id=op,
            item=seed_item,
            source_record_id=thread_bound.record_id,
            ordinal=0,
            submission_id=self._submission_id,
            turn_id=self._turn_id,
        )
        batch = (started, thread_created, thread_bound, seed_record)
        ack = await self._coordinator.append_batch(batch, cancel=self._cancel)
        # ack 后投影 child seed（child thread），推进 child projection
        envelopes = await self._coordinator.load_acknowledged(ack, batch)
        seed_envelopes = tuple(
            e for e in envelopes if e.record_type == "conversation_item"
        )
        try:
            result = await self._state.projector.project(seed_envelopes, ack)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            raise self._coordinator.freeze(error) from None
        self._coordinator.update_projection(result)
        child_state = AuditedSessionState(
            thread_id=child_thread_id,
            coordinator=self._coordinator,
            projector=self._state.projector,
            max_attachment_bytes=self._state.max_attachment_bytes,
            max_total_attachment_bytes=self._state.max_total_attachment_bytes,
        )
        return ChildSkillContext(
            child_state=child_state,
            seed_item=seed_item,
            skill_operation_id=op,
            started_record_id=started.record_id,
            child_thread_id=child_thread_id,
            call_id=self._parent_call_id,
        )

    async def finish_child(
        self,
        *,
        child: ChildSkillContext,
        status: SkillStatus,
        end_reason: str,
        final_text: str,
        outcome_payload: dict[str, Any],
        error: StableErrorV1 | None,
    ) -> ResponseItem:
        """原子 finished+thread_terminal+skill_outcome，先于外层 Tool outcome。"""
        op = child.skill_operation_id
        finished = self._factory.build(
            operation_id=op,
            record_type="skill_dispatch_finished",
            payload=SkillDispatchFinishedV1(
                started_record_id=child.started_record_id,
                call_id=child.call_id,
                child_thread_id=child.child_thread_id,
                status=status,
                end_reason=end_reason,
                final_text=final_text,
                stable_error=error,
            ),
            submission_id=self._submission_id,
            thread_id=self._state.thread_id,
            turn_id=self._turn_id,
            causation_id=child.started_record_id,
        )
        thread_terminal = self._factory.build(
            operation_id=op,
            record_type="thread_terminal",
            payload=ThreadTerminalV1(
                status=status.value,
                end_reason=end_reason,
                stable_error=error,
            ),
            submission_id=self._submission_id,
            thread_id=child.child_thread_id,
            turn_id=self._turn_id,
            causation_id=finished.record_id,
        )
        # skill_outcome 是子 thread 的战绩会话项（与 legacy 一致，进子 transcript）
        outcome_item = skill_outcome_item(
            outcome_payload,
            thread_id=child.child_thread_id,
        )
        outcome_record = conversation_item_record(
            self._factory,
            operation_id=op,
            item=outcome_item,
            source_record_id=finished.record_id,
            ordinal=1,
            submission_id=self._submission_id,
            turn_id=self._turn_id,
        )
        batch = (finished, thread_terminal, outcome_record)
        ack = await self._coordinator.append_batch(batch)
        envelopes = await self._coordinator.load_acknowledged(ack, batch)
        outcome_envelopes = tuple(
            e for e in envelopes if e.record_type == "conversation_item"
        )
        try:
            result = await self._state.projector.project(outcome_envelopes, ack)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error_:
            raise self._coordinator.freeze(error_) from None
        self._coordinator.update_projection(result)
        return outcome_item


__all__ = [
    "AuditedSkillDispatch",
    "ChildSkillContext",
]
