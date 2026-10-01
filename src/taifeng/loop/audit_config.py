"""strict SessionJournal business 模式的注入配置与静态能力门禁。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import MemberDescriptorType
from typing import TYPE_CHECKING, Literal

from taifeng.conversation.journal.backend import SessionJournalCore
from taifeng.llm.audit import (
    AttemptObservableClientAdapter,
    AttemptObservableModelClient,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from taifeng.llm.client import ModelClient
    from taifeng.loop.audit_resume_resolution import AuditToolOutcomeResolver
    from taifeng.skill.registry import SkillSnapshot


# 协议的定义在 journal 包里（后端 seam，ADR 0114）；这里保留旧名
AuditJournalCore = SessionJournalCore


@dataclass(frozen=True, slots=True)
class AuditConfig:
    """调用方注入的 strict audit per-pool 配置。

    ``tool_outcome_resolver``：resume 时对「结果未知、回查也查不清」的工具调用征求人的裁决
    （ADR 0070）。None = 不问人，这类调用令 resume 以 ``audit_resume_recovery_required`` 拒绝。
    """

    journal_core: SessionJournalCore
    writer_id: str
    max_attachment_bytes: int
    max_total_attachment_bytes: int
    tool_outcome_resolver: AuditToolOutcomeResolver | None = None

    def __post_init__(self) -> None:
        """拒绝不能形成稳定 bootstrap/附件边界的基础配置。"""
        if type(self.writer_id) is not str or not self.writer_id:
            raise ValueError("audit_writer_id_empty")
        if self.tool_outcome_resolver is not None and not callable(self.tool_outcome_resolver):
            raise ValueError("audit_tool_outcome_resolver_invalid")
        if (
            type(self.max_attachment_bytes) is not int
            or self.max_attachment_bytes <= 0
        ):
            raise ValueError("audit_attachment_limit_invalid")
        if (
            type(self.max_total_attachment_bytes) is not int
            or self.max_total_attachment_bytes <= 0
        ):
            raise ValueError("audit_total_attachment_limit_invalid")


@dataclass(frozen=True, slots=True)
class AuditStaticInputs:
    """EnginePool 调用点提供的实际 resolved dependencies 快照。"""

    model_client: ModelClient
    skill_snapshot: SkillSnapshot
    tools: tuple[object, ...] = ()
    custom_store: object | None = None
    custom_directory: object | None = None
    index_hook: object | None = None
    hooks: object | None = None
    permission_policy: object | None = None
    permission_prompter: object | None = None
    hitl_enabled: bool = False
    compressor: object | None = None
    memory_store: object | None = None
    memory_query_builder: object | None = None
    pinned_state_sources: tuple[object, ...] = ()
    instruction_layers: tuple[object, ...] = ()
    detached_spawn_enabled: bool = False
    barrier_enabled: bool = False
    peer_messaging_enabled: bool = False
    failure_policy: object | None = None
    failure_suspension_enabled: bool = True
    failure_suspend_ttl_seconds: int | None = None
    failure_suspend_max_auto_retries: int | None = None
    failure_suspend_on_expire: Literal["abort", "retry"] = "abort"
    skill_suspension_enabled: bool = True
    skill_authorization: object | None = None
    skill_working_set: object | None = None
    context_engine: object | None = None


class AuditCapabilityError(ValueError):
    """静态 strict audit capability 不受支持。"""

    def __init__(self, code: str) -> None:
        """只暴露可稳定断言的错误 code。"""
        super().__init__(code)
        self.code = code


type AuditToolEffectKind = Literal[
    "pure",
    "idempotent",
    "reconcilable",
    "external_non_idempotent",
]
"""ADR 0025 定义的稳定 Tool effect 分类。"""

type AuditToolReconciliationMode = Literal["none", "query", "retry", "manual"]
"""strict audit Tool 恢复策略的有限集合。"""

AUDIT_TOOL_EFFECT_KINDS: frozenset[AuditToolEffectKind] = frozenset(
    {"pure", "idempotent", "reconcilable", "external_non_idempotent"}
)
"""Task 8.2 可复用的稳定 effect kind 集合。"""

AUDIT_TOOL_RECONCILIATION_MODES: frozenset[AuditToolReconciliationMode] = (
    frozenset({"none", "query", "retry", "manual"})
)
"""Task 8.2 可复用的稳定 reconciliation mode 集合。"""

AUDIT_TOOL_EFFECT_RECONCILIATION: frozenset[
    tuple[AuditToolEffectKind, AuditToolReconciliationMode]
] = frozenset(
    {
        ("pure", "none"),
        ("idempotent", "retry"),
        ("reconcilable", "query"),
        ("reconcilable", "manual"),
        ("external_non_idempotent", "manual"),
    }
)
"""Task 8.2 可复用的 effect kind / reconciliation 合法组合。"""


_OBJECT_CAPABILITY_RULES = (
    ("custom_store", "audit_custom_store_unsupported"),
    ("custom_directory", "audit_custom_directory_unsupported"),
    ("index_hook", "audit_index_hook_unsupported"),
    ("hooks", "audit_hooks_unsupported"),
    ("permission_policy", "audit_permission_unsupported"),
    ("permission_prompter", "audit_hitl_unsupported"),
    ("context_engine", "audit_context_engine_unsupported"),
    ("compressor", "audit_compressor_unsupported"),
    ("memory_store", "audit_memory_unsupported"),
    ("memory_query_builder", "audit_memory_query_builder_unsupported"),
    ("failure_policy", "audit_failure_policy_unsupported"),
    ("skill_authorization", "audit_skill_authorization_unsupported"),
    ("skill_working_set", "audit_skill_working_set_unsupported"),
)

_COLLECTION_CAPABILITY_RULES = (
    ("pinned_state_sources", "audit_pinned_state_unsupported"),
    ("instruction_layers", "audit_instruction_layers_unsupported"),
)

_BOOLEAN_CAPABILITY_RULES = (
    ("hitl_enabled", "audit_hitl_unsupported"),
    ("detached_spawn_enabled", "audit_spawn_unsupported"),
    ("barrier_enabled", "audit_barrier_unsupported"),
    ("peer_messaging_enabled", "audit_peer_unsupported"),
    ("failure_suspension_enabled", "audit_failure_suspension_unsupported"),
    ("skill_suspension_enabled", "audit_skill_suspension_unsupported"),
)

# 分离式派发（spawn_skill / kill_skill / join_skill / wait_peer / wait_any）、join-barrier
# （await_skills）与 peer 消息（send_message）已接入（ADR 0098 / 0099 / 0100）；
# 仍在能力面之外的是后台 shell 任务。
_SPAWN_TOOL_NAMES = frozenset({"run_in_background"})
_BARRIER_TOOL_NAMES = frozenset({"wait_for_task"})
_PEER_TOOL_NAMES: frozenset[str] = frozenset()
_MISSING = object()


def validate_audit_config(
    config: AuditConfig | None,
    *,
    static_inputs: AuditStaticInputs | None = None,
) -> None:
    """验证调用点传入的 resolved dependencies；不访问 Journal core。"""
    if config is None:
        return
    if static_inputs is None:
        raise AuditCapabilityError("audit_static_inputs_required")
    _validate_unsupported_fields(static_inputs)
    _validate_model_capability(static_inputs)
    _validate_skill_capability(static_inputs)
    _validate_tool_capabilities(static_inputs.tools)


def _validate_unsupported_fields(inputs: AuditStaticInputs) -> None:
    """按稳定优先级拒绝未接入的 store/context/suspension 能力。"""
    for field_name, code in _OBJECT_CAPABILITY_RULES:
        value = getattr(inputs, field_name)
        if value is None:
            continue
        admit = _ADMISSION_CHECKS.get(field_name)
        if admit is not None and admit(value):
            continue
        raise AuditCapabilityError(code)
    for field_name, code in _COLLECTION_CAPABILITY_RULES:
        if getattr(inputs, field_name):
            raise AuditCapabilityError(code)
    for field_name, code in _BOOLEAN_CAPABILITY_RULES:
        if getattr(inputs, field_name) is True:
            raise AuditCapabilityError(code)
    if (
        inputs.failure_suspend_ttl_seconds is not None
        or inputs.failure_suspend_max_auto_retries is not None
        or inputs.failure_suspend_on_expire != "abort"
    ):
        raise AuditCapabilityError("audit_failure_suspension_unsupported")


_AUDITABLE_COMPRESSION = frozenset({"fold", "fold_model"})


def _compressor_is_auditable(compressor: object) -> bool:
    """压缩协调器里的每个策略都声明了折叠式的审计支持（ADR 0094）。

    声明读类属性 ``audit_support``，不执行任何 descriptor；没有策略的协调器无事可审。
    """
    strategies = _static_tool_attribute(compressor, "strategies")
    if strategies is _MISSING:
        strategies = getattr(compressor, "strategies", _MISSING)
    if not isinstance(strategies, tuple):
        return False
    return all(
        inspect.getattr_static(type(strategy), "audit_support", None) in _AUDITABLE_COMPRESSION
        for strategy in strategies
    )


def _hooks_are_auditable(hooks: object) -> bool:
    """hook 运行器是内核的 ``HookRunner``：裁决可以按 turn 绑定落账（ADR 0096）。"""
    from taifeng.hooks.types import HookRunner

    return isinstance(hooks, HookRunner)


def _permission_is_auditable(policy: object) -> bool:
    """权限策略是内核的 ``PermissionPolicy``（ADR 0096）。

    以挂起的方式征求审批同样可用：挂起与恢复本身进 Journal（ADR 0097）。
    """
    from taifeng.permission.policy import PermissionPolicy

    return isinstance(policy, PermissionPolicy)


# 字段 → 「这个具体的值可以在审计模式下使用」的判定；没有列出的字段一律拒绝
_ADMISSION_CHECKS: dict[str, Callable[[object], bool]] = {
    "compressor": _compressor_is_auditable,
    "hooks": _hooks_are_auditable,
    "permission_policy": _permission_is_auditable,
}


def _validate_model_capability(inputs: AuditStaticInputs) -> None:
    """只接受不可覆盖的 exact 官方 observer adapter。"""
    if type(inputs.model_client) is not AttemptObservableClientAdapter:
        raise AuditCapabilityError("audit_model_attempt_unobservable")


def _validate_skill_capability(inputs: AuditStaticInputs) -> None:
    """拒绝 snapshot 中任一已加载的声明式 orchestration。"""
    if any(skill.orchestration is not None for skill in inputs.skill_snapshot.skills):
        raise AuditCapabilityError("audit_orchestration_unsupported")


def _validate_tool_capabilities(tools: tuple[object, ...]) -> None:
    """拒绝静态 Tool 能力并 fail-closed 校验内部 audit metadata view。"""
    for tool in tools:
        name = _static_tool_attribute(tool, "name")
        if type(name) is not str:
            raise AuditCapabilityError("audit_tool_metadata_incomplete")
        if name in _SPAWN_TOOL_NAMES:
            raise AuditCapabilityError("audit_spawn_unsupported")
        if name in _BARRIER_TOOL_NAMES:
            raise AuditCapabilityError("audit_barrier_unsupported")
        if name in _PEER_TOOL_NAMES:
            raise AuditCapabilityError("audit_peer_unsupported")
        _validate_tool_metadata(tool)


def _validate_tool_metadata(tool: object) -> None:
    """按 ADR 0025 校验单个 Tool metadata，不执行任意 descriptor。"""
    effect_kind = _static_tool_attribute(tool, "effect_kind")
    idempotency_key = _static_tool_attribute(tool, "idempotency_key")
    reconciliation = _static_tool_attribute(tool, "reconciliation")
    can_suspend = _static_tool_attribute(tool, "can_suspend")
    if any(
        value is _MISSING
        for value in (effect_kind, idempotency_key, reconciliation, can_suspend)
    ):
        raise AuditCapabilityError("audit_tool_metadata_incomplete")
    if (
        type(effect_kind) is not str
        or effect_kind not in AUDIT_TOOL_EFFECT_KINDS
    ):
        raise AuditCapabilityError("audit_tool_effect_kind_invalid")
    if idempotency_key is not None and (
        type(idempotency_key) is not str or not idempotency_key
    ):
        raise AuditCapabilityError("audit_tool_metadata_incomplete")
    if (
        type(reconciliation) is not str
        or reconciliation not in AUDIT_TOOL_RECONCILIATION_MODES
        or (effect_kind, reconciliation) not in AUDIT_TOOL_EFFECT_RECONCILIATION
    ):
        raise AuditCapabilityError("audit_tool_reconciliation_invalid")
    if type(can_suspend) is not bool:
        raise AuditCapabilityError("audit_tool_metadata_incomplete")


def _static_tool_attribute(tool: object, attribute: str) -> object:
    """静态读取实例字段；property/任意 descriptor 不执行并视为缺失。"""
    value = inspect.getattr_static(tool, attribute, _MISSING)
    if isinstance(value, MemberDescriptorType):
        try:
            return value.__get__(tool, type(tool))
        except AttributeError:
            return _MISSING
    if isinstance(value, property) or inspect.isroutine(value):
        return _MISSING
    return value


__all__ = [
    "AUDIT_TOOL_EFFECT_KINDS",
    "AUDIT_TOOL_EFFECT_RECONCILIATION",
    "AUDIT_TOOL_RECONCILIATION_MODES",
    "AttemptObservableModelClient",
    "AuditCapabilityError",
    "AuditConfig",
    "AuditJournalCore",
    "AuditStaticInputs",
    "AuditToolEffectKind",
    "AuditToolReconciliationMode",
    "validate_audit_config",
]
