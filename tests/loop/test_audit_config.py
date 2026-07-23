"""strict SessionJournal audit 静态 capability gate 测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest

from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_config import (
    AuditCapabilities,
    AuditCapabilityError,
    AuditConfig,
    ModelAttemptCapability,
    validate_audit_config,
)
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.orchestration import OrchestrationSpec, SerialStep
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.loop.audit_config import AuditJournalCore


async def _tool_handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """返回不产生 effect 的固定 ToolResult。"""
    del args, ctx
    return ToolResult.ok("ok")


def _tool(
    name: str = "safe_tool",
    *,
    effect_kind: Literal["read", "write", "external"] | None = "read",
    reconciliation: Literal["none", "idempotency_key", "manual"] | None = "none",
    can_suspend: bool | None = False,
) -> ToolSpec:
    """构造 metadata 可控的 ToolSpec。"""
    return ToolSpec(
        name=name,
        description="safe",
        input_schema={"type": "object"},
        handler=_tool_handler,
        effect_kind=effect_kind,
        reconciliation=reconciliation,
        can_suspend=can_suspend,
    )


def _skill(
    skill_id: str,
    *,
    skill_type: str = "atomic",
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
        type=skill_type,  # type: ignore[arg-type]
        child_skills=child_skills,
        orchestration=orchestration,
    )


def _capabilities() -> AuditCapabilities:
    """构造 strict audit 可接受的最小静态能力快照。"""
    client = SimClient(turns=[SimTurn(text="ok")])
    return AuditCapabilities(
        model_client=client,
        model_attempt=ModelAttemptCapability.declare_single_attempt(client),
        skill_snapshot=SkillSnapshot(
            version=1,
            skills=(_skill("atomic"),),
        ),
        tools=(_tool(),),
    )


def _config(capabilities: AuditCapabilities | None = None) -> AuditConfig:
    """注入不会在静态验证中触发 IO 的 Journal core 占位。"""
    return AuditConfig(
        journal_core=cast("AuditJournalCore", object()),
        writer_id="writer-1",
        max_attachment_bytes=1024,
        max_total_attachment_bytes=4096,
        capabilities=capabilities or _capabilities(),
    )


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        ({"resume_thread_id": "old-thread"}, "audit_resume_unsupported"),
        ({"custom_store": object()}, "audit_custom_store_unsupported"),
        ({"custom_directory": object()}, "audit_custom_directory_unsupported"),
        ({"index_hook": object()}, "audit_index_hook_unsupported"),
        ({"hooks": object()}, "audit_hooks_unsupported"),
        ({"permission_policy": object()}, "audit_permission_unsupported"),
        ({"hitl_prompter": object()}, "audit_hitl_unsupported"),
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
    ],
)
def test_unsupported_static_capability_has_stable_error_code(
    override: dict[str, Any],
    expected_code: str,
) -> None:
    """每项 unsupported 静态配置都在 effect 前以稳定 code 拒绝。"""
    capabilities = replace(_capabilities(), **override)

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

    assert caught.value.code == expected_code
    assert str(caught.value) == expected_code


def test_unobserved_model_client_is_rejected() -> None:
    """未显式声明 attempt 边界的 ModelClient 不得依赖 duck attribute 放行。"""
    capabilities = replace(_capabilities(), model_attempt=None)

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

    assert caught.value.code == "audit_model_attempt_unobservable"


def test_model_attempt_declaration_must_bind_the_configured_client() -> None:
    """显式 capability marker 不能替另一个 client 作能力背书。"""
    other = SimClient(turns=[SimTurn(text="other")])
    capabilities = replace(
        _capabilities(),
        model_attempt=ModelAttemptCapability.declare_single_attempt(other),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

    assert caught.value.code == "audit_model_attempt_client_mismatch"


def test_model_attempt_duck_attribute_cannot_bypass_explicit_marker() -> None:
    """带同名属性的任意对象不能伪装成结构化 capability marker。"""
    capabilities = _capabilities()

    class _DuckMarker:
        client = capabilities.model_client

    capabilities = replace(
        capabilities,
        model_attempt=_DuckMarker(),  # type: ignore[arg-type]
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

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
    ],
)
def test_registered_spawn_and_barrier_tools_are_rejected(
    tool_name: str,
    expected_code: str,
) -> None:
    """仅注册 detached spawn/barrier 工具也必须触发静态拒绝。"""
    capabilities = replace(_capabilities(), tools=(_tool(tool_name),))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

    assert caught.value.code == expected_code


@pytest.mark.parametrize("tool_name", ["send_message", "wait_peer"])
def test_registered_peer_tool_is_rejected(tool_name: str) -> None:
    """注册 send_message 即声明 peer capability，不能等到 runtime 才发现。"""
    capabilities = replace(_capabilities(), tools=(_tool(tool_name),))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

    assert caught.value.code == "audit_peer_unsupported"


@pytest.mark.parametrize(
    ("tool", "expected_code"),
    [
        (
            _tool(effect_kind=None),
            "audit_tool_effect_kind_missing",
        ),
        (
            _tool(reconciliation=None),
            "audit_tool_reconciliation_missing",
        ),
        (
            _tool(can_suspend=None),
            "audit_tool_suspension_metadata_missing",
        ),
        (
            _tool(can_suspend=True),
            "audit_tool_suspension_unsupported",
        ),
    ],
)
def test_tool_audit_metadata_is_complete_and_non_suspending(
    tool: ToolSpec,
    expected_code: str,
) -> None:
    """strict audit 拒绝 metadata 缺失或声明可 suspend 的 Tool。"""
    capabilities = replace(_capabilities(), tools=(tool,))

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

    assert caught.value.code == expected_code


def test_any_loaded_orchestration_skill_is_rejected() -> None:
    """snapshot 内任一已加载 orchestration 都不能绕过 entry 可达性判断。"""
    atomic = _skill("child")
    orchestrated = _skill(
        "composite",
        skill_type="composite",
        child_skills=frozenset({"child"}),
        orchestration=OrchestrationSpec(steps=(SerialStep(("child",)),)),
    )
    capabilities = replace(
        _capabilities(),
        skill_snapshot=SkillSnapshot(
            version=2,
            skills=(atomic, orchestrated),
            reachable_graph={"composite": frozenset({"child"})},
        ),
    )

    with pytest.raises(AuditCapabilityError) as caught:
        validate_audit_config(_config(capabilities))

    assert caught.value.code == "audit_orchestration_unsupported"


def test_valid_minimal_audit_configuration_passes_and_is_frozen() -> None:
    """合法最小配置通过；bootstrap 配置本身不可重绑定。"""
    config = _config()

    validate_audit_config(config)

    with pytest.raises(FrozenInstanceError):
        config.writer_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        ({"writer_id": ""}, "audit_writer_id_empty"),
        ({"max_attachment_bytes": 0}, "audit_attachment_limit_invalid"),
        ({"max_total_attachment_bytes": 0}, "audit_total_attachment_limit_invalid"),
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


def test_legacy_none_skips_strict_validation() -> None:
    """audit=None 的 legacy 路径不要求任何 strict capability。"""
    validate_audit_config(None)
