"""Telemetry —— TelemetrySink 协议 + ConsoleSink / JsonlSink / OtelTelemetrySink。

通过订阅 ``AgentEngine.subscribe_all()`` 来观测引擎运行时事件，
将其格式化为：
    - Console（带颜色 + 缩进，类似 claw-code / codex）
    - JSONL（结构化日志，给运维管道）
    - OTel / OTLP（业务侧 ``[telemetry-otel]`` extra 启用；生产推荐）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.telemetry.console import ConsoleSink, attach_console_sink
from taifeng.telemetry.jsonl_sink import JsonlSink, attach_jsonl_sink
from taifeng.telemetry.sink import TelemetrySink

if TYPE_CHECKING:
    from taifeng.telemetry.otel_sink import OtelSinkConfig, OtelTelemetrySink

__all__ = [
    "ConsoleSink",
    "JsonlSink",
    "OtelSinkConfig",
    "OtelTelemetrySink",
    "TelemetrySink",
    "attach_console_sink",
    "attach_jsonl_sink",
]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute：未装 ``telemetry-otel`` extra 时不在 import 阶段报错。"""
    if name in ("OtelSinkConfig", "OtelTelemetrySink"):
        from taifeng.telemetry import otel_sink

        return getattr(otel_sink, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
