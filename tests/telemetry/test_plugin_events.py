"""插件生命周期事件的登记与渲染（plugin-core 任务 2.1，R3）。"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, get_args

import pytest

from taifeng.loop.event import EventMsg, MsgKind, PluginFailed, PluginLoaded, PluginUnloaded
from taifeng.telemetry.console import _KIND_TAG, _fmt_event
from taifeng.telemetry.jsonl_sink import JsonlSink

if TYPE_CHECKING:
    from pathlib import Path

_BASE = {"plugin_id": "audit", "version": "1.2", "scope": "pool", "duration_ms": 3}
_CASES = [
    PluginLoaded(data=dict(_BASE)),
    PluginFailed(data={**_BASE, "failure_class": "RuntimeError", "rollback_errors": 0}),
    PluginUnloaded(data={**_BASE, "failure_class": None}),
]


def test_kinds_registered() -> None:
    assert {"plugin_loaded", "plugin_failed", "plugin_unloaded"} <= set(get_args(MsgKind))


@pytest.mark.parametrize("msg", _CASES, ids=lambda m: m.kind)
def test_event_roundtrips_through_eventmsg(msg: PluginLoaded) -> None:
    ev = EventMsg(submission_id="*", msg=msg)
    restored = EventMsg.model_validate_json(ev.model_dump_json())
    assert restored.msg.kind == msg.kind
    assert restored.msg.data == msg.data


@pytest.mark.parametrize("msg", _CASES, ids=lambda m: m.kind)
def test_console_renders_with_dedicated_tag(msg: PluginLoaded) -> None:
    assert _KIND_TAG[msg.kind][0] == "plug"
    line = _fmt_event(EventMsg(submission_id="*", msg=msg), color=False)
    assert "plug" in line
    assert "audit" in line


async def test_jsonl_sink_writes_plugin_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    for msg in _CASES:
        await sink.handle(EventMsg(submission_id="*", msg=msg))
    kinds = [json.loads(line)["msg"]["kind"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["plugin_loaded", "plugin_failed", "plugin_unloaded"]


async def test_otel_sink_accepts_pool_level_plugin_events() -> None:
    """pool 级事件没有 turn span 可挂：OTel 走通用路径，不抛错。"""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    from taifeng.telemetry.otel_sink import OtelSinkConfig, OtelTelemetrySink

    sink = OtelTelemetrySink(
        OtelSinkConfig(service_name="taifeng-test"),
        tracer_provider=TracerProvider(),
        meter_provider=MeterProvider(),
    )
    for msg in _CASES:
        await sink.handle(EventMsg(submission_id="*", msg=msg))
    await sink.close()


def test_console_sink_writes_line() -> None:
    from taifeng.telemetry.console import ConsoleSink

    out = io.StringIO()
    sink = ConsoleSink(out=out, color=False)
    import asyncio

    asyncio.run(sink.handle(EventMsg(submission_id="*", msg=_CASES[0])))
    assert "plugin_loaded" in out.getvalue()
