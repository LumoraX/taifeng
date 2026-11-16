"""PluginSpec / PluginContext / 错误集合的契约测试（plugin-core 任务 1）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.plugin import (
    DuplicatePluginError,
    MissingPluginDependencyError,
    PluginCapabilityError,
    PluginCloseError,
    PluginContext,
    PluginDependencyCycleError,
    PluginManager,
    PluginSetupError,
    PluginSpec,
    PluginStateError,
)


class _Handle:
    """测试用注册 handle：记录撤销。"""

    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    async def aclose(self) -> None:
        self._log.append(f"revoke:{self._name}")


def _ctx() -> PluginContext:
    return PluginContext(plugin_id="demo", cancel=CancellationToken(), track=lambda h: None)


def test_plugin_spec_is_frozen() -> None:
    spec = PluginSpec(id="demo", version="1")
    with pytest.raises(FrozenInstanceError):
        spec.id = "other"  # type: ignore[misc]


def test_plugin_spec_defaults() -> None:
    spec = PluginSpec(id="demo", version="1")
    assert spec.requires == ()
    assert spec.scope == "pool"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"id": "", "version": "1"}, ValueError),
        ({"id": "demo", "version": ""}, ValueError),
        ({"id": "demo", "version": "1", "scope": "engine"}, ValueError),
        ({"id": "demo", "version": "1", "requires": ["a"]}, TypeError),
    ],
)
def test_plugin_spec_rejects_invalid_fields(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        PluginSpec(**kwargs)  # type: ignore[arg-type]


def test_context_exposes_only_allowlisted_members() -> None:
    ctx = _ctx()
    assert {name for name in dir(ctx) if not name.startswith("_")} == {"plugin_id", "cancel"}
    assert not hasattr(ctx, "__dict__")  # __slots__：插件不能往 context 上挂东西


def test_context_reports_identity_and_token() -> None:
    token = CancellationToken(name="pool")
    ctx = PluginContext(plugin_id="demo", cancel=token, track=lambda h: None)
    assert ctx.plugin_id == "demo"
    assert ctx.cancel is token


def test_sealed_context_rejects_registration() -> None:
    ctx = _ctx()
    ctx._ensure_open("tools")  # noqa: SLF001 —— 封口前允许
    ctx._seal()  # noqa: SLF001
    with pytest.raises(PluginCapabilityError) as caught:
        ctx._ensure_open("tools")  # noqa: SLF001
    assert caught.value.capability == "tools"
    assert caught.value.plugin_id == "demo"


async def test_context_sealed_once_setup_returns() -> None:
    kept: list[PluginContext] = []

    class Keeper:
        spec = PluginSpec(id="keeper", version="1")

        async def setup(self, ctx: PluginContext) -> None:
            kept.append(ctx)

    manager = PluginManager()
    await manager.load([Keeper()])
    with pytest.raises(PluginCapabilityError):
        kept[0]._ensure_open("tools")  # noqa: SLF001
    await manager.close()


async def test_tracked_registrations_are_revoked_by_manager() -> None:
    log: list[str] = []

    class Registers:
        spec = PluginSpec(id="reg", version="1")

        async def setup(self, ctx: PluginContext) -> None:
            ctx._track(_Handle(log, "a"))  # noqa: SLF001 —— 模拟一个 capability
            ctx._track(_Handle(log, "b"))  # noqa: SLF001

    manager = PluginManager()
    await manager.load([Registers()])
    assert log == []
    await manager.close()
    assert log == ["revoke:b", "revoke:a"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (DuplicatePluginError("x"), "duplicate_plugin"),
        (MissingPluginDependencyError("x"), "missing_dependency"),
        (PluginDependencyCycleError("x"), "dependency_cycle"),
        (PluginSetupError("x"), "setup_failed"),
        (PluginCloseError("x", error_count=1), "close_failed"),
        (PluginCapabilityError("x", plugin_id=None, capability="tools"), "capability_denied"),
        (PluginStateError("x"), "invalid_state"),
    ],
)
def test_errors_carry_stable_codes(error: Exception, code: str) -> None:
    assert error.code == code  # type: ignore[attr-defined]
    assert str(error).startswith(f"[{code}]")
