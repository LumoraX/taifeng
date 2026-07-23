"""strict SessionJournal audit 静态 capability gate 测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest

from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_config import (
    AttemptObservableModelClient,
    AuditCapabilityError,
    AuditConfig,
    AuditStaticInputs,
    validate_audit_config,
    validate_audit_session_request,
)
from taifeng.skill.definition import SkillDefinition, SkillType
from taifeng.skill.orchestration import OrchestrationSpec, SerialStep
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import (
        JournalAck,
        JournalRecord,
        SessionCreateResult,
        SessionDescriptor,
        SessionLease,
    )
    from taifeng.llm.client import ModelClientSession
    from taifeng.loop.cancellation import CancellationToken


class _JournalCore:
    """不会在静态验证中触发 IO 的 Journal core fake。"""

    async def create_session(
        self,
        descriptor: SessionDescriptor,
    ) -> SessionCreateResult:
        """静态测试禁止触发 Journal create。"""
        raise AssertionError(descriptor)

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """静态测试禁止触发 Journal append。"""
        raise AssertionError(records, lease, expected_seq)

    async def close_session(self, lease: SessionLease) -> None:
        """静态测试禁止触发 Journal close。"""
        raise AssertionError(lease)


class _ObservedClient(AttemptObservableModelClient):
    """显式实现 observer-aware session API 的 nominal 测试 client。"""

    def __init__(self) -> None:
        """用 SimClient 提供不联网的普通 session。"""
        self._inner = SimClient(turns=[SimTurn(text="ok")])

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """实现 legacy ModelClient session 边界。"""
        return self._inner.session(cancel=cancel, model=model)

    def session_with_attempt_observer(
        self,
        *,
        cancel: CancellationToken,
        attempt_observer: object,
        model: str | None = None,
    ) -> ModelClientSession:
        """显式承载未来 Task 7 observer 注入点，测试不执行网络 dispatch。"""
        del attempt_observer
        return self._inner.session(cancel=cancel, model=model)


class _DuckObservedClient:
    """拥有同名方法但未继承 nominal 边界的普通对象。"""

    def session(self, **kwargs: object) -> object:
        """伪造普通 session 属性。"""
        return kwargs

    def session_with_attempt_observer(self, **kwargs: object) -> object:
        """伪造 observer-aware 属性。"""
        return kwargs


class _DescriptorObservedClient(AttemptObservableModelClient):
    """用 property 伪装 observer-aware method 的真实 subclass。"""

    def __init__(self) -> None:
        self._inner = SimClient(turns=[SimTurn(text="descriptor")])

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """实现普通 session。"""
        return self._inner.session(cancel=cancel, model=model)

    @property
    def session_with_attempt_observer(self) -> object:  # type: ignore[override]
        """若 validator 执行 descriptor，测试必须立即失败。"""
        raise AssertionError("descriptor must not execute")


@dataclass(frozen=True, slots=True)
class _AuditedTool:
    """Task 5 validator 使用的完整内部 tool metadata view。"""

    name: str = "safe_tool"
    effect_kind: Literal[
        "pure",
        "idempotent",
        "reconcilable",
        "external_non_idempotent",
    ] = "pure"
    idempotency_key: str | None = None
    reconciliation: str = "none"
    can_suspend: bool = False


async def _legacy_tool_handler(
    args: dict[str, Any],
    ctx: ToolContext,
) -> ToolResult:
    """返回不产生 effect 的固定 ToolResult。"""
    del args, ctx
    return ToolResult.ok("ok")


def _legacy_tool(name: str = "legacy_tool") -> ToolSpec:
    """按 Task 5 前的原始签名构造 legacy ToolSpec。"""
    return ToolSpec(
        name=name,
        description="legacy",
        input_schema={"type": "object"},
        handler=_legacy_tool_handler,
    )


def _skill(
    skill_id: str,
    *,
    skill_type: SkillType = "atomic",
    child_skills: frozenset[str] = frozenset(),
    orchestration: OrchestrationSpec | None = None,
) -> SkillDefinition:
    """构造 validator 使用的最小 SkillDefinition。"""
    return SkillDefinition(
        id=skill_id,
        name=skill_id,
        description=skill_id,
        version="1",
        body="# body",
        body_path=Path(f"/{skill_id}/SKILL.md"),
        type=skill_type,
        child_skills=child_skills,
        orchestration=orchestration,
    )


def _static_inputs() -> AuditStaticInputs:
    """构造 strict audit 可接受的 resolved 依赖快照。"""
    return AuditStaticInputs(
        model_client=_ObservedClient(),
        skill_snapshot=SkillSnapshot(
            version=1,
            skills=(_skill("atomic"),),
        ),
        tools=(_AuditedTool(),),
        failure_suspension_enabled=False,
        skill_suspension_enabled=False,
    )


def _config() -> AuditConfig:
    """构造只含真正 audit 注入值的最小配置。"""
    return AuditConfig(
        journal_core=_JournalCore(),
        writer_id="writer-1",
        max_attachment_bytes=1024,
        max_total_attachment_bytes=4096,
    )


def test_config_has_no_shadow_capability_or_resume_fields() -> None:
    """AuditConfig 不复制 Pool resolved dependencies 或 per-session resume。"""
    assert {field.name for field in fields(AuditConfig)} == {
        "journal_core",
        "writer_id",
        "max_attachment_bytes",
        "max_total_attachment_bytes",
    }


def test_resolved_inputs_are_validated_instead_of_config_shadow() -> None:
    """validator 直接消费调用点传入的实际 resolved hooks。"""
    config = _config()
    valid = _static_inputs()

    validate_audit_config(config, static_inputs=valid)
    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(
            config,
            static_inputs=replace(valid, hooks=object()),
        )

    assert caught.value.code == "audit_hooks_unsupported"


def test_audit_session_resume_is_rejected_by_separate_request_gate() -> None:
    """per-session resume 在 get_or_create 生命周期门禁独立拒绝。"""
    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_session_request(_config(), resume_thread_id="old-thread")

    assert caught.value.code == "audit_resume_unsupported"


def test_new_audit_session_request_passes() -> None:
    """新 Session 请求不携带 resume 时通过 request gate。"""
    validate_audit_session_request(_config(), resume_thread_id=None)


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        ({"custom_store": object()}, "audit_custom_store_unsupported"),
        ({"custom_directory": object()}, "audit_custom_directory_unsupported"),
        ({"index_hook": object()}, "audit_index_hook_unsupported"),
        ({"hooks": object()}, "audit_hooks_unsupported"),
        ({"permission_policy": object()}, "audit_permission_unsupported"),
        ({"permission_prompter": object()}, "audit_hitl_unsupported"),
        ({"hitl_enabled": True}, "audit_hitl_unsupported"),
        ({"compressor": object()}, "audit_compressor_unsupported"),
        ({"memory_store": object()}, "audit_memory_unsupported"),
        (
            {"memory_query_builder": object()},
            "audit_memory_query_builder_unsupported",
        ),
        ({"pinned_state_sources": (object(),)}, "audit_pinned_state_unsupported"),
        (
            {"instruction_layers": (object(),)},
            "audit_instruction_layers_unsupported",
        ),
        ({"detached_spawn_enabled": True}, "audit_spawn_unsupported"),
        ({"barrier_enabled": True}, "audit_barrier_unsupported"),
        ({"peer_messaging_enabled": True}, "audit_peer_unsupported"),
        ({"failure_policy": object()}, "audit_failure_policy_unsupported"),
        (
            {"failure_suspension_enabled": True},
            "audit_failure_suspension_unsupported",
        ),
        (
            {"failure_suspend_ttl_seconds": 60},
            "audit_failure_suspension_unsupported",
        ),
        (
            {"failure_suspend_max_auto_retries": 2},
            "audit_failure_suspension_unsupported",
        ),
        (
            {"failure_suspend_on_expire": "retry"},
            "audit_failure_suspension_unsupported",
        ),
        (
            {"skill_suspension_enabled": True},
            "audit_skill_suspension_unsupported",
        ),
    ],
)
def test_unsupported_resolved_capability_has_stable_error_code(
    override: dict[str, Any],
    expected_code: str,
) -> None:
    """每项 unsupported resolved 依赖都以稳定 code 拒绝。"""
    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(
            _config(),
            static_inputs=replace(_static_inputs(), **override),
        )

    assert caught.value.code == expected_code
    assert str(caught.value) == expected_code


def test_ordinary_model_client_cannot_self_declare_observability() -> None:
    """普通 SimClient 无官方 helper，且不满足 nominal observer-aware 边界。"""
    assert not hasattr(AttemptObservableModelClient, "declare_single_attempt")
    inputs = replace(
        _static_inputs(),
        model_client=SimClient(turns=[SimTurn(text="opaque")]),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_model_attempt_unobservable"


def test_duck_observer_methods_do_not_satisfy_nominal_client_boundary() -> None:
    """同名方法不能绕过 nominal AttemptObservableModelClient 检查。"""
    inputs = replace(
        _static_inputs(),
        model_client=cast("Any", _DuckObservedClient()),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_model_attempt_unobservable"


def test_abc_virtual_subclass_registration_cannot_bypass_nominal_boundary() -> None:
    """ABC.register 不得把普通本地 dummy 变成真实 observer-aware client。"""

    class _VirtualClient:
        """仅用于本测试的 virtual subclass，避免污染生产 SimClient。"""

    AttemptObservableModelClient.register(_VirtualClient)
    inputs = replace(
        _static_inputs(),
        model_client=cast("Any", _VirtualClient()),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_model_attempt_unobservable"


def test_observer_aware_method_must_be_real_non_descriptor_implementation() -> None:
    """真实继承仍不能用 property 伪装 observer-aware method。"""
    inputs = replace(
        _static_inputs(),
        model_client=cast("Any", _DescriptorObservedClient()),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_model_attempt_unobservable"


@pytest.mark.parametrize(
    ("tool_name", "expected_code"),
    [
        ("spawn_skill", "audit_spawn_unsupported"),
        ("kill_skill", "audit_spawn_unsupported"),
        ("run_in_background", "audit_spawn_unsupported"),
        ("await_skills", "audit_barrier_unsupported"),
        ("join_skill", "audit_barrier_unsupported"),
        ("wait_for_task", "audit_barrier_unsupported"),
        ("send_message", "audit_peer_unsupported"),
        ("wait_peer", "audit_peer_unsupported"),
        ("request_user_input", "audit_hitl_unsupported"),
    ],
)
def test_registered_unsupported_tools_are_rejected(
    tool_name: str,
    expected_code: str,
) -> None:
    """已注册 Tool 名称足以暴露 detached/barrier/peer/HITL 能力。"""
    inputs = replace(
        _static_inputs(),
        tools=(_AuditedTool(name=tool_name),),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == expected_code


def test_current_legacy_tool_spec_is_metadata_incomplete() -> None:
    """Task 8.2 前的真实 ToolSpec 在 strict audit 中 fail closed。"""
    inputs = replace(_static_inputs(), tools=(_legacy_tool(),))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_metadata_incomplete"


def test_complete_non_suspending_tool_metadata_view_passes() -> None:
    """内部完整 metadata view 可通过，而无需提前修改 ToolSpec。"""
    validate_audit_config(
        _config(),
        static_inputs=replace(_static_inputs(), tools=(_AuditedTool(),)),
    )


def test_suspending_tool_metadata_view_is_rejected() -> None:
    """完整 metadata 仍必须显式声明 non-suspending。"""
    inputs = replace(
        _static_inputs(),
        tools=(_AuditedTool(can_suspend=True),),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_suspension_unsupported"


def test_tool_effect_kind_uses_adr_0025_taxonomy() -> None:
    """Task 5 只接受 ADR 0025 effect_kind，不重新发明 taxonomy。"""

    @dataclass(frozen=True)
    class _WrongTaxonomyTool:
        name: str = "wrong"
        effect_kind: str = "read"
        idempotency_key: str | None = None
        reconciliation: str = "none"
        can_suspend: bool = False

    inputs = replace(_static_inputs(), tools=(_WrongTaxonomyTool(),))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_effect_kind_invalid"


@pytest.mark.parametrize(
    ("effect_kind", "reconciliation"),
    [
        ("pure", "retry"),
        ("idempotent", "none"),
        ("reconcilable", "retry"),
        ("external_non_idempotent", "query"),
    ],
)
def test_tool_effect_and_reconciliation_combination_is_constrained(
    effect_kind: str,
    reconciliation: str,
) -> None:
    """effect kind 只接受契约规定的 recovery 策略组合。"""
    tool = replace(
        _AuditedTool(),
        effect_kind=cast("Any", effect_kind),
        reconciliation=reconciliation,
    )
    inputs = replace(_static_inputs(), tools=(tool,))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_reconciliation_invalid"


def test_garbage_tool_reconciliation_is_rejected() -> None:
    """任意非空字符串不能作为 reconciliation mode。"""
    tool = replace(_AuditedTool(), reconciliation="garbage")
    inputs = replace(_static_inputs(), tools=(tool,))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_reconciliation_invalid"


@pytest.mark.parametrize(
    ("effect_kind", "reconciliation"),
    [
        ("pure", "none"),
        ("idempotent", "retry"),
        ("reconcilable", "query"),
        ("reconcilable", "manual"),
        ("external_non_idempotent", "manual"),
    ],
)
def test_supported_tool_effect_and_reconciliation_combinations_pass(
    effect_kind: str,
    reconciliation: str,
) -> None:
    """ADR 0025 的有限 effect/recovery 组合可复用并稳定通过。"""
    tool = replace(
        _AuditedTool(),
        effect_kind=cast("Any", effect_kind),
        reconciliation=reconciliation,
    )
    validate_audit_config(
        _config(),
        static_inputs=replace(_static_inputs(), tools=(tool,)),
    )


def test_tool_metadata_properties_are_not_executed_during_static_validation() -> None:
    """metadata 读取不执行 property/任意 duck getter。"""

    class _PropertyTool:
        name = "property_tool"

        @property
        def effect_kind(self) -> str:
            raise AssertionError("property must not execute")

    inputs = replace(_static_inputs(), tools=(_PropertyTool(),))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_metadata_incomplete"


def test_tool_metadata_comparison_hooks_are_not_executed() -> None:
    """metadata 校验先做类型判断，不执行任意 __eq__/__hash__。"""

    class _ExplosiveValue:
        def __eq__(self, other: object) -> bool:
            raise AssertionError(other)

        def __hash__(self) -> int:
            raise AssertionError("hash")

    @dataclass(frozen=True)
    class _UnsafeMetadataTool:
        name: str = "unsafe"
        effect_kind: object = _ExplosiveValue()
        idempotency_key: str | None = None
        reconciliation: str = "none"
        can_suspend: bool = False

    inputs = replace(_static_inputs(), tools=(_UnsafeMetadataTool(),))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_effect_kind_invalid"


def test_tool_metadata_string_subclass_hooks_are_not_executed() -> None:
    """字符串子类不进入 set membership，避免执行其比较/hash hooks。"""

    class _ExplosiveStr(str):
        def __eq__(self, other: object) -> bool:
            raise AssertionError(other)

        def __hash__(self) -> int:
            raise AssertionError("hash")

    tool = replace(
        _AuditedTool(),
        effect_kind=cast("Any", _ExplosiveStr("pure")),
    )
    inputs = replace(_static_inputs(), tools=(tool,))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_effect_kind_invalid"


def test_uninitialized_tool_metadata_slot_is_stably_incomplete() -> None:
    """未初始化 slots descriptor 不泄漏 AttributeError。"""

    class _UninitializedSlotsTool:
        __slots__ = (
            "can_suspend",
            "effect_kind",
            "idempotency_key",
            "name",
            "reconciliation",
        )

        def __init__(self) -> None:
            self.name = "uninitialized_slot"
            self.idempotency_key = None
            self.reconciliation = "none"
            self.can_suspend = False

    inputs = replace(_static_inputs(), tools=(_UninitializedSlotsTool(),))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_tool_metadata_incomplete"


def test_any_loaded_orchestration_skill_is_rejected() -> None:
    """snapshot 内任一已加载 orchestration 都不能绕过可达性判断。"""
    atomic = _skill("child")
    orchestrated = _skill(
        "composite",
        skill_type="composite",
        child_skills=frozenset({"child"}),
        orchestration=OrchestrationSpec(steps=(SerialStep(("child",)),)),
    )
    inputs = replace(
        _static_inputs(),
        skill_snapshot=SkillSnapshot(
            version=2,
            skills=(atomic, orchestrated),
            reachable_graph={"composite": frozenset({"child"})},
        ),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(), static_inputs=inputs)

    assert caught.value.code == "audit_orchestration_unsupported"


def test_legacy_tool_spec_shape_is_fully_restored() -> None:
    """Task 5 前的 ToolSpec 构造字段与默认值完全保留。"""
    assert {field.name for field in fields(ToolSpec)} == {
        "name",
        "description",
        "input_schema",
        "handler",
        "parallel_safe",
        "timeout_seconds",
        "refunds_iteration",
    }
    tool = _legacy_tool()
    assert tool.parallel_safe is False
    assert tool.timeout_seconds == 60.0
    assert tool.refunds_iteration is False


def test_valid_minimal_audit_configuration_passes_and_is_frozen() -> None:
    """合法 config + resolved inputs 通过，bootstrap 配置不可重绑定。"""
    config = _config()

    validate_audit_config(config, static_inputs=_static_inputs())

    with pytest.raises(FrozenInstanceError):
        config.writer_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        ({"writer_id": ""}, "audit_writer_id_empty"),
        ({"writer_id": object()}, "audit_writer_id_empty"),
        ({"writer_id": True}, "audit_writer_id_empty"),
        ({"max_attachment_bytes": 0}, "audit_attachment_limit_invalid"),
        ({"max_attachment_bytes": object()}, "audit_attachment_limit_invalid"),
        ({"max_attachment_bytes": True}, "audit_attachment_limit_invalid"),
        ({"max_attachment_bytes": 1.5}, "audit_attachment_limit_invalid"),
        ({"max_total_attachment_bytes": 0}, "audit_total_attachment_limit_invalid"),
        (
            {"max_total_attachment_bytes": object()},
            "audit_total_attachment_limit_invalid",
        ),
        ({"max_total_attachment_bytes": True}, "audit_total_attachment_limit_invalid"),
        ({"max_total_attachment_bytes": 1.5}, "audit_total_attachment_limit_invalid"),
    ],
)
def test_audit_bootstrap_values_are_validated(
    override: dict[str, Any],
    expected_code: str,
) -> None:
    """writer identity 与附件限制在配置构造时稳定拒绝非法值。"""
    with pytest.raises(ValueError) as caught:
        replace(_config(), **override)

    assert str(caught.value) == expected_code


def test_strict_config_requires_resolved_static_inputs() -> None:
    """strict config 不能在缺失实际 Pool dependencies 时默认放行。"""
    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config())

    assert caught.value.code == "audit_static_inputs_required"


def test_legacy_none_skips_static_and_session_validation() -> None:
    """audit=None 的 legacy 路径不要求 strict resolved inputs 或新 Session。"""
    validate_audit_config(None)
    validate_audit_session_request(None, resume_thread_id="legacy-thread")
