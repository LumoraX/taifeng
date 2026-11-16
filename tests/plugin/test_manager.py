"""PluginManager 生命周期测试：依赖顺序、回滚、取消、有界关闭、事件（plugin-core 任务 2）。"""

from __future__ import annotations

import asyncio

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.plugin import (
    DuplicatePluginError,
    MissingPluginDependencyError,
    PluginCloseError,
    PluginContext,
    PluginDependencyCycleError,
    PluginLifecycleEvent,
    PluginManager,
    PluginSetupError,
    PluginSpec,
    PluginStateError,
)


class _Handle:
    """记录撤销顺序的注册 handle。"""

    def __init__(self, log: list[str], name: str, *, fail: bool = False) -> None:
        self._log = log
        self._name = name
        self._fail = fail

    async def aclose(self) -> None:
        self._log.append(f"revoke:{self._name}")
        if self._fail:
            raise RuntimeError("revoke boom")


class _Recording:
    """记录 setup / close 的插件；可配置依赖、失败、注册与卡死。"""

    def __init__(
        self,
        log: list[str],
        plugin_id: str,
        *,
        requires: tuple[str, ...] = (),
        fail_setup: bool = False,
        fail_close: bool = False,
        hang_close: bool = False,
        register: bool = False,
    ) -> None:
        self.spec = PluginSpec(id=plugin_id, version="1.0", requires=requires)
        self._log = log
        self._fail_setup = fail_setup
        self._fail_close = fail_close
        self._hang_close = hang_close
        self._register = register

    async def setup(self, ctx: PluginContext) -> None:
        self._log.append(f"setup:{self.spec.id}")
        if self._register:
            ctx._track(_Handle(self._log, self.spec.id))  # noqa: SLF001
        if self._fail_setup:
            raise RuntimeError("boom")

    async def close(self) -> None:
        self._log.append(f"close:{self.spec.id}")
        if self._hang_close:
            await asyncio.Event().wait()
        if self._fail_close:
            raise RuntimeError("close boom")


class _NoClose:
    """不实现可选 close() 的插件。"""

    spec = PluginSpec(id="noclose", version="1")

    async def setup(self, ctx: PluginContext) -> None:
        return None


def _events() -> tuple[list[PluginLifecycleEvent], PluginManager]:
    events: list[PluginLifecycleEvent] = []

    async def on_event(ev: PluginLifecycleEvent) -> None:
        events.append(ev)

    return events, PluginManager(on_event=on_event)


async def test_dependency_order_and_reverse_close() -> None:
    log: list[str] = []
    manager = PluginManager()
    await manager.load([_Recording(log, "b", requires=("a",)), _Recording(log, "a")])
    assert log == ["setup:a", "setup:b"]
    assert manager.snapshot().loaded_ids == ("a", "b")
    assert manager.snapshot().state == "loaded"
    await manager.close()
    assert log == ["setup:a", "setup:b", "close:b", "close:a"]
    assert manager.snapshot().state == "closed"
    assert manager.snapshot().loaded_ids == ()


async def test_same_layer_keeps_input_order() -> None:
    log: list[str] = []
    manager = PluginManager()
    await manager.load([_Recording(log, "c"), _Recording(log, "a"), _Recording(log, "b")])
    assert log == ["setup:c", "setup:a", "setup:b"]
    await manager.close()


async def test_plugin_close_runs_before_its_registrations_are_revoked() -> None:
    log: list[str] = []
    manager = PluginManager()
    await manager.load([_Recording(log, "a", register=True)])
    await manager.close()
    assert log == ["setup:a", "close:a", "revoke:a"]


async def test_plugin_without_close_is_supported() -> None:
    manager = PluginManager()
    await manager.load([_NoClose()])
    await manager.close()
    assert manager.snapshot().state == "closed"


async def test_setup_failure_rolls_back_prior_plugins() -> None:
    log: list[str] = []
    manager = PluginManager()
    with pytest.raises(PluginSetupError, match="bad") as caught:
        await manager.load([_Recording(log, "a"), _Recording(log, "bad", fail_setup=True)])
    assert log == ["setup:a", "setup:bad", "close:a"]
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert caught.value.plugin_id == "bad"
    assert manager.snapshot().loaded_ids == ()
    assert manager.snapshot().state == "closed"


async def test_failed_plugin_registrations_are_revoked_before_rollback() -> None:
    log: list[str] = []
    manager = PluginManager()
    with pytest.raises(PluginSetupError):
        await manager.load([
            _Recording(log, "a", register=True),
            _Recording(log, "bad", fail_setup=True, register=True),
        ])
    assert log == ["setup:a", "setup:bad", "revoke:bad", "close:a", "revoke:a"]


async def test_rollback_errors_are_counted_and_original_error_kept() -> None:
    log: list[str] = []
    events, manager = _events()
    with pytest.raises(PluginSetupError) as caught:
        await manager.load([
            _Recording(log, "a", fail_close=True),
            _Recording(log, "bad", fail_setup=True),
        ])
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "boom"
    failed = [e for e in events if e.kind == "plugin_failed"]
    assert len(failed) == 1
    assert failed[0].data["rollback_errors"] == 1
    assert failed[0].data["failure_class"] == "RuntimeError"
    assert manager.snapshot().state == "closed_with_errors"


@pytest.mark.parametrize(
    ("plugins", "error"),
    [
        (lambda log: [_Recording(log, "a"), _Recording(log, "a")], DuplicatePluginError),
        (lambda log: [_Recording(log, "a", requires=("ghost",))], MissingPluginDependencyError),
        (
            lambda log: [_Recording(log, "a", requires=("b",)), _Recording(log, "b", requires=("a",))],
            PluginDependencyCycleError,
        ),
        (lambda log: [_Recording(log, "a", requires=("a",))], PluginDependencyCycleError),
    ],
)
async def test_invalid_graph_rejected_before_any_setup(plugins, error) -> None:  # type: ignore[no-untyped-def]
    log: list[str] = []
    manager = PluginManager()
    with pytest.raises(error):
        await manager.load(plugins(log))
    assert log == []
    assert manager.snapshot().state == "new"


async def test_object_without_spec_rejected() -> None:
    with pytest.raises(TypeError):
        await PluginManager().load([object()])  # type: ignore[list-item]


async def test_load_twice_rejected() -> None:
    manager = PluginManager()
    await manager.load([])
    with pytest.raises(PluginStateError):
        await manager.load([])


async def test_cancel_during_setup_rolls_back() -> None:
    log: list[str] = []
    root = CancellationToken(name="pool")
    started = asyncio.Event()

    class Hangs:
        spec = PluginSpec(id="hangs", version="1")

        async def setup(self, ctx: PluginContext) -> None:
            assert ctx.cancel.name == "pool/plugin:hangs"
            started.set()
            await asyncio.Event().wait()

    manager = PluginManager(cancel=root)
    task = asyncio.create_task(manager.load([_Recording(log, "a"), Hangs()]))
    await started.wait()
    root.cancel()
    with pytest.raises(PluginSetupError) as caught:
        await task
    assert isinstance(caught.value.__cause__, asyncio.CancelledError)
    assert log == ["setup:a", "close:a"]


async def test_already_cancelled_token_skips_setup() -> None:
    log: list[str] = []
    root = CancellationToken(name="pool")
    root.cancel()
    with pytest.raises(PluginSetupError):
        await PluginManager(cancel=root).load([_Recording(log, "a")])
    assert log == []


async def test_external_task_cancel_propagates_after_rollback() -> None:
    log: list[str] = []
    started = asyncio.Event()

    class Hangs:
        spec = PluginSpec(id="hangs", version="1")

        async def setup(self, ctx: PluginContext) -> None:
            started.set()
            await asyncio.Event().wait()

    manager = PluginManager()
    task = asyncio.create_task(manager.load([_Recording(log, "a"), Hangs()]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == ["setup:a", "close:a"]


async def test_close_timeout_marks_errors_and_continues() -> None:
    log: list[str] = []
    events: list[PluginLifecycleEvent] = []

    async def on_event(ev: PluginLifecycleEvent) -> None:
        events.append(ev)

    manager = PluginManager(on_event=on_event, close_timeout_seconds=0.05)
    await manager.load([_Recording(log, "a"), _Recording(log, "stuck", hang_close=True)])
    with pytest.raises(PluginCloseError) as caught:
        await manager.close()
    assert caught.value.error_count == 1
    assert log[-2:] == ["close:stuck", "close:a"]
    assert manager.snapshot().state == "closed_with_errors"
    unloaded = {e.data["plugin_id"]: e.data["failure_class"] for e in events if e.kind == "plugin_unloaded"}
    assert unloaded == {"stuck": "TimeoutError", "a": None}


async def test_close_is_idempotent() -> None:
    log: list[str] = []
    manager = PluginManager()
    await manager.load([_Recording(log, "a")])
    await manager.close()
    await manager.close()
    assert log.count("close:a") == 1


async def test_close_before_load_is_noop() -> None:
    manager = PluginManager()
    await manager.close()
    assert manager.snapshot().state == "closed"


async def test_revoke_failure_counts_as_close_error() -> None:
    log: list[str] = []

    class BadRevoke:
        spec = PluginSpec(id="bad", version="1")

        async def setup(self, ctx: PluginContext) -> None:
            ctx._track(_Handle(log, "x", fail=True))  # noqa: SLF001
            ctx._track(_Handle(log, "y"))  # noqa: SLF001

    manager = PluginManager()
    await manager.load([BadRevoke()])
    with pytest.raises(PluginCloseError):
        await manager.close()
    assert log == ["revoke:y", "revoke:x"]


async def test_lifecycle_events_carry_only_metadata() -> None:
    log: list[str] = []
    events, manager = _events()
    await manager.load([_Recording(log, "a")])
    await manager.close()
    assert [e.kind for e in events] == ["plugin_loaded", "plugin_unloaded"]
    loaded = dict(events[0].data)
    assert set(loaded) == {"plugin_id", "version", "scope", "duration_ms"}
    assert loaded["plugin_id"] == "a"
    assert loaded["version"] == "1.0"
    assert loaded["scope"] == "pool"
    assert isinstance(loaded["duration_ms"], int)
    assert set(events[1].data) == {"plugin_id", "version", "scope", "duration_ms", "failure_class"}


async def test_event_callback_failure_does_not_break_lifecycle() -> None:
    async def on_event(ev: PluginLifecycleEvent) -> None:
        raise RuntimeError("observer down")

    log: list[str] = []
    manager = PluginManager(on_event=on_event)
    await manager.load([_Recording(log, "a")])
    await manager.close()
    assert log == ["setup:a", "close:a"]


def test_close_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        PluginManager(close_timeout_seconds=0)
