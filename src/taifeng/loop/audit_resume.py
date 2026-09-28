"""strict audit Session 的 resume：投影 marker 定位 → Journal 接管 → 对账 → 续跑。

顺序（任一步失败都不启动 Engine、不产生 effect）：

1. 读取投影 thread 的 audited marker，拿到 ``journal_session_id``（须等于请求 Session）；
2. ``open_existing`` 以更高 writer epoch 接管 Journal（跨进程 flock 互斥）；
3. strict 读取全部 committed envelopes：root thread 必须等于 ``resume_thread_id``；存在
   未结算 effect 即 fail closed（``audit_resume_recovery_required`` + record id 列表）；
4. 用 root thread 已提交 ``conversation_item`` 重建 initial history；
5. coordinator 用新 lease / expected_seq，projector 复用既有投影 thread 并以 Journal 为
   真相核对（缺后缀补齐，分叉只标 stale）。

步骤 2 之后的失败只释放 lease，不写 ``session_ended``——resume 失败不是 Session 的终结。
"""

from __future__ import annotations

import secrets
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
from taifeng.loop.audit_resume_scan import (
    ResumedHistory,
    find_unsettled_effects,
    rebuild_root_history,
    root_thread_id,
)

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import JournalEnvelope
    from taifeng.conversation.models import ResponseItem
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.loop.audit_config import AuditConfig


class AuditResumeError(RuntimeError):
    """审计 Session resume 被拒绝；``code`` 稳定可断言，不携带底层异常文本。

    codes：``audit_resume_projection_unavailable`` / ``audit_resume_marker_missing`` /
    ``audit_resume_marker_invalid`` / ``audit_resume_session_mismatch`` /
    ``audit_resume_journal_missing`` / ``audit_resume_busy`` /
    ``audit_resume_session_ended`` / ``audit_resume_recovery_required`` /
    ``audit_resume_open_failed`` / ``audit_resume_journal_invalid`` /
    ``audit_resume_thread_mismatch`` /
    ``audit_resume_projection_conflict`` / ``audit_resume_session_active``。
    """

    def __init__(
        self,
        code: str,
        *,
        session_id: str,
        thread_id: str,
        record_ids: tuple[str, ...] = (),
    ) -> None:
        """记录稳定 code、定位信息与（仅 recovery_required 时）待对账 record id。"""
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


async def _open_journal(
    config: AuditConfig,
    *,
    session_id: str,
    thread_id: str,
) -> SessionOpenResult:
    """以本 pool writer 身份接管 Journal，把 core 错误映射为稳定 resume code。"""
    codes: tuple[tuple[type[Exception], str], ...] = (
        (JournalBusyError, "audit_resume_busy"),
        (JournalSessionEndedError, "audit_resume_session_ended"),
        (JournalRecoveryRequiredError, "audit_resume_recovery_required"),
        (JournalSessionNotFoundError, "audit_resume_journal_missing"),
    )
    operation_id = f"{session_id}:resume:{secrets.token_hex(8)}"
    try:
        opened = await config.journal_core.open_existing(
            session_id, writer_id=config.writer_id, operation_id=operation_id
        )
    except Exception as exc:
        code = next((c for kind, c in codes if isinstance(exc, kind)), None)
        raise AuditResumeError(
            code or "audit_resume_open_failed",
            session_id=session_id,
            thread_id=thread_id,
        ) from exc
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


async def _load_validated_history(
    config: AuditConfig,
    *,
    session_id: str,
    thread_id: str,
) -> ResumedHistory | None:
    """strict 读取 committed envelopes 并校验；文件缺失返回 None 交由 open 报错。"""
    try:
        envelopes = tuple(
            [envelope async for envelope in config.journal_core.load(session_id)]
        )
        if not envelopes:
            return None
        return _validated_history(envelopes, session_id=session_id, thread_id=thread_id)
    except (JournalError, ValidationError, ValueError) as exc:
        # 完整性 / 解码违约：Journal 不可信，不得续跑
        raise AuditResumeError(
            "audit_resume_journal_invalid", session_id=session_id, thread_id=thread_id
        ) from exc


def _validated_history(
    envelopes: tuple[JournalEnvelope, ...],
    *,
    session_id: str,
    thread_id: str,
) -> ResumedHistory:
    """核对 root thread、拒绝未结算 effect，并重建 root history。"""
    if root_thread_id(envelopes) != thread_id:
        raise AuditResumeError(
            "audit_resume_thread_mismatch", session_id=session_id, thread_id=thread_id
        )
    pending = find_unsettled_effects(envelopes)
    if pending:
        raise AuditResumeError(
            "audit_resume_recovery_required",
            session_id=session_id,
            thread_id=thread_id,
            record_ids=pending,
        )
    return rebuild_root_history(envelopes, thread_id)


async def resume_audited_session(
    *,
    config: AuditConfig,
    projection_store: JsonlMessageStore | None,
    session_id: str,
    resume_thread_id: str,
) -> tuple[AuditedSessionState, tuple[ResponseItem, ...]]:
    """接管已有 audited Session，返回续跑用的 state 与 initial history。

    Raises:
        AuditResumeError: 任何拒绝；接管成功后的失败会先释放 lease 再抛。
    """
    journal_session_id = await _journal_session_from_marker(
        projection_store, session_id=session_id, thread_id=resume_thread_id
    )
    assert projection_store is not None
    # 预检（不持锁、只读）：注定被拒的请求不写接管记录，避免无意义地抬高 epoch
    await _load_validated_history(
        config, session_id=journal_session_id, thread_id=resume_thread_id
    )
    opened = await _open_journal(
        config, session_id=journal_session_id, thread_id=resume_thread_id
    )
    try:
        # 权威校验：持锁后重读，覆盖预检与接管之间他人写入的窗口
        history = await _load_validated_history(
            config, session_id=journal_session_id, thread_id=resume_thread_id
        )
        if history is None:
            raise AuditResumeError(
                "audit_resume_journal_invalid",
                session_id=journal_session_id,
                thread_id=resume_thread_id,
            )
        state = await _resumed_state(
            config, projection_store, opened.lease, opened.ack.last_seq,
            resume_thread_id, history,
        )
    except BaseException:
        await _emergency_close(config, opened.lease)
        raise
    return state, history.items


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
    "ensure_audited_cache_hit",
    "resume_audited_session",
]
