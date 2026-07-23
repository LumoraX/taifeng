"""strict SessionJournal business 模式的注入配置与静态能力门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import (
        JournalAck,
        JournalRecord,
        SessionCreateResult,
        SessionDescriptor,
        SessionLease,
    )
    from taifeng.llm.client import ModelClient
    from taifeng.skill.registry import SkillSnapshot
    from taifeng.tool.spec import ToolSpec


class AuditJournalCore(Protocol):
    """strict bootstrap 与 coordinator 所需的最小 Journal core 边界。"""

    async def create_session(
        self,
        descriptor: SessionDescriptor,
    ) -> SessionCreateResult:
        """原子创建一个新 Session Journal。"""
        ...

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """按 expected seq 追加一个 durable batch。"""
        ...

    async def close_session(self, lease: SessionLease) -> None:
        """只释放指定 Session 的 writer lease。"""
        ...


@dataclass(frozen=True, slots=True)
class ModelAttemptCapability:
    """绑定具体 ModelClient 的静态 attempt 边界声明。

    本类型只表达 provider 已知的“一次 stream 对应一次网络 attempt”边界，不实现
    observer、dispatch 或网络逻辑。Task 7 接入 observer 后将消费同一显式边界。
    """

    client: ModelClient
    mode: Literal["one_attempt_per_stream"] = "one_attempt_per_stream"

    @classmethod
    def declare_single_attempt(cls, client: ModelClient) -> ModelAttemptCapability:
        """为一个具体 client 显式声明单 attempt stream 能力。"""
        return cls(client=client)


@dataclass(frozen=True, slots=True)
class AuditCapabilities:
    """EnginePool 后续可从全部注入依赖构造的不可变静态能力快照。"""

    model_client: ModelClient
    skill_snapshot: SkillSnapshot
    model_attempt: ModelAttemptCapability | None = None
    tools: tuple[ToolSpec, ...] = ()
    resume_thread_id: str | None = None
    custom_store: object | None = None
    custom_directory: object | None = None
    index_hook: object | None = None
    hooks: object | None = None
    permission_policy: object | None = None
    hitl_prompter: object | None = None
    compressor: object | None = None
    memory_store: object | None = None
    memory_query_builder: object | None = None
    pinned_state_sources: tuple[object, ...] = ()
    instruction_layers: tuple[object, ...] = ()
    detached_spawn_enabled: bool = False
    barrier_enabled: bool = False
    peer_messaging_enabled: bool = False


@dataclass(frozen=True, slots=True)
class AuditConfig:
    """strict audit bootstrap 的全部显式注入配置。"""

    journal_core: AuditJournalCore
    writer_id: str
    max_attachment_bytes: int
    max_total_attachment_bytes: int
    capabilities: AuditCapabilities

    def __post_init__(self) -> None:
        """拒绝不能形成稳定 bootstrap/附件边界的基础配置。"""
        if not self.writer_id:
            raise ValueError("audit_writer_id_empty")
        if self.max_attachment_bytes <= 0:
            raise ValueError("audit_attachment_limit_invalid")
        if self.max_total_attachment_bytes <= 0:
            raise ValueError("audit_total_attachment_limit_invalid")


class AuditCapabilityError(ValueError):
    """静态 strict audit capability 不受支持。"""

    def __init__(self, code: str) -> None:
        """只暴露可稳定断言的错误 code。"""
        super().__init__(code)
        self.code = code


_OBJECT_CAPABILITY_RULES = (
    ("resume_thread_id", "audit_resume_unsupported"),
    ("custom_store", "audit_custom_store_unsupported"),
    ("custom_directory", "audit_custom_directory_unsupported"),
    ("index_hook", "audit_index_hook_unsupported"),
    ("hooks", "audit_hooks_unsupported"),
    ("permission_policy", "audit_permission_unsupported"),
    ("hitl_prompter", "audit_hitl_unsupported"),
    ("compressor", "audit_compressor_unsupported"),
    ("memory_store", "audit_memory_unsupported"),
    ("memory_query_builder", "audit_memory_query_builder_unsupported"),
)

_COLLECTION_CAPABILITY_RULES = (
    ("pinned_state_sources", "audit_pinned_state_unsupported"),
    ("instruction_layers", "audit_instruction_layers_unsupported"),
)

_BOOLEAN_CAPABILITY_RULES = (
    ("detached_spawn_enabled", "audit_spawn_unsupported"),
    ("barrier_enabled", "audit_barrier_unsupported"),
    ("peer_messaging_enabled", "audit_peer_unsupported"),
)

_SPAWN_TOOL_NAMES = frozenset({"spawn_skill", "kill_skill", "run_in_background"})
_BARRIER_TOOL_NAMES = frozenset({"await_skills", "join_skill", "wait_for_task"})
_PEER_TOOL_NAMES = frozenset({"send_message", "wait_peer"})
_EFFECT_KINDS = frozenset({"read", "write", "external"})
_RECONCILIATION_MODES = frozenset({"none", "idempotency_key", "manual"})


def validate_audit_config(config: AuditConfig | None) -> None:
    """验证 strict audit 静态能力；``None`` 保持 legacy 路径无变化。"""
    if config is None:
        return
    capabilities = config.capabilities
    _validate_unsupported_fields(capabilities)
    _validate_model_capability(capabilities)
    _validate_skill_capability(capabilities)
    _validate_tool_capabilities(capabilities.tools)


def _validate_unsupported_fields(capabilities: AuditCapabilities) -> None:
    """按稳定优先级拒绝未接入的 store/context/spawn 能力。"""
    for field_name, code in _OBJECT_CAPABILITY_RULES:
        if getattr(capabilities, field_name) is not None:
            raise AuditCapabilityError(code)
    for field_name, code in _COLLECTION_CAPABILITY_RULES:
        if getattr(capabilities, field_name):
            raise AuditCapabilityError(code)
    for field_name, code in _BOOLEAN_CAPABILITY_RULES:
        if getattr(capabilities, field_name) is True:
            raise AuditCapabilityError(code)


def _validate_model_capability(capabilities: AuditCapabilities) -> None:
    """只接受绑定当前 client 的显式静态 attempt 声明。"""
    marker = capabilities.model_attempt
    if not isinstance(marker, ModelAttemptCapability):
        raise AuditCapabilityError("audit_model_attempt_unobservable")
    if marker.client is not capabilities.model_client:
        raise AuditCapabilityError("audit_model_attempt_client_mismatch")


def _validate_skill_capability(capabilities: AuditCapabilities) -> None:
    """拒绝 snapshot 中任一已加载的声明式 orchestration。"""
    if any(skill.orchestration is not None for skill in capabilities.skill_snapshot.skills):
        raise AuditCapabilityError("audit_orchestration_unsupported")


def _validate_tool_capabilities(tools: tuple[ToolSpec, ...]) -> None:
    """拒绝 spawn/peer Tool，并校验 strict audit metadata。"""
    names = frozenset(tool.name for tool in tools)
    if names & _SPAWN_TOOL_NAMES:
        raise AuditCapabilityError("audit_spawn_unsupported")
    if names & _BARRIER_TOOL_NAMES:
        raise AuditCapabilityError("audit_barrier_unsupported")
    if names & _PEER_TOOL_NAMES:
        raise AuditCapabilityError("audit_peer_unsupported")
    for tool in tools:
        _validate_tool_metadata(tool)


def _validate_tool_metadata(tool: ToolSpec) -> None:
    """校验单个 Tool 的 effect/reconciliation/suspension 声明。"""
    if tool.effect_kind is None:
        raise AuditCapabilityError("audit_tool_effect_kind_missing")
    if tool.effect_kind not in _EFFECT_KINDS:
        raise AuditCapabilityError("audit_tool_effect_kind_invalid")
    if tool.reconciliation is None:
        raise AuditCapabilityError("audit_tool_reconciliation_missing")
    if tool.reconciliation not in _RECONCILIATION_MODES:
        raise AuditCapabilityError("audit_tool_reconciliation_invalid")
    if tool.can_suspend is None:
        raise AuditCapabilityError("audit_tool_suspension_metadata_missing")
    if tool.can_suspend is not False:
        raise AuditCapabilityError("audit_tool_suspension_unsupported")


__all__ = [
    "AuditCapabilities",
    "AuditCapabilityError",
    "AuditConfig",
    "AuditJournalCore",
    "ModelAttemptCapability",
    "validate_audit_config",
]
