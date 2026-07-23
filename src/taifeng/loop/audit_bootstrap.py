"""EnginePool 的 strict audit Journal-first bootstrap 与 downgrade 门禁。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng.conversation.journal.models import (
    RootThreadDescriptor,
    SessionDescriptor,
)
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.journal.records import StableErrorV1
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.loop.audit import SessionAuditCoordinator
from taifeng.loop.audit_config import (
    AuditConfig,
    AuditStaticInputs,
    validate_audit_config,
)
from taifeng.loop.audit_lifecycle import SessionFinishResult, ThreadTerminalRequest

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.context.compressor import CompressionOrchestrator
    from taifeng.conversation.store import MessageStore
    from taifeng.llm.client import ModelClient
    from taifeng.skill.registry import SkillSnapshot


@dataclass(frozen=True, slots=True)
class AuditedSessionState:
    """EnginePool 持有的 audited Session bootstrap ownership。"""

    thread_id: str
    coordinator: SessionAuditCoordinator
    projector: JournalConversationProjector


class AuditEngineCreationError(RuntimeError):
    """audited Engine 无法完成安全 bootstrap。"""

    code = "audit_engine_creation_failed"

    def __init__(
        self,
        session_id: str,
        *,
        finish_result: SessionFinishResult | None = None,
    ) -> None:
        """只暴露稳定 code/session 与 coordinator 的有限结果。"""
        super().__init__(f"{self.code}: session={session_id}")
        self.session_id = session_id
        self.finish_result = finish_result


class AuditDowngradeError(RuntimeError):
    """legacy resume 指向 audited transcript。"""

    code = "audit_downgrade_forbidden"

    def __init__(self, thread_id: str) -> None:
        """只暴露稳定 code/thread id，不拼接 metadata。"""
        super().__init__(f"{self.code}: thread={thread_id}")
        self.thread_id = thread_id


def resolve_projection_store(store: MessageStore) -> JsonlMessageStore | None:
    """从默认 store 或 EnginePool 内建 hook wrapper 解析真实 JSONL 投影。"""
    if isinstance(store, JsonlMessageStore):
        return store
    candidate = getattr(store, "audit_projection_store", None)
    return candidate if isinstance(candidate, JsonlMessageStore) else None


def validate_pool_audit(
    config: AuditConfig | None,
    *,
    model_client: ModelClient,
    skill_snapshot: SkillSnapshot,
    tools: Iterable[object],
    store: MessageStore,
    compressors: CompressionOrchestrator | None,
    hooks: object | None,
    permission_policy: object | None,
    memory_store: object | None,
    memory_query_builder: object | None,
    pinned_state_sources: Iterable[object],
    instruction_layers: Iterable[object],
    failure_policy: object | None,
    failure_suspend_ttl_seconds: int | None,
    failure_suspend_max_auto_retries: int | None,
    failure_suspend_on_expire: str,
) -> None:
    """用 EnginePool 已解析的真实依赖构造 static gate 输入。"""
    projection_store = resolve_projection_store(store)
    validate_audit_config(
        config,
        static_inputs=AuditStaticInputs(
            model_client=model_client,
            skill_snapshot=skill_snapshot,
            tools=tuple(tools),
            custom_store=None if projection_store is not None else store,
            custom_directory=getattr(store, "audit_custom_directory", None),
            index_hook=getattr(store, "audit_index_hook", None),
            hooks=hooks,
            permission_policy=permission_policy,
            compressor=compressors,
            memory_store=memory_store,
            memory_query_builder=memory_query_builder,
            pinned_state_sources=tuple(pinned_state_sources),
            instruction_layers=tuple(instruction_layers),
            failure_policy=failure_policy,
            failure_suspension_enabled=False,
            failure_suspend_ttl_seconds=failure_suspend_ttl_seconds,
            failure_suspend_max_auto_retries=failure_suspend_max_auto_retries,
            failure_suspend_on_expire=failure_suspend_on_expire,  # type: ignore[arg-type]
            skill_suspension_enabled=False,
        ),
    )


async def ensure_legacy_resume_allowed(
    store: MessageStore,
    thread_id: str,
) -> None:
    """metadata-only 检查 audited marker，禁止 legacy history load 降级。"""
    projection_store = resolve_projection_store(store)
    if projection_store is None:
        return
    try:
        marker = await projection_store.audited_projection_marker(thread_id)
    except Exception as exc:
        raise AuditDowngradeError(thread_id) from exc
    if marker is not None:
        raise AuditDowngradeError(thread_id)


async def bootstrap_audited_session(
    *,
    config: AuditConfig,
    store: MessageStore,
    session_id: str,
    entry_skill_id: str,
    cwd: str | None,
) -> AuditedSessionState:
    """按 Journal→coordinator→projection 顺序建立 audited Session。"""
    projection_store = resolve_projection_store(store)
    if projection_store is None:
        raise AuditEngineCreationError(session_id)
    thread_id = f"thr_{secrets.token_hex(8)}"
    descriptor = _session_descriptor(
        config=config,
        session_id=session_id,
        thread_id=thread_id,
        entry_skill_id=entry_skill_id,
        cwd=cwd,
    )
    try:
        created = await config.journal_core.create_session(descriptor)
    except Exception as exc:
        raise AuditEngineCreationError(session_id) from exc
    coordinator = SessionAuditCoordinator(
        core=config.journal_core,
        lease=created.lease,
        expected_seq=created.ack.last_seq,
    )
    projector = JournalConversationProjector(projection_store)
    state = AuditedSessionState(thread_id, coordinator, projector)
    try:
        await projector.bootstrap_thread(
            thread_id=thread_id,
            cwd=cwd,
            entry_skill_id=entry_skill_id,
            source=f"session:{session_id}",
            extra={
                "audit_required": True,
                "journal_session_id": session_id,
                "journal_schema_version": 1,
            },
        )
    except BaseException as exc:
        await fail_audited_bootstrap(state, exc)
    return state


async def fail_audited_bootstrap(
    state: AuditedSessionState,
    cause: BaseException,
) -> None:
    """唯一 finish 路径收敛 root error/session end，再抛稳定创建错误。"""
    terminal = ThreadTerminalRequest(
        thread_id=state.thread_id,
        status="error",
        end_reason="engine_bootstrap_failed",
        stable_error=StableErrorV1(
            code="audit_engine_bootstrap_failed",
            class_name="AuditEngineBootstrapFailure",
            failure_class="bootstrap",
            retryable=False,
        ),
    )
    result = await state.coordinator.finish(
        thread_terminals=(terminal,),
        reason="engine_bootstrap_failed",
        status="error",
    )
    raise AuditEngineCreationError(
        state.coordinator.session_id,
        finish_result=result,
    ) from cause


def _session_descriptor(
    *,
    config: AuditConfig,
    session_id: str,
    thread_id: str,
    entry_skill_id: str,
    cwd: str | None,
) -> SessionDescriptor:
    """构造不含任意对象、repr 或环境值的 canonical bootstrap descriptor。"""
    extra: dict[str, Any] = {"cwd": cwd} if cwd is not None else {}
    return SessionDescriptor(
        session_id=session_id,
        creation_operation_id=f"{session_id}:create",
        writer_id=config.writer_id,
        root_thread=RootThreadDescriptor(
            thread_id=thread_id,
            entry_skill_id=entry_skill_id,
            source=f"session:{session_id}",
            extra=extra,
        ),
        config={
            "audit_required": True,
            "journal_schema_version": 1,
            "strict_mode": "session_journal_business_v1",
            "max_attachment_bytes": config.max_attachment_bytes,
            "max_total_attachment_bytes": config.max_total_attachment_bytes,
        },
    )


__all__ = [
    "AuditDowngradeError",
    "AuditEngineCreationError",
    "AuditedSessionState",
    "bootstrap_audited_session",
    "ensure_legacy_resume_allowed",
    "fail_audited_bootstrap",
    "validate_pool_audit",
]
