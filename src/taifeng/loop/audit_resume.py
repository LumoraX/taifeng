"""strict audit Session 的 resume：投影 marker 定位 → Journal 接管 → 对账 → 续跑。

顺序（任一步失败都不启动 Engine、不产生 effect）：

1. 读取投影 thread 的 audited marker，拿到 ``journal_session_id``（须等于请求 Session）；
2. 只读预检（不持锁）：注定被拒的请求不写接管记录；
3. ``open_existing`` 以更高 writer epoch 接管 Journal（跨进程 flock 互斥）；
4. 持锁后 strict 重读全部 committed envelopes：root thread 必须等于 ``resume_thread_id``；
   root thread 上「结果未知」的工具调用按副作用分流收敛（``audit_resume_tools``，ADR 0070），
   结论作为 ``tool_recovery_committed``（+ output 会话项）原子追加；其余未结算 effect、以及
   无法自动收敛且无人裁决的调用一律 fail closed（``audit_resume_recovery_required`` + record id）；
5. 用 root thread 已提交 ``conversation_item`` 重建 initial history；
6. coordinator 用新 lease / expected_seq，projector 复用既有投影 thread 并以 Journal 为
   真相核对（缺后缀补齐，分叉只标 stale）。

步骤 3 之后的失败只释放 lease，不写 ``session_ended``——resume 失败不是 Session 的终结。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from taifeng.conversation.journal.errors import (
    JournalBusyError,
    JournalError,
    JournalRecoveryRequiredError,
    JournalSessionEndedError,
    JournalSessionNotFoundError,
)
from taifeng.conversation.journal.models import SessionLease, SessionOpenResult
from taifeng.conversation.journal.projector import (
    JournalConversationProjector,
    ProjectionOrderError,
)
from taifeng.loop.audit import SessionAuditCoordinator
from taifeng.loop.audit_bootstrap import AuditedSessionState, _emergency_close
from taifeng.loop.audit_resume_resolution import AuditToolResolutionError
from taifeng.loop.audit_resume_scan import (
    ResumedHistory,
    find_unsettled_effects,
    rebuild_root_history,
    root_thread_id,
)
from taifeng.loop.audit_resume_tools import (
    UnresolvedToolCall,
    needs_operator_without_lock,
    plan_audited_tool_recovery,
    split_unsettled,
)

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord
    from taifeng.conversation.models import ResponseItem
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.loop.audit_config import AuditConfig
    from taifeng.loop.tool_recovery import RecoveredCall
    from taifeng.tool.registry import ToolRegistry

# core 错误 → 稳定 resume code（接管与恢复追加共用；其余 core 错误归 open_failed）
_CORE_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (JournalBusyError, "audit_resume_busy"),
    (JournalSessionEndedError, "audit_resume_session_ended"),
    (JournalRecoveryRequiredError, "audit_resume_recovery_required"),
    (JournalSessionNotFoundError, "audit_resume_journal_missing"),
)


class AuditResumeError(RuntimeError):
    """审计 Session resume 被拒绝；``code`` 稳定可断言，不携带底层异常文本。

    codes：``audit_resume_projection_unavailable`` / ``audit_resume_marker_missing`` /
    ``audit_resume_marker_invalid`` / ``audit_resume_session_mismatch`` /
    ``audit_resume_journal_missing`` / ``audit_resume_busy`` /
    ``audit_resume_session_ended`` / ``audit_resume_recovery_required`` /
    ``audit_resume_open_failed`` / ``audit_resume_journal_invalid`` /
    ``audit_resume_thread_mismatch`` / ``audit_resume_resolution_invalid`` /
    ``audit_resume_projection_conflict`` / ``audit_resume_session_active``。

    ``record_ids`` 仅在 ``recovery_required``（本次无法自动结算、需人处置的 record）与
    ``resolution_invalid``（裁决不适用的 record）时非空。
    """

    def __init__(
        self,
        code: str,
        *,
        session_id: str,
        thread_id: str,
        record_ids: tuple[str, ...] = (),
    ) -> None:
        """记录稳定 code、定位信息与（recovery_required / resolution_invalid 时）相关 record id。"""
        super().__init__(f"{code}: session={session_id}, thread={thread_id}")
        self.code = code
        self.session_id = session_id
        self.thread_id = thread_id
        self.record_ids = record_ids


def ensure_audited_cache_hit(
    config: AuditConfig | None,
    session_id: str,
    cached_thread_id: str,
    resume_thread_id: str | None,
) -> None:
    """audited Session 已 live 时，resume 只能指向同一 root thread。"""
    if config is None or resume_thread_id is None or resume_thread_id == cached_thread_id:
        return
    raise AuditResumeError(
        "audit_resume_session_active",
        session_id=session_id,
        thread_id=resume_thread_id,
    )


async def _journal_session_from_marker(
    projection_store: JsonlMessageStore | None,
    *,
    session_id: str,
    thread_id: str,
) -> str:
    """metadata-only 读取 audited marker，定位并核对 Journal Session identity。"""

    def fail(code: str) -> AuditResumeError:
        return AuditResumeError(code, session_id=session_id, thread_id=thread_id)

    if projection_store is None:
        raise fail("audit_resume_projection_unavailable")
    try:
        marker = await projection_store.audited_projection_marker(thread_id)
    except (OSError, ValueError) as exc:
        raise fail("audit_resume_marker_invalid") from exc
    if marker is None:
        raise fail("audit_resume_marker_missing")
    if marker.session_id != session_id:
        raise fail("audit_resume_session_mismatch")
    return marker.session_id


def _core_error(exc: Exception, *, session_id: str, thread_id: str) -> AuditResumeError:
    """把 core 异常映射为稳定 resume code（不携带底层异常文本）。"""
    code = next((c for kind, c in _CORE_ERROR_CODES if isinstance(exc, kind)), None)
    return AuditResumeError(
        code or "audit_resume_open_failed", session_id=session_id, thread_id=thread_id
    )


async def _open_journal(
    config: AuditConfig,
    *,
    session_id: str,
    thread_id: str,
    operation_id: str,
) -> SessionOpenResult:
    """以本 pool writer 身份接管 Journal，把 core 错误映射为稳定 resume code。"""
    try:
        opened = await config.journal_core.open_existing(
            session_id, writer_id=config.writer_id, operation_id=operation_id
        )
    except Exception as exc:
        raise _core_error(exc, session_id=session_id, thread_id=thread_id) from exc
    lease = opened.lease
    if (
        type(opened) is not SessionOpenResult
        or lease.session_id != session_id
        or lease.writer_id != config.writer_id
        or lease.writer_epoch < 2
        or opened.ack.writer_epoch != lease.writer_epoch
    ):
        # core 返回值不满足 trust boundary：仍须释放已取得的 writer
        await _emergency_close(config, lease)
        raise AuditResumeError(
            "audit_resume_open_failed", session_id=session_id, thread_id=thread_id
        )
    return opened


@dataclass(frozen=True, slots=True)
class _Triage:
    """一次 strict 读取的分拣结果：可按工具恢复收敛的调用 vs 其余未结算 record。"""

    envelopes: tuple[JournalEnvelope, ...]
    pending: tuple[str, ...]
    tool_calls: tuple[UnresolvedToolCall, ...]
    others: tuple[str, ...]

    def ordered(self, record_ids: tuple[str, ...]) -> tuple[str, ...]:
        """按 Journal seq 顺序输出给定 record id。"""
        wanted = set(record_ids)
        return tuple(record_id for record_id in self.pending if record_id in wanted)


@dataclass(frozen=True, slots=True)
class AuditResumeResult:
    """resume 成功后交给 EnginePool 的续跑材料。"""

    state: AuditedSessionState
    history: tuple[ResponseItem, ...]
    recovered_tool_calls: tuple[RecoveredCall, ...]


async def _read_triage(
    config: AuditConfig,
    *,
    session_id: str,
    thread_id: str,
) -> _Triage | None:
    """strict 读取 committed envelopes 并分拣；文件缺失返回 None 交由 open 报错。"""
    try:
        envelopes = tuple(
            [envelope async for envelope in config.journal_core.load(session_id)]
        )
        if not envelopes:
            return None
        if root_thread_id(envelopes) != thread_id:
            raise AuditResumeError(
                "audit_resume_thread_mismatch", session_id=session_id, thread_id=thread_id
            )
        pending = find_unsettled_effects(envelopes)
        tool_calls, others = split_unsettled(envelopes, pending, thread_id)
    except (JournalError, ValidationError, ValueError) as exc:
        # 完整性 / 解码违约：Journal 不可信，不得续跑
        raise AuditResumeError(
            "audit_resume_journal_invalid", session_id=session_id, thread_id=thread_id
        ) from exc
    return _Triage(envelopes, pending, tool_calls, others)


def _refuse(triage: _Triage, record_ids: tuple[str, ...], *, session_id: str,
            thread_id: str) -> AuditResumeError:
    """构造 recovery_required 拒绝，record id 按 Journal seq 排序。"""
    return AuditResumeError(
        "audit_resume_recovery_required",
        session_id=session_id,
        thread_id=thread_id,
        record_ids=triage.ordered(record_ids),
    )


def _precheck(
    triage: _Triage,
    *,
    tool_registry: ToolRegistry,
    config: AuditConfig,
    session_id: str,
    thread_id: str,
) -> None:
    """只读预检：不持锁、不回查即可断定会被拒的请求直接拒绝，不写接管记录。

    存在工具以外的未结算 effect 时恢复不会运行，列出全部未结算 record；否则只列出无回查、
    不可安全重发、又没有 resolver 可问的调用（需回查 / 可问人的留到持锁后判定）。
    """
    if triage.others:
        raise _refuse(triage, triage.pending, session_id=session_id, thread_id=thread_id)
    hopeless = tuple(
        call.record_id
        for call in triage.tool_calls
        if needs_operator_without_lock(call, tool_registry, config.tool_outcome_resolver)
    )
    if hopeless:
        raise _refuse(triage, hopeless, session_id=session_id, thread_id=thread_id)


async def resume_audited_session(
    *,
    config: AuditConfig,
    projection_store: JsonlMessageStore | None,
    session_id: str,
    resume_thread_id: str,
    tool_registry: ToolRegistry,
) -> AuditResumeResult:
    """接管已有 audited Session，收敛结果未知的工具调用，返回续跑材料。

    Raises:
        AuditResumeError: 任何拒绝；接管成功后的失败会先释放 lease 再抛。
        Exception: ``tool_outcome_resolver`` 自身的异常原样上抛（同样先释放 lease）。
    """
    journal_session_id = await _journal_session_from_marker(
        projection_store, session_id=session_id, thread_id=resume_thread_id
    )
    assert projection_store is not None
    ids = {"session_id": journal_session_id, "thread_id": resume_thread_id}
    # 预检（不持锁、只读）：注定被拒的请求不写接管记录，避免无意义地抬高 epoch
    precheck = await _read_triage(config, **ids)
    if precheck is not None:
        _precheck(precheck, tool_registry=tool_registry, config=config, **ids)
    operation_id = f"{journal_session_id}:resume:{secrets.token_hex(8)}"
    opened = await _open_journal(config, operation_id=operation_id, **ids)
    try:
        # 权威校验：持锁后重读，覆盖预检与接管之间他人写入的窗口
        triage = await _read_triage(config, **ids)
        if triage is None:
            raise AuditResumeError(
                "audit_resume_journal_invalid",
                session_id=journal_session_id,
                thread_id=resume_thread_id,
            )
        if triage.others:
            raise _refuse(triage, triage.pending, **ids)
        envelopes, expected_seq, recovered = await _recover_tool_calls(
            config, opened, triage, tool_registry=tool_registry, operation_id=operation_id,
            **ids,
        )
        history = _rebuild_history(envelopes, **ids)
        state = await _resumed_state(
            config, projection_store, opened.lease, expected_seq, resume_thread_id, history,
        )
    except BaseException:
        await _emergency_close(config, opened.lease)
        raise
    return AuditResumeResult(state, history.items, recovered)


async def _recover_tool_calls(
    config: AuditConfig,
    opened: SessionOpenResult,
    triage: _Triage,
    *,
    tool_registry: ToolRegistry,
    operation_id: str,
    session_id: str,
    thread_id: str,
) -> tuple[tuple[JournalEnvelope, ...], int, tuple[RecoveredCall, ...]]:
    """持锁收敛 root thread 的结果未知工具调用；返回 (envelopes, expected_seq, 处置结论)。

    全部调用都得出结论才原子追加一个 batch；任一仍需人裁决即整批不写并拒绝。追加后 strict
    重读，确认已无未结算 effect 且 tail 与 ack 一致。
    """
    ids = {"session_id": session_id, "thread_id": thread_id}
    if not triage.tool_calls:
        return triage.envelopes, opened.ack.last_seq, ()
    try:
        plan = await plan_audited_tool_recovery(
            triage.tool_calls,
            registry=tool_registry,
            resolver=config.tool_outcome_resolver,
            session_id=session_id,
            recovery_operation_id=operation_id,
        )
    except AuditToolResolutionError as exc:
        raise AuditResumeError(
            "audit_resume_resolution_invalid",
            session_id=session_id,
            thread_id=thread_id,
            record_ids=(exc.record_id,),
        ) from exc
    if plan.pending:
        raise _refuse(triage, plan.pending, **ids)
    last_seq = await _append_recovery(config, opened, plan.records, **ids)
    reread = await _read_triage(config, **ids)
    if reread is None or reread.pending or reread.envelopes[-1].seq != last_seq:
        # 恢复记录已 durable 却读不回一致状态：core 返回值不满足 trust boundary
        raise AuditResumeError(
            "audit_resume_open_failed", session_id=session_id, thread_id=thread_id
        )
    return reread.envelopes, last_seq, plan.recovered


async def _append_recovery(
    config: AuditConfig,
    opened: SessionOpenResult,
    records: tuple[JournalRecord, ...],
    *,
    session_id: str,
    thread_id: str,
) -> int:
    """以接管 lease 原子追加恢复结论，返回新的 committed tail seq。"""
    try:
        ack = await config.journal_core.append_batch(
            records, lease=opened.lease, expected_seq=opened.ack.last_seq
        )
    except Exception as exc:
        raise _core_error(exc, session_id=session_id, thread_id=thread_id) from exc
    return ack.last_seq


def _rebuild_history(
    envelopes: tuple[JournalEnvelope, ...], *, session_id: str, thread_id: str,
) -> ResumedHistory:
    """重建 root history；payload 解码违约即 Journal 不可信。"""
    try:
        return rebuild_root_history(envelopes, thread_id)
    except (ValidationError, ValueError) as exc:
        raise AuditResumeError(
            "audit_resume_journal_invalid", session_id=session_id, thread_id=thread_id
        ) from exc


async def _resumed_state(
    config: AuditConfig,
    projection_store: JsonlMessageStore,
    lease: SessionLease,
    expected_seq: int,
    thread_id: str,
    history: ResumedHistory,
) -> AuditedSessionState:
    """构造新 lease 的 coordinator，并让 projector 复用、核对既有投影 thread。"""
    coordinator = SessionAuditCoordinator(
        core=config.journal_core, lease=lease, expected_seq=expected_seq
    )
    projector = JournalConversationProjector(projection_store)
    try:
        projection = await projector.reconcile_resumed_thread(
            thread_id=thread_id,
            session_id=lease.session_id,
            items=history.items,
            first_seq=history.first_seq,
            last_seq=history.last_seq,
        )
    except ProjectionOrderError as exc:
        raise AuditResumeError(
            "audit_resume_projection_conflict",
            session_id=lease.session_id,
            thread_id=thread_id,
        ) from exc
    coordinator.update_projection(projection)

    async def abort_resume() -> None:
        """Engine 启动失败：只释放本次接管的 lease，不写 session_ended。"""
        await _emergency_close(config, lease)

    return AuditedSessionState(
        thread_id=thread_id,
        coordinator=coordinator,
        projector=projector,
        max_attachment_bytes=config.max_attachment_bytes,
        max_total_attachment_bytes=config.max_total_attachment_bytes,
        next_turn_index=history.next_turn_index,
        abort_resume=abort_resume,
    )


__all__ = [
    "AuditResumeError",
    "AuditResumeResult",
    "ensure_audited_cache_hit",
    "resume_audited_session",
]
