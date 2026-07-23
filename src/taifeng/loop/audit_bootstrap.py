"""EnginePool 的 strict audit Journal-first bootstrap 与 downgrade 门禁。"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.conversation.journal.models import (
    Durability,
    JournalAck,
    RootThreadDescriptor,
    SessionCreateResult,
    SessionDescriptor,
    SessionLease,
    build_initialization_records,
)
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.journal.records import StableErrorV1
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
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.llm.client import ModelClient
    from taifeng.skill.registry import SkillSnapshot


@dataclass(frozen=True, slots=True)
class AuditedSessionState:
    """EnginePool 持有的 audited Session bootstrap ownership。"""

    thread_id: str
    coordinator: SessionAuditCoordinator
    projector: JournalConversationProjector


@dataclass(frozen=True, slots=True)
class AuditStoreBinding:
    """EnginePool 从 nominal store 类型冻结出的审计依赖。"""

    projection_store: JsonlMessageStore | None
    custom_store: object | None
    custom_directory: object | None
    index_hook: object | None


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


class AuditSessionReleaseError(RuntimeError):
    """audited Session 未能同时完成 terminal durable ack 与 lease 释放。"""

    code = "audit_session_release_incomplete"

    def __init__(
        self,
        session_id: str,
        *,
        finish_result: SessionFinishResult,
    ) -> None:
        """仅暴露稳定 code/session 与 coordinator 防御性结果。"""
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


_HASH_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _InvalidCreateResultError(RuntimeError):
    """Journal core 返回值不满足 bootstrap trust boundary。"""


def validate_pool_audit(
    config: AuditConfig | None,
    *,
    model_client: ModelClient,
    skill_snapshot: SkillSnapshot,
    tools: Iterable[object],
    store_binding: AuditStoreBinding,
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
    if config is None:
        return
    validate_audit_config(
        config,
        static_inputs=AuditStaticInputs(
            model_client=model_client,
            skill_snapshot=skill_snapshot,
            tools=tuple(tools),
            custom_store=store_binding.custom_store,
            custom_directory=store_binding.custom_directory,
            index_hook=store_binding.index_hook,
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
    projection_store: JsonlMessageStore | None,
    thread_id: str,
) -> None:
    """metadata-only 检查 audited marker，禁止 legacy history load 降级。"""
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
    projection_store: JsonlMessageStore | None,
    session_id: str,
    entry_skill_id: str,
    cwd: str | None,
) -> AuditedSessionState:
    """按 Journal→coordinator→projection 顺序建立 audited Session。"""
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
    created = await _validated_create_result(config, descriptor, created)
    coordinator = SessionAuditCoordinator(
        core=config.journal_core,
        lease=created.lease,
        expected_seq=created.ack.last_seq,
    )
    projector = JournalConversationProjector(projection_store)
    state = AuditedSessionState(thread_id, coordinator, projector)
    try:
        projected_thread_id = await projector.bootstrap_thread(
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
        if projected_thread_id != thread_id:
            raise _InvalidCreateResultError
    except BaseException as exc:
        await fail_audited_bootstrap(
            state,
            exc,
            reason="projection_bootstrap_failed",
        )
    return state


async def fail_audited_bootstrap(
    state: AuditedSessionState,
    cause: BaseException,
    *,
    reason: str = "engine_bootstrap_failed",
) -> None:
    """唯一 finish 路径收敛 root error/session end，再抛稳定创建错误。"""
    terminal = ThreadTerminalRequest(
        thread_id=state.thread_id,
        status="error",
        end_reason=reason,
        stable_error=StableErrorV1(
            code=reason,
            class_name=_bootstrap_failure_class(reason),
            failure_class="bootstrap",
            retryable=False,
        ),
    )
    result = await state.coordinator.finish(
        thread_terminals=(terminal,),
        reason=reason,
        status="error",
    )
    raise AuditEngineCreationError(
        state.coordinator.session_id,
        finish_result=result,
    ) from cause


async def _validated_create_result(
    config: AuditConfig,
    descriptor: SessionDescriptor,
    result: object,
) -> SessionCreateResult:
    """先建立可信 lease，再精确重验初始化 ack；失败不泄漏执行能力。"""
    lease = _copy_trusted_lease(config, descriptor, result)
    if lease is None:
        raise AuditEngineCreationError(descriptor.session_id) from (
            _InvalidCreateResultError()
        )
    assert isinstance(result, SessionCreateResult)
    try:
        ack = _copy_initialization_ack(descriptor, lease, result)
    except _InvalidCreateResultError as exc:
        await _emergency_close(config, lease)
        raise AuditEngineCreationError(descriptor.session_id) from exc
    return SessionCreateResult(lease=lease, ack=ack)


def _copy_trusted_lease(
    config: AuditConfig,
    descriptor: SessionDescriptor,
    result: object,
) -> SessionLease | None:
    """只复制 exact、与本次初始化身份一致的首个 writer lease。"""
    if type(result) is not SessionCreateResult:
        return None
    lease = result.lease
    if type(lease) is not SessionLease:
        return None
    valid = (
        type(lease.session_id) is str
        and lease.session_id == descriptor.session_id
        and type(lease.writer_id) is str
        and lease.writer_id == config.writer_id
        and type(lease.writer_epoch) is int
        and lease.writer_epoch == 1
        and type(lease.lease_id) is str
        and bool(lease.lease_id)
    )
    if not valid:
        return None
    return SessionLease(
        session_id=lease.session_id,
        writer_id=lease.writer_id,
        writer_epoch=lease.writer_epoch,
        lease_id=lease.lease_id,
    )


def _copy_initialization_ack(
    descriptor: SessionDescriptor,
    lease: SessionLease,
    result: SessionCreateResult,
) -> JournalAck:
    """精确验证三记录初始化 ack，并重建隔离副本。"""
    ack = result.ack
    if type(ack) is not JournalAck:
        raise _InvalidCreateResultError
    expected_ids = tuple(
        record.record_id for record in build_initialization_records(descriptor)
    )
    fields_valid = (
        type(ack.session_id) is str
        and type(ack.first_seq) is int
        and type(ack.last_seq) is int
        and type(ack.record_ids) is tuple
        and all(type(record_id) is str for record_id in ack.record_ids)
        and type(ack.tail_hash) is str
        and _HASH_HEX_PATTERN.fullmatch(ack.tail_hash) is not None
        and type(ack.writer_epoch) is int
        and type(ack.durability) is Durability
    )
    values_valid = (
        ack.session_id == lease.session_id
        and ack.first_seq == 1
        and ack.last_seq == 3
        and ack.record_ids == expected_ids
        and ack.writer_epoch == lease.writer_epoch
        and ack.durability is Durability.COMMITTED
    )
    if not fields_valid or not values_valid:
        raise _InvalidCreateResultError
    return JournalAck(
        session_id=ack.session_id,
        first_seq=ack.first_seq,
        last_seq=ack.last_seq,
        record_ids=ack.record_ids,
        tail_hash=ack.tail_hash,
        writer_epoch=ack.writer_epoch,
        durability=ack.durability,
    )


async def _emergency_close(config: AuditConfig, lease: SessionLease) -> None:
    """ack 不可信时 bounded/shielded 释放唯一可信 lease。"""
    with anyio.move_on_after(5.0, shield=True):
        try:
            await config.journal_core.close_session(lease)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001
            return


def _bootstrap_failure_class(reason: str) -> str:
    """把有限 bootstrap reason 映射为不含任意异常文本的类名。"""
    if reason == "projection_bootstrap_failed":
        return "ProjectionBootstrapFailure"
    return "EngineBootstrapFailure"


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
    "AuditStoreBinding",
    "AuditDowngradeError",
    "AuditEngineCreationError",
    "AuditSessionReleaseError",
    "AuditedSessionState",
    "bootstrap_audited_session",
    "ensure_legacy_resume_allowed",
    "fail_audited_bootstrap",
    "validate_pool_audit",
]
