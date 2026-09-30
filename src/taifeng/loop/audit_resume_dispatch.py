"""审计 Session resume 时按 skill 派发树自底向上收敛（ADR 0076）。

同步 ``call_skill`` 的子 skill 在独立子 thread 上运行。进程死在子 skill 执行途中时，Journal 里
留下一条链：父 thread 悬空的 ``call_skill`` 意图 → 未结算的 ``skill_selected`` → 子 thread 上
待收敛的工具调用（子 skill 还可以再派发，链可以更深）。此前只要出现未结算的 skill 派发，resume
整体 fail closed。

本模块把这条链自底向上收敛，结论同属一个恢复 batch：

1. 子 thread 上的工具调用按既有规则结算（结果未知 → ``audit_resume_tools``；从未登记意图 →
   ``audit_resume_undispatched``）；
2. 被中断的派发落终态：``skill_dispatch_finished(cancelled, process_recovery)`` +
   子 thread 的 ``thread_terminal``；不记战绩（执行没有跑完，不是 skill 的成败）；
3. 父 thread 的 ``call_skill`` 意图落 ``tool_recovery_committed(basis=dispatch)`` 并补一条结果，
   逐条列出子 skill 内各调用的处置结论，模型据此判断重新派发会不会重复副作用。

``call_skill`` 意图的结局完全由派发谱系决定，不看它的 ``effect_kind`` 声明：

| 谱系形态 | 结论 | 补写给模型的结果 |
| --- | --- | --- |
| 没有 ``skill_selected`` | dispatch / not_started | 「未执行，可安全重发」 |
| 有 selected、没有 started 也没有 finished | dispatch / not_started（并补 finished(rejected)） | 同上 |
| 有 finished（子 skill 已 durable 结束） | dispatch / completed | 已落账的子 skill 结果 |
| 有 started、没有 finished | dispatch / interrupted | 中断说明 + 子调用处置清单 |

恢复从不执行工具、不续跑子 skill。任一调用仍需人裁决时整批不写（全有或全无）。

参照：ADR 0025 恢复语义；差异：把「skill 派发」这一层的未结算 effect 拆解为其内部已有规则可
处理的工具调用，而不是为派发本身引入新的人裁决入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    SkillDispatchFinishedV1,
    SkillDispatchStartedV1,
    SkillSelectedV1,
    SkillStatus,
    StableErrorV1,
    ThreadTerminalV1,
)
from taifeng.loop.audit_resume_llm import abandoned_request_record, unsettled_llm_requests
from taifeng.loop.audit_resume_scan import find_undispatched_calls
from taifeng.loop.audit_resume_spawn import (
    InterruptedSpawn,
    interrupted_spawn_records,
    interrupted_spawns,
)
from taifeng.loop.audit_resume_submissions import (
    application_recovery_records,
    unapplied_user_messages,
)
from taifeng.loop.audit_resume_tools import (
    ToolCallDecision,
    UnresolvedToolCall,
    decide_tool_call,
    needs_operator_without_lock,
    recovery_records,
    split_unsettled,
)
from taifeng.loop.audit_resume_undispatched import (
    UndispatchedCall,
    needs_operator,
    plan_undispatched_recovery,
    undispatched_call,
)
from taifeng.loop.tool_recovery import (
    DISPATCH_NOT_STARTED_TEXT,
    RecoveredCall,
    dispatch_interrupted_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord
    from taifeng.loop.audit_resume_resolution import AuditToolOutcomeResolver
    from taifeng.tool.registry import ToolRegistry

CALL_SKILL_TOOL_NAME = "call_skill"
"""同步 skill 派发工具名；其意图按派发谱系结算。"""

RECOVERY_END_REASON = "process_recovery"
"""被中断的派发 / 子 thread 的终态原因。"""

RECOVERY_BEFORE_START_END_REASON = "process_recovery_before_start"
"""已选定、从未启动的派发的终态原因。"""

_SYSTEM_ACTOR = ActorRef(kind="system", source="recovery")
_INTERRUPTED_ERROR = StableErrorV1(
    code="skill_dispatch_interrupted",
    class_name="ProcessRecovery",
    failure_class="skill_error",
    retryable=True,
)


@dataclass(frozen=True, slots=True)
class DispatchCall:
    """一个悬空的 ``call_skill`` 调用及其已 durable 的派发谱系。"""

    call: UnresolvedToolCall
    selected: JournalEnvelope | None
    started: JournalEnvelope | None
    finished: JournalEnvelope | None

    @property
    def child_thread_id(self) -> str | None:
        """已启动派发的子 thread id；未启动为 None。"""
        if self.started is None:
            return None
        return SkillDispatchStartedV1.model_validate(self.started.payload).child_thread_id

    @property
    def interrupted(self) -> bool:
        """子 skill 已启动但没有 durable 终态。"""
        return self.started is not None and self.finished is None


@dataclass(frozen=True, slots=True)
class RecoveryScope:
    """一次 strict 读取里全部待收敛项的分拣结果。

    Attributes:
        threads: 可收敛的 thread：root + 被中断派发的子 thread（逐层传递）。
        tool_calls: 结果未知的普通工具调用。
        dispatches: 悬空的 ``call_skill`` 调用。
        undispatched: 从未登记意图的调用。
        others: 其余未结算 record（仍一律 fail closed）。
        spawns: 没有终态的分离式派发（ADR 0098）。
        unapplied: 已准入、尚未应用的用户消息（ADR 0101）。
        llm_requests: 可收敛 thread 上没有 checkpoint 的 LLM 请求（ADR 0103）。
    """

    root_thread_id: str
    threads: frozenset[str]
    tool_calls: tuple[UnresolvedToolCall, ...]
    dispatches: tuple[DispatchCall, ...]
    undispatched: tuple[UndispatchedCall, ...]
    others: tuple[str, ...]
    spawns: tuple[InterruptedSpawn, ...] = ()
    unapplied: tuple[JournalEnvelope, ...] = ()
    llm_requests: tuple[JournalEnvelope, ...] = ()

    @property
    def empty(self) -> bool:
        """没有任何可收敛项。"""
        return not (
            self.tool_calls or self.dispatches or self.undispatched
            or self.spawns or self.unapplied or self.llm_requests
        )

    @property
    def child_threads(self) -> tuple[str, ...]:
        """被中断派发的子 thread（按 id 排序，稳定）；恢复会向它们追加记录。"""
        return tuple(sorted(self.threads - {self.root_thread_id}))


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """一次 resume 的完整收敛计划。

    Attributes:
        records: 待原子追加的记录，按因果顺序（子调用 → 派发终态 → 父调用）。
        recovered: root thread 上已收敛调用的处置结论（随 ``thread_resumed`` 透出）。
        pending: 仍需人处置的 record id；非空时整批不写。
    """

    records: tuple[JournalRecord, ...]
    recovered: tuple[RecoveredCall, ...]
    pending: tuple[str, ...]


@dataclass(slots=True)
class _Lineage:
    """按 skill operation 索引的派发谱系记录。"""

    selected: dict[str, JournalEnvelope]
    started: dict[str, JournalEnvelope]
    finished: dict[str, JournalEnvelope]

    @classmethod
    def index(cls, envelopes: Sequence[JournalEnvelope]) -> _Lineage:
        """收集全部派发谱系记录。"""
        lineage = cls({}, {}, {})
        targets = {
            "skill_selected": lineage.selected,
            "skill_dispatch_started": lineage.started,
            "skill_dispatch_finished": lineage.finished,
        }
        for envelope in envelopes:
            target = targets.get(envelope.record_type)
            if target is not None and envelope.operation_id is not None:
                target[envelope.operation_id] = envelope
        return lineage

    def of_tool(self, tool_operation_id: str) -> str | None:
        """外层 tool operation 之下的 skill operation（一次 call_skill 只派发一个目标）。"""
        prefix = f"{tool_operation_id}:skill:"
        for operation_id in self.selected:
            if operation_id.startswith(prefix):
                return operation_id
        return None


def _eligible_threads(
    lineage: _Lineage,
    pending: frozenset[str],
    root_thread_id: str,
    spawns: tuple[InterruptedSpawn, ...] = (),
) -> frozenset[str]:
    """root + 被中断派发的子 thread；父 thread 可收敛时子 thread 才可收敛（逐层传递）。

    分离式派发由 root thread 名下的记录发起，其子 thread 直接可收敛。
    """
    threads = {root_thread_id} | {
        spawn.child_thread_id for spawn in spawns
        if spawn.started.thread_id == root_thread_id
    }
    grown = True
    while grown:
        grown = False
        for operation_id, selected in lineage.selected.items():
            started = lineage.started.get(operation_id)
            if (
                selected.record_id not in pending
                or selected.thread_id not in threads
                or started is None
            ):
                continue
            child = SkillDispatchStartedV1.model_validate(started.payload).child_thread_id
            if child not in threads:
                threads.add(child)
                grown = True
    return frozenset(threads)


def build_recovery_scope(
    envelopes: Sequence[JournalEnvelope],
    pending: Sequence[str],
    root_thread_id: str,
) -> RecoveryScope:
    """把未结算 record 与残留调用分拣成可收敛项与其余。纯计算，不做 IO。

    Raises:
        ValueError / pydantic.ValidationError: 相关 record 形状违约（Journal 不可信）。
    """
    lineage = _Lineage.index(envelopes)
    spawns = interrupted_spawns(envelopes, frozenset(pending))
    threads = _eligible_threads(lineage, frozenset(pending), root_thread_id, spawns)
    calls, leftover = split_unsettled(
        envelopes, pending, root_thread_id, eligible_threads=threads
    )
    tool_calls: list[UnresolvedToolCall] = []
    dispatches: list[DispatchCall] = []
    owned: set[str] = {
        spawn.started.record_id for spawn in spawns if spawn.child_thread_id in threads
    }
    unapplied = unapplied_user_messages(envelopes, frozenset(pending), root_thread_id)
    owned.update(envelope.record_id for envelope in unapplied)
    llm_requests = unsettled_llm_requests(envelopes, frozenset(pending), threads)
    owned.update(envelope.record_id for envelope in llm_requests)
    for call in calls:
        is_dispatch = (
            call.intent_payload.name == CALL_SKILL_TOOL_NAME
            and call.outcome_record_id is None
        )
        if not is_dispatch:
            tool_calls.append(call)
            continue
        assert call.intent.operation_id is not None
        operation_id = lineage.of_tool(call.intent.operation_id)
        selected = lineage.selected.get(operation_id) if operation_id else None
        dispatches.append(DispatchCall(
            call=call,
            selected=selected,
            started=lineage.started.get(operation_id) if operation_id else None,
            finished=lineage.finished.get(operation_id) if operation_id else None,
        ))
        if selected is not None:
            owned.add(selected.record_id)
    undispatched = tuple(
        undispatched_call(envelope)
        for thread_id in sorted(threads)
        for envelope in find_undispatched_calls(envelopes, thread_id)
    )
    return RecoveryScope(
        root_thread_id=root_thread_id,
        threads=threads,
        tool_calls=tuple(tool_calls),
        dispatches=tuple(dispatches),
        undispatched=undispatched,
        # 已由某个悬空 call_skill 认领的 skill_selected 随派发一起结算；没有终态的派发同理
        others=tuple(record_id for record_id in leftover if record_id not in owned),
        spawns=tuple(spawn for spawn in spawns if spawn.started.record_id in owned),
        unapplied=unapplied,
        llm_requests=llm_requests,
    )


def hopeless_without_lock(
    scope: RecoveryScope,
    registry: ToolRegistry,
    resolver: AuditToolOutcomeResolver | None,
) -> tuple[str, ...]:
    """不持锁、不回查即可断定只能交人的 record id（供只读预检使用）。"""
    return tuple(
        call.record_id
        for call in scope.tool_calls
        if needs_operator_without_lock(call, registry, resolver)
    ) + tuple(call.record_id for call in scope.undispatched if needs_operator(call))


@dataclass(slots=True)
class _ThreadResult:
    """一个 thread 的收敛结果。"""

    records: list[JournalRecord]
    recovered: list[RecoveredCall]
    pending: list[str]


class _Planner:
    """沿派发树深度优先收敛：先子 thread，后派发终态，最后父调用。"""

    def __init__(
        self,
        scope: RecoveryScope,
        *,
        registry: ToolRegistry,
        resolver: AuditToolOutcomeResolver | None,
        session_id: str,
        recovery_operation_id: str,
    ) -> None:
        """冻结本次恢复的输入。"""
        self._scope = scope
        self._registry = registry
        self._resolver = resolver
        self._session_id = session_id
        self._recovery_operation_id = recovery_operation_id

    async def settle_thread(self, thread_id: str) -> _ThreadResult:
        """按 Journal seq 顺序收敛该 thread 上的全部待收敛项。"""
        result = _ThreadResult([], [], [])
        work: list[tuple[int, UnresolvedToolCall | DispatchCall]] = [
            (call.intent.seq, call)
            for call in self._scope.tool_calls
            if call.intent.thread_id == thread_id
        ]
        work += [
            (dispatch.call.intent.seq, dispatch)
            for dispatch in self._scope.dispatches
            if dispatch.call.intent.thread_id == thread_id
        ]
        for _, item in sorted(work, key=lambda pair: pair[0]):
            if isinstance(item, DispatchCall):
                await self._settle_dispatch(item, result)
            else:
                await self._settle_tool_call(item, result)
        # 没有 checkpoint 的 LLM 请求：作废（没有外部副作用，回复没进过对话）
        result.records.extend(
            abandoned_request_record(
                request, session_id=self._session_id,
                recovery_operation_id=self._recovery_operation_id,
            )
            for request in self._scope.llm_requests
            if request.thread_id == thread_id
        )
        undispatched = plan_undispatched_recovery(
            [
                call for call in self._scope.undispatched
                if call.function_call.thread_id == thread_id
            ],
            session_id=self._session_id,
            recovery_operation_id=self._recovery_operation_id,
        )
        result.records.extend(undispatched.records)
        result.recovered.extend(undispatched.recovered)
        result.pending.extend(undispatched.pending)
        return result

    async def _settle_tool_call(
        self, call: UnresolvedToolCall, result: _ThreadResult,
    ) -> None:
        """普通工具调用：回查 → 副作用声明 → 人。"""
        spec = self._registry.get(call.intent_payload.name)
        decision = await decide_tool_call(
            call, spec, self._resolver, session_id=self._session_id
        )
        if decision is None:
            result.pending.append(call.record_id)
            return
        self._record_call(call, decision, result)

    async def _settle_dispatch(
        self, dispatch: DispatchCall, result: _ThreadResult,
    ) -> None:
        """``call_skill`` 调用：按派发谱系结算。"""
        if dispatch.finished is not None:
            self._record_call(dispatch.call, _completed_decision(dispatch.finished), result)
            return
        if dispatch.started is None:
            if dispatch.selected is not None:
                result.records.append(self._never_started_record(dispatch.selected))
            decision = ToolCallDecision(
                "dispatch", "not_started", DISPATCH_NOT_STARTED_TEXT, True, None
            )
            self._record_call(dispatch.call, decision, result)
            return
        child_thread_id = dispatch.child_thread_id
        assert child_thread_id is not None
        assert dispatch.selected is not None
        child = await self.settle_thread(child_thread_id)
        if child.pending:
            # 子 thread 仍有调用需人裁决：本层不落任何结论
            result.pending.extend(child.pending)
            return
        result.records.extend(child.records)
        result.records.extend(
            self._interrupted_records(dispatch.selected, dispatch.started, child_thread_id)
        )
        skill_id = SkillSelectedV1.model_validate(dispatch.selected.payload).skill_id
        decision = ToolCallDecision(
            "dispatch",
            "interrupted",
            dispatch_interrupted_text(skill_id, child.recovered),
            True,
            None,
        )
        self._record_call(dispatch.call, decision, result)

    def _record_call(
        self,
        call: UnresolvedToolCall,
        decision: ToolCallDecision,
        result: _ThreadResult,
    ) -> None:
        """落一个调用的结论记录与处置。"""
        result.records.extend(recovery_records(
            call,
            decision,
            session_id=self._session_id,
            recovery_operation_id=self._recovery_operation_id,
        ))
        result.recovered.append(RecoveredCall(
            call.intent_payload.call_id, call.intent_payload.name, decision.disposition
        ))

    def _factory(self, selected: JournalEnvelope) -> JournalRecordFactory:
        """派发谱系记录的 factory：identity 取父 thread 与其 submission。"""
        assert selected.thread_id is not None
        assert selected.submission_id is not None
        return JournalRecordFactory(
            session_id=self._session_id,
            actor=_SYSTEM_ACTOR,
            identities=JournalIdentities(
                self._session_id, selected.thread_id, selected.submission_id
            ),
        )

    def _never_started_record(self, selected: JournalEnvelope) -> JournalRecord:
        """已选定、从未启动的派发：补 finished(rejected)，不带子谱系。"""
        assert selected.operation_id is not None
        payload = SkillSelectedV1.model_validate(selected.payload)
        return self._factory(selected).build(
            operation_id=selected.operation_id,
            record_type="skill_dispatch_finished",
            payload=SkillDispatchFinishedV1(
                started_record_id=None,
                call_id=payload.call_id,
                child_thread_id=None,
                status=SkillStatus.REJECTED,
                end_reason=RECOVERY_BEFORE_START_END_REASON,
            ),
            submission_id=selected.submission_id,
            thread_id=selected.thread_id,
            turn_id=selected.turn_id,
            causation_id=selected.record_id,
            correlation_id=self._recovery_operation_id,
        )

    def _interrupted_records(
        self,
        selected: JournalEnvelope,
        started: JournalEnvelope,
        child_thread_id: str,
    ) -> tuple[JournalRecord, JournalRecord]:
        """被中断的派发：finished(cancelled) + 子 thread 的 thread_terminal。"""
        assert selected.operation_id is not None
        factory = self._factory(selected)
        payload = SkillSelectedV1.model_validate(selected.payload)
        finished = factory.build(
            operation_id=selected.operation_id,
            record_type="skill_dispatch_finished",
            payload=SkillDispatchFinishedV1(
                started_record_id=started.record_id,
                call_id=payload.call_id,
                child_thread_id=child_thread_id,
                status=SkillStatus.CANCELLED,
                end_reason=RECOVERY_END_REASON,
                stable_error=_INTERRUPTED_ERROR,
            ),
            submission_id=selected.submission_id,
            thread_id=selected.thread_id,
            turn_id=selected.turn_id,
            causation_id=started.record_id,
            correlation_id=self._recovery_operation_id,
        )
        terminal = factory.build(
            operation_id=selected.operation_id,
            record_type="thread_terminal",
            payload=ThreadTerminalV1(
                status=SkillStatus.CANCELLED.value,
                end_reason=RECOVERY_END_REASON,
                stable_error=_INTERRUPTED_ERROR,
            ),
            submission_id=selected.submission_id,
            thread_id=child_thread_id,
            turn_id=selected.turn_id,
            causation_id=finished.record_id,
            correlation_id=self._recovery_operation_id,
        )
        return finished, terminal


def _completed_decision(finished: JournalEnvelope) -> ToolCallDecision:
    """子 skill 已 durable 结束：用已落账的终态还原父调用本应得到的结果。"""
    payload = SkillDispatchFinishedV1.model_validate(finished.payload)
    if payload.status is SkillStatus.SUCCESS:
        return ToolCallDecision("dispatch", "completed", payload.final_text or "", False, None)
    reason = payload.end_reason or payload.status.value
    return ToolCallDecision(
        "dispatch", "completed", f"sub_skill_failed: {reason}", True, None
    )


async def plan_recovery(
    scope: RecoveryScope,
    *,
    registry: ToolRegistry,
    resolver: AuditToolOutcomeResolver | None,
    session_id: str,
    recovery_operation_id: str,
) -> RecoveryPlan:
    """从 root thread 起沿派发树收敛全部待收敛项（调用方须已持有写者锁）。

    Raises:
        AuditToolResolutionError: resolver 返回的裁决不适用于该调用。
        Exception: resolver 自身抛出的异常原样上抛。
    """
    planner = _Planner(
        scope,
        registry=registry,
        resolver=resolver,
        session_id=session_id,
        recovery_operation_id=recovery_operation_id,
    )
    root = await planner.settle_thread(scope.root_thread_id)
    for spawn in scope.spawns:
        # 分离式派发：先收敛子 thread 上的调用，再落这次派发的终态
        child = await planner.settle_thread(spawn.child_thread_id)
        root.pending.extend(child.pending)
        if child.pending:
            continue
        root.records.extend(child.records)
        root.records.extend(interrupted_spawn_records(
            spawn, session_id=session_id, recovery_operation_id=recovery_operation_id,
        ))
    for accepted in scope.unapplied:
        # 已准入、尚未应用的消息落在对话末尾：其他结论先写
        root.records.extend(application_recovery_records(
            accepted, session_id=session_id, recovery_operation_id=recovery_operation_id,
        ))
    return RecoveryPlan(tuple(root.records), tuple(root.recovered), tuple(root.pending))


__all__ = [
    "CALL_SKILL_TOOL_NAME",
    "RECOVERY_BEFORE_START_END_REASON",
    "RECOVERY_END_REASON",
    "DispatchCall",
    "RecoveryPlan",
    "RecoveryScope",
    "build_recovery_scope",
    "hopeless_without_lock",
    "plan_recovery",
]
