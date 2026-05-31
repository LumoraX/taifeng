"""OtelTelemetrySink 测试 —— 全部用 InMemorySpanExporter / InMemoryMetricReader。

任何测试都**不发**真实网络请求。覆盖 6 个 Requirement 的 Scenario：
1. OtelSinkConfig 默认 / from_env / 冻结
2. OtelTelemetrySink 构造（带注入 / 缺 OTel extra）
3. EventMsg → OTel 映射（turn → tool span 嵌套 / counter 累计 / 异常吞掉 / 无正文）
4. close 超时
5. R1 业务零侵入静态扫描
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import taifeng
from taifeng.loop.event import EventMsg
from taifeng.telemetry.otel_sink import (
    OtelSinkConfig,
    OtelTelemetrySink,
    _safe_attrs,
)

# ============ fixtures ============


@pytest.fixture
def memory_tracer_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """构造一个仅落 InMemory 的 TracerProvider，避免任何网络。"""
    exporter = InMemorySpanExporter()
    tp = TracerProvider(resource=Resource.create({"service.name": "test"}))
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp, exporter


@pytest.fixture
def memory_meter_provider() -> tuple[MeterProvider, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    mp = MeterProvider(
        resource=Resource.create({"service.name": "test"}),
        metric_readers=[reader],
    )
    return mp, reader


@pytest.fixture
def sink(
    memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
    memory_meter_provider: tuple[MeterProvider, InMemoryMetricReader],
) -> Iterator[OtelTelemetrySink]:
    tp, _ = memory_tracer_provider
    mp, _ = memory_meter_provider
    cfg = OtelSinkConfig(service_name="taifeng-test")
    yield OtelTelemetrySink(cfg, tracer_provider=tp, meter_provider=mp)


@pytest.fixture
def clean_otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OTEL_SERVICE_NAME",
        "OTEL_SERVICE_VERSION",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
    ):
        monkeypatch.delenv(var, raising=False)


def _event(submission_id: str, kind: str, data: dict[str, object] | None = None) -> EventMsg:
    return EventMsg.model_validate(
        {"submission_id": submission_id, "msg": {"kind": kind, "data": data or {}}}
    )


# ============ Requirement 1: OtelSinkConfig ============


class TestOtelSinkConfig:
    def test_default_constructor(self) -> None:
        cfg = OtelSinkConfig(service_name="my-svc")
        assert cfg.service_version == taifeng.__version__
        assert cfg.protocol == "grpc"
        assert cfg.resource_attributes == {}
        assert cfg.otlp_endpoint is None
        assert cfg.sampler is None

    def test_from_env_missing_service_name_raises(self, clean_otel_env: None) -> None:
        with pytest.raises(ValueError, match="OTEL_SERVICE_NAME"):
            OtelSinkConfig.from_env()

    def test_from_env_with_minimal(
        self, clean_otel_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_SERVICE_NAME", "my-agent")
        cfg = OtelSinkConfig.from_env()
        assert cfg.service_name == "my-agent"

    def test_from_env_invalid_protocol_falls_back_to_grpc(
        self, clean_otel_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_SERVICE_NAME", "x")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "weird")
        cfg = OtelSinkConfig.from_env()
        assert cfg.protocol == "grpc"

    def test_frozen_dataclass_rejects_mutation(self) -> None:
        cfg = OtelSinkConfig(service_name="x")
        with pytest.raises(Exception):  # noqa: B017,PT011 - FrozenInstanceError
            cfg.service_name = "y"  # type: ignore[misc]


# ============ Requirement 2: OtelTelemetrySink 构造 ============


class TestSinkConstruction:
    def test_constructs_with_injected_providers(
        self,
        memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
        memory_meter_provider: tuple[MeterProvider, InMemoryMetricReader],
    ) -> None:
        tp, _ = memory_tracer_provider
        mp, _ = memory_meter_provider
        cfg = OtelSinkConfig(service_name="x")
        sink = OtelTelemetrySink(cfg, tracer_provider=tp, meter_provider=mp)
        assert sink is not None


# ============ Requirement 4: EventMsg → OTel 映射 ============


class TestEventMapping:
    async def test_turn_started_creates_span_with_submission_id(
        self,
        sink: OtelTelemetrySink,
        memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
    ) -> None:
        _, exporter = memory_tracer_provider
        await sink.handle(_event("sub-1", "turn_started"))
        await sink.handle(_event("sub-1", "turn_completed", {"iterations": 3}))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        turn_span = spans[0]
        assert turn_span.name == "taifeng.turn"
        assert turn_span.attributes is not None
        assert turn_span.attributes.get("taifeng.submission_id") == "sub-1"
        assert turn_span.attributes.get("iterations") == 3

    async def test_skill_span_nested_in_turn(
        self,
        sink: OtelTelemetrySink,
        memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
    ) -> None:
        _, exporter = memory_tracer_provider
        await sink.handle(_event("sub-2", "turn_started"))
        await sink.handle(
            _event(
                "sub-2",
                "skill_dispatched",
                {"skill_id": "expert", "call_id": "s1", "depth": 1},
            )
        )
        await sink.handle(
            _event(
                "sub-2",
                "skill_returned",
                {
                    "skill_id": "expert",
                    "call_id": "s1",
                    "success": True,
                    "summary": "should not appear in attrs",
                },
            )
        )
        await sink.handle(_event("sub-2", "turn_completed"))

        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        skill_span = next(s for s in spans if s.name == "taifeng.skill.expert")
        turn_span = next(s for s in spans if s.name == "taifeng.turn")
        assert skill_span.parent is not None
        assert skill_span.parent.span_id == turn_span.context.span_id
        assert skill_span.attributes is not None
        assert "summary" not in skill_span.attributes

    async def test_tool_span_nested_in_turn_no_payload(
        self,
        sink: OtelTelemetrySink,
        memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
    ) -> None:
        _, exporter = memory_tracer_provider
        await sink.handle(_event("sub-3", "turn_started"))
        await sink.handle(
            _event(
                "sub-3",
                "tool_call_started",
                {
                    "call_id": "c1",
                    "name": "file_read",
                    "arguments": "should-not-appear",
                },
            )
        )
        await sink.handle(
            _event(
                "sub-3",
                "tool_call_completed",
                {
                    "call_id": "c1",
                    "name": "file_read",
                    "is_error": False,
                    "duration_ms": 12,
                    "output": "should-not-appear",
                },
            )
        )
        await sink.handle(_event("sub-3", "turn_completed"))

        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        tool_span = next(s for s in spans if s.name == "taifeng.tool.file_read")
        turn_span = next(s for s in spans if s.name == "taifeng.turn")
        assert tool_span.parent is not None
        assert tool_span.parent.span_id == turn_span.context.span_id
        assert tool_span.attributes is not None
        assert tool_span.attributes.get("tool.name") == "file_read"
        assert tool_span.attributes.get("tool.call_id") == "c1"
        assert tool_span.attributes.get("tool.is_error") is False
        assert tool_span.attributes.get("tool.duration_ms") == 12
        assert "arguments" not in tool_span.attributes
        assert "output" not in tool_span.attributes

    async def test_cache_breaks_counter_accumulates(
        self,
        sink: OtelTelemetrySink,
        memory_meter_provider: tuple[MeterProvider, InMemoryMetricReader],
    ) -> None:
        _, reader = memory_meter_provider
        for _ in range(3):
            await sink.handle(
                _event(
                    "sub-4",
                    "cache_break_detected",
                    {"unexpected": True, "reason": "head_modified", "token_drop": 120},
                )
            )

        data = reader.get_metrics_data()
        total = _sum_counter(data, "taifeng.cache.breaks", reason="head_modified")
        assert total == 3

    async def test_compaction_completed_with_cache_invalidated_also_increments_cache_breaks(
        self,
        sink: OtelTelemetrySink,
        memory_meter_provider: tuple[MeterProvider, InMemoryMetricReader],
    ) -> None:
        _, reader = memory_meter_provider
        await sink.handle(
            _event(
                "sub-5",
                "compaction_completed",
                {
                    "success": True,
                    "cache_invalidated": True,
                    "removed_count": 2,
                    "reason": "head_swap",
                },
            )
        )
        data = reader.get_metrics_data()
        assert _sum_counter(data, "taifeng.cache.breaks", reason="head_swap") == 1

    async def test_compaction_started_increments_compaction_attempts(
        self,
        sink: OtelTelemetrySink,
        memory_meter_provider: tuple[MeterProvider, InMemoryMetricReader],
    ) -> None:
        _, reader = memory_meter_provider
        await sink.handle(
            _event(
                "sub-6",
                "compaction_started",
                {"strategy": "sliding", "phase": "mid_turn", "token_estimate": 200},
            )
        )
        data = reader.get_metrics_data()
        assert _sum_counter(data, "taifeng.compaction.attempts", strategy="sliding") == 1

    async def test_assistant_text_only_records_bytes_no_body(
        self,
        sink: OtelTelemetrySink,
        memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
    ) -> None:
        _, exporter = memory_tracer_provider
        await sink.handle(_event("sub-7", "turn_started"))
        await sink.handle(_event("sub-7", "assistant_text", {"delta": "Hello world!"}))
        await sink.handle(_event("sub-7", "turn_completed"))
        spans = exporter.get_finished_spans()
        turn_span = next(s for s in spans if s.name == "taifeng.turn")
        evs = [e for e in turn_span.events if e.name == "assistant_text"]
        assert len(evs) == 1
        assert evs[0].attributes is not None
        assert evs[0].attributes.get("bytes") == len("Hello world!")
        assert "delta" not in evs[0].attributes

    async def test_turn_failed_sets_status_error(
        self,
        sink: OtelTelemetrySink,
        memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
    ) -> None:
        _, exporter = memory_tracer_provider
        await sink.handle(_event("sub-8", "turn_started"))
        await sink.handle(
            _event(
                "sub-8",
                "turn_failed",
                {"error": "context_overflow", "kind": "LLMError", "iterations": 2},
            )
        )
        spans = exporter.get_finished_spans()
        turn_span = next(s for s in spans if s.name == "taifeng.turn")
        from opentelemetry.trace.status import StatusCode

        assert turn_span.status.status_code == StatusCode.ERROR

    async def test_turn_failed_increments_failures_counter_by_class(
        self,
        sink: OtelTelemetrySink,
        memory_meter_provider: tuple[MeterProvider, InMemoryMetricReader],
    ) -> None:
        """G3：turn_failed 按 failure_class 维度累计 taifeng.turn.failures。"""
        _, reader = memory_meter_provider
        await sink.handle(
            _event(
                "sub-f",
                "turn_failed",
                {
                    "error": "429",
                    "kind": "RateLimitError",
                    "failure_class": "provider_rate_limit",
                    "suggested_action": "退避后重试",
                    "iterations": 1,
                },
            )
        )
        data = reader.get_metrics_data()
        assert (
            _sum_counter(
                data,
                "taifeng.turn.failures",
                failure_class="provider_rate_limit",
            )
            == 1
        )

    async def test_thread_resumed_emits_generic_event(
        self,
        sink: OtelTelemetrySink,
        memory_tracer_provider: tuple[TracerProvider, InMemorySpanExporter],
    ) -> None:
        _, exporter = memory_tracer_provider
        await sink.handle(_event("sub-9", "turn_started"))
        await sink.handle(
            _event(
                "sub-9",
                "thread_resumed",
                {"thread_id": "thr-1", "item_count": 5},
            )
        )
        await sink.handle(_event("sub-9", "turn_completed"))
        spans = exporter.get_finished_spans()
        turn_span = next(s for s in spans if s.name == "taifeng.turn")
        names = [e.name for e in turn_span.events]
        assert "taifeng.event.thread_resumed" in names

    async def test_handle_swallows_exception(
        self,
        sink: OtelTelemetrySink,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(self: OtelTelemetrySink, ev: EventMsg) -> None:
            raise RuntimeError("synthetic boom")

        monkeypatch.setattr(OtelTelemetrySink, "_dispatch", _boom)

        import logging

        with caplog.at_level(logging.WARNING, logger="taifeng.telemetry.otel_sink"):
            await sink.handle(_event("sub-10", "turn_started"))
        assert any("synthetic boom" in rec.message for rec in caplog.records)


def _sum_counter(metrics_data: object, metric_name: str, **filters: str) -> int:
    """从 InMemoryMetricReader 数据中累加指定 counter 的 value。"""
    total = 0
    for rm in getattr(metrics_data, "resource_metrics", []):
        for sm in getattr(rm, "scope_metrics", []):
            for m in getattr(sm, "metrics", []):
                if m.name != metric_name:
                    continue
                for dp in m.data.data_points:
                    attrs = dict(dp.attributes or {})
                    if all(attrs.get(k) == v for k, v in filters.items()):
                        total += int(dp.value)
    return total


# ============ Requirement 5: close 超时 ============


class TestCloseTimeout:
    async def test_close_returns_within_reasonable_time(
        self, sink: OtelTelemetrySink
    ) -> None:
        import time

        t0 = time.monotonic()
        await sink.close(timeout_millis=100)
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < 1000, f"close 卡了 {elapsed_ms:.1f}ms"


# ============ Requirement 6: R1 业务零侵入静态扫描 ============


class TestR1NoBusinessTermInSource:
    BLACKLIST = ("tenant", "user_id", "audience", "患者", "病例", "管家", "匠人")

    def test_source_has_no_business_keywords(self) -> None:
        src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "taifeng"
            / "telemetry"
            / "otel_sink.py"
        )
        text = src.read_text(encoding="utf-8")
        for kw in self.BLACKLIST:
            assert kw not in text, f"R1 黑名单关键词 {kw!r} 出现在 otel_sink.py"


# ============ 辅助：_safe_attrs 单测 ============


class TestSafeAttrs:
    def test_filters_payload_blacklist(self) -> None:
        out = _safe_attrs(
            {
                "call_id": "c1",
                "name": "x",
                "arguments": "secret",
                "output": "more secret",
                "delta": "stream chunk",
                "summary": "subagent summary",
                "user_text_preview": "user input",
            }
        )
        assert out == {"call_id": "c1", "name": "x"}

    def test_stringifies_complex_types(self) -> None:
        out = _safe_attrs({"obj": {"a": 1}, "lst_mixed": [1, "x"]})
        assert isinstance(out["obj"], str)
        assert out["lst_mixed"] == [1, "x"]

    def test_drops_none(self) -> None:
        out = _safe_attrs({"a": None, "b": 1})
        assert "a" not in out
        assert out["b"] == 1
