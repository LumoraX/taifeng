"""OtelTelemetrySink —— 把 ``EventMsg`` 桥接到 OpenTelemetry / OTLP collector。

R1 业务零侵入：本模块**禁止**出现任何业务字段名。
R2 Cache 友好：sink 是只读消费者，不动 context、不动 prompt。
R3 可观测：这正是 R3 的落地实现。
R4 可取消：所有 export 走 OTel SDK 的 BatchSpanProcessor（异步队列），不阻塞主 actor。

业务侧按需 ``uv pip install -e ".[telemetry-otel]"``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import taifeng as _taifeng_pkg

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, MeterProvider
    from opentelemetry.trace import Span, TracerProvider

# OTel 包按 optional extra 提供；未装时构造时报错，而非 import 时报错
try:
    from opentelemetry import metrics as _otel_metrics  # noqa: F401
    from opentelemetry import trace as _otel_trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter as _OTLPMetricExporterGrpc,
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as _OTLPSpanExporterGrpc,
    )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter as _OTLPMetricExporterHttp,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as _OTLPSpanExporterHttp,
    )
    from opentelemetry.sdk.metrics import MeterProvider as _SdkMeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider as _SdkTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
    _OTEL_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - 仅在未装 extra 时触发
    _OTEL_AVAILABLE = False
    _OTEL_IMPORT_ERROR = exc

from taifeng.loop.event import EventMsg

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OtelSinkConfig:
    """OtelTelemetrySink 配置对象。

    业务侧构造时注入 ``service_name`` / ``service_version`` / ``otlp_endpoint``；
    本类**禁止**出现任何业务字段（R1）。
    """

    service_name: str
    service_version: str = field(default_factory=lambda: _taifeng_pkg.__version__)
    otlp_endpoint: str | None = None
    protocol: Literal["grpc", "http/protobuf"] = "grpc"
    resource_attributes: dict[str, str] = field(default_factory=dict)
    sampler: str | None = None

    @classmethod
    def from_env(cls) -> OtelSinkConfig:
        """从标准 ``OTEL_*`` 环境变量构造实例。

        ``OTEL_SERVICE_NAME`` 未设置时抛 ``ValueError``。该类方法是 sink 自身的
        引导例外；业务代码请走构造函数显式注入。
        """
        import os

        svc = os.environ.get("OTEL_SERVICE_NAME")
        if not svc:
            raise ValueError(
                "OtelSinkConfig.from_env 需要设置 OTEL_SERVICE_NAME 环境变量；"
                "或直接 OtelSinkConfig(service_name=...) 显式构造。"
            )
        ver = os.environ.get("OTEL_SERVICE_VERSION", _taifeng_pkg.__version__)
        ep = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        proto_env = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        if proto_env not in ("grpc", "http/protobuf"):
            _log.warning(
                "OTEL_EXPORTER_OTLP_PROTOCOL=%r 非合法值，回落到 grpc。", proto_env
            )
            proto_env = "grpc"
        return cls(
            service_name=svc,
            service_version=ver,
            otlp_endpoint=ep,
            protocol=proto_env,  # type: ignore[arg-type]
        )


# ============ EventMsg → OTel 映射常量 ============

# 黑名单字段：一律不进 OTel attribute（避免 PII / 超长）
_PAYLOAD_BLACKLIST: frozenset[str] = frozenset(
    {"arguments", "output", "delta", "summary", "user_text_preview"}
)

# 只统计 bytes 不带正文的事件类型
_TEXT_ONLY_KINDS: frozenset[str] = frozenset({"assistant_text", "assistant_reasoning"})


def _safe_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """从 ``event.msg.data`` 过滤掉正文字段，并把复杂类型转字符串。

    OTel attribute 仅允许 ``str``/``bool``/``int``/``float``/序列；其余 stringify。
    """
    result: dict[str, Any] = {}
    for k, v in data.items():
        if k in _PAYLOAD_BLACKLIST:
            continue
        if isinstance(v, str | bool | int | float):
            result[k] = v
        elif isinstance(v, list | tuple) and all(
            isinstance(x, str | bool | int | float) for x in v
        ):
            result[k] = list(v)
        elif v is None:
            continue
        else:
            result[k] = str(v)
    return result


class OtelTelemetrySink:
    """实现 ``TelemetrySink`` 协议，把 EventMsg 桥接到 OTel SDK。

    构造时若 OTel extra 未装，立即抛 ``RuntimeError`` 指引安装。
    """

    def __init__(
        self,
        config: OtelSinkConfig,
        *,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        """构造一个 OTel sink。

        :param config: 必填配置对象。
        :param tracer_provider: 测试可注入预绑定 InMemorySpanExporter 的 provider；
            未注入时按 config 自动构造默认 OTLP provider。
        :param meter_provider: 同上，metric 通道。
        """
        if not _OTEL_AVAILABLE:
            raise RuntimeError(
                "OTel SDK 未安装；请运行 `install taifeng[telemetry-otel]` "
                "（或 `pip install opentelemetry-api opentelemetry-sdk "
                "opentelemetry-exporter-otlp`）。"
                f"原始 import 错误：{_OTEL_IMPORT_ERROR!r}"
            ) from _OTEL_IMPORT_ERROR

        self._config = config

        # ---- TracerProvider ----
        if tracer_provider is None:
            resource = Resource.create(
                {
                    "service.name": config.service_name,
                    "service.version": config.service_version,
                    **config.resource_attributes,
                }
            )
            tp = _SdkTracerProvider(resource=resource)
            if config.protocol == "grpc":
                span_exporter = _OTLPSpanExporterGrpc(endpoint=config.otlp_endpoint)
            else:
                span_exporter = _OTLPSpanExporterHttp(endpoint=config.otlp_endpoint)
            tp.add_span_processor(BatchSpanProcessor(span_exporter))
            self._tracer_provider = tp
        else:
            self._tracer_provider = tracer_provider

        # ---- MeterProvider ----
        if meter_provider is None:
            resource = Resource.create(
                {
                    "service.name": config.service_name,
                    "service.version": config.service_version,
                    **config.resource_attributes,
                }
            )
            if config.protocol == "grpc":
                metric_exporter = _OTLPMetricExporterGrpc(endpoint=config.otlp_endpoint)
            else:
                metric_exporter = _OTLPMetricExporterHttp(endpoint=config.otlp_endpoint)
            mp = _SdkMeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
            )
            self._meter_provider = mp
        else:
            self._meter_provider = meter_provider

        self._tracer = self._tracer_provider.get_tracer(
            "taifeng.telemetry.otel", _taifeng_pkg.__version__
        )
        meter = self._meter_provider.get_meter(
            "taifeng.telemetry.otel", _taifeng_pkg.__version__
        )

        # 预建 2 个 counter；taifeng.provider.retries 暂搁待 ProviderRetry 事件落地
        self._counter_compaction: Counter = meter.create_counter(
            "taifeng.compaction.attempts",
            description="次数：CompactionStarted 派发计数",
        )
        self._counter_cache_breaks: Counter = meter.create_counter(
            "taifeng.cache.breaks",
            description="次数：CacheBreakDetected / CompactionCompleted(cache_invalidated=True)",
        )
        # G3：turn 失败按稳定 failure_class 维度计数（telemetry 聚合）
        self._counter_failures: Counter = meter.create_counter(
            "taifeng.turn.failures",
            description="次数：TurnFailed，按 failure_class 维度",
        )

        # 跨方法维护 span 嵌套；**禁止**模块级全局
        self._turn_spans: dict[str, Span] = {}
        self._tool_spans: dict[str, Span] = {}
        self._skill_spans: dict[str, Span] = {}

    async def handle(self, ev: EventMsg) -> None:
        """实现 ``TelemetrySink.handle``。fire-and-forget。"""
        try:
            self._dispatch(ev)
        except Exception as exc:  # noqa: BLE001 - sink 必须 fire-and-forget
            _log.warning(
                "OtelTelemetrySink.handle 处理 event %s 失败：%r",
                getattr(ev.msg, "kind", "<unknown>"),
                exc,
            )

    def _dispatch(self, ev: EventMsg) -> None:
        """同步分派 —— 按 kind 路由到具体 handler。"""
        kind = ev.msg.kind
        data = ev.msg.data
        submission_id = ev.submission_id

        if kind == "turn_started":
            self._on_turn_started(submission_id, data)
        elif kind == "turn_completed":
            self._on_turn_ended(submission_id, data, failed=False)
        elif kind == "turn_failed":
            self._on_turn_ended(submission_id, data, failed=True)
        elif kind == "tool_call_started":
            self._on_tool_started(submission_id, data)
        elif kind == "tool_call_completed":
            self._on_tool_completed(submission_id, data)
        elif kind == "skill_dispatched":
            self._on_skill_dispatched(submission_id, data)
        elif kind == "skill_returned":
            self._on_skill_returned(submission_id, data)
        elif kind in _TEXT_ONLY_KINDS:
            self._on_text(submission_id, kind, data)
        elif kind == "compaction_started":
            self._on_compaction_started(submission_id, data)
        elif kind == "compaction_completed":
            self._on_compaction_completed(submission_id, data)
        elif kind == "cache_break_detected":
            self._on_cache_break(submission_id, data)
        else:
            self._on_generic_event(submission_id, kind, data)

    # ---------- per-kind handlers ----------

    def _on_turn_started(self, submission_id: str, data: dict[str, Any]) -> None:
        attrs = _safe_attrs(data)
        attrs.setdefault("taifeng.submission_id", submission_id)
        span = self._tracer.start_span(name="taifeng.turn", attributes=attrs)
        self._turn_spans[submission_id] = span

    def _on_turn_ended(
        self, submission_id: str, data: dict[str, Any], *, failed: bool
    ) -> None:
        # G3：失败按 failure_class 计数（在 span 早退之前，确保始终计数）
        if failed:
            self._counter_failures.add(
                1,
                attributes={
                    "failure_class": str(data.get("failure_class", "unknown"))
                },
            )
        span = self._turn_spans.pop(submission_id, None)
        if span is None:
            self._on_generic_event(
                submission_id, "turn_failed" if failed else "turn_completed", data
            )
            return
        for k, v in _safe_attrs(data).items():
            span.set_attribute(k, v)
        if failed:
            span.set_status(Status(StatusCode.ERROR, description=str(data.get("error", ""))))
        span.end()

    def _on_tool_started(self, submission_id: str, data: dict[str, Any]) -> None:
        name = str(data.get("name", "unknown"))
        call_id = str(data.get("call_id", ""))
        attrs = _safe_attrs(data)
        attrs["tool.name"] = name
        attrs["tool.call_id"] = call_id
        attrs["taifeng.submission_id"] = submission_id
        parent = self._turn_spans.get(submission_id)
        ctx = _otel_trace.set_span_in_context(parent) if parent is not None else None
        span = self._tracer.start_span(
            name=f"taifeng.tool.{name}", context=ctx, attributes=attrs
        )
        self._tool_spans[call_id] = span

    def _on_tool_completed(self, submission_id: str, data: dict[str, Any]) -> None:
        call_id = str(data.get("call_id", ""))
        span = self._tool_spans.pop(call_id, None)
        if span is None:
            self._on_generic_event(submission_id, "tool_call_completed", data)
            return
        is_error = bool(data.get("is_error", False))
        for k, v in _safe_attrs(data).items():
            if k == "is_error":
                span.set_attribute("tool.is_error", v)
            elif k == "duration_ms":
                span.set_attribute("tool.duration_ms", v)
            elif k == "name":
                span.set_attribute("tool.name", v)
            else:
                span.set_attribute(k, v)
        if is_error:
            span.set_status(Status(StatusCode.ERROR))
        span.end()

    def _on_skill_dispatched(self, submission_id: str, data: dict[str, Any]) -> None:
        skill_id = str(data.get("skill_id", "unknown"))
        call_id = str(data.get("call_id", ""))
        attrs = _safe_attrs(data)
        attrs["skill.id"] = skill_id
        attrs["skill.call_id"] = call_id
        attrs["taifeng.submission_id"] = submission_id
        parent = self._turn_spans.get(submission_id)
        ctx = _otel_trace.set_span_in_context(parent) if parent is not None else None
        span = self._tracer.start_span(
            name=f"taifeng.skill.{skill_id}", context=ctx, attributes=attrs
        )
        self._skill_spans[call_id] = span

    def _on_skill_returned(self, submission_id: str, data: dict[str, Any]) -> None:
        call_id = str(data.get("call_id", ""))
        span = self._skill_spans.pop(call_id, None)
        if span is None:
            self._on_generic_event(submission_id, "skill_returned", data)
            return
        success = bool(data.get("success", True))
        for k, v in _safe_attrs(data).items():
            span.set_attribute(k, v)
        if not success:
            span.set_status(Status(StatusCode.ERROR))
        span.end()

    def _on_text(self, submission_id: str, kind: str, data: dict[str, Any]) -> None:
        parent = self._turn_spans.get(submission_id)
        if parent is None:
            return
        delta = data.get("delta", "")
        try:
            n_bytes = len(delta) if isinstance(delta, str) else 0
        except TypeError:
            n_bytes = 0
        parent.add_event(
            name=kind,
            attributes={"bytes": n_bytes, "taifeng.submission_id": submission_id},
        )

    def _on_compaction_started(self, submission_id: str, data: dict[str, Any]) -> None:
        strategy = str(data.get("strategy", "unknown"))
        self._counter_compaction.add(1, attributes={"strategy": strategy})
        parent = self._turn_spans.get(submission_id)
        if parent is not None:
            parent.add_event(
                name="compaction_started",
                attributes=_safe_attrs(data) | {"taifeng.submission_id": submission_id},
            )

    def _on_compaction_completed(self, submission_id: str, data: dict[str, Any]) -> None:
        if bool(data.get("cache_invalidated", False)):
            reason = str(data.get("reason", "compaction_completed"))
            self._counter_cache_breaks.add(1, attributes={"reason": reason})
        parent = self._turn_spans.get(submission_id)
        if parent is not None:
            parent.add_event(
                name="compaction_completed",
                attributes=_safe_attrs(data) | {"taifeng.submission_id": submission_id},
            )

    def _on_cache_break(self, submission_id: str, data: dict[str, Any]) -> None:
        reason = str(data.get("reason", "unknown"))
        self._counter_cache_breaks.add(1, attributes={"reason": reason})
        parent = self._turn_spans.get(submission_id)
        if parent is not None:
            parent.add_event(
                name="cache_break_detected",
                attributes=_safe_attrs(data) | {"taifeng.submission_id": submission_id},
            )

    def _on_generic_event(
        self, submission_id: str, kind: str, data: dict[str, Any]
    ) -> None:
        parent = self._turn_spans.get(submission_id)
        attrs = _safe_attrs(data) | {"taifeng.submission_id": submission_id}
        if parent is not None:
            parent.add_event(name=f"taifeng.event.{kind}", attributes=attrs)

    async def close(self, *, timeout_millis: int = 5000) -> None:
        """显式 flush + shutdown exporter。SHALL NOT 无限阻塞（R4）。"""
        try:
            self._tracer_provider.force_flush(timeout_millis=timeout_millis)
        except Exception as exc:  # noqa: BLE001
            _log.warning("OtelTelemetrySink.close tracer force_flush 失败：%r", exc)
        try:
            self._tracer_provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            _log.warning("OtelTelemetrySink.close tracer shutdown 失败：%r", exc)
        try:
            self._meter_provider.force_flush(timeout_millis=timeout_millis)
        except Exception as exc:  # noqa: BLE001
            _log.warning("OtelTelemetrySink.close meter force_flush 失败：%r", exc)
        try:
            self._meter_provider.shutdown(timeout_millis=timeout_millis)
        except Exception as exc:  # noqa: BLE001
            _log.warning("OtelTelemetrySink.close meter shutdown 失败：%r", exc)


__all__ = ["OtelSinkConfig", "OtelTelemetrySink"]
