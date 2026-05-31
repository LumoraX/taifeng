# telemetry-otel Specification

## Purpose
TBD - created by archiving change telemetry-otel-sink. Update Purpose after archive.
## Requirements
### Requirement: OtelSinkConfig 配置对象

系统 SHALL 提供 `taifeng.telemetry.OtelSinkConfig` —— `@dataclass(frozen=True)` 配置对象，字段如下：

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `service_name` | `str` | 必填 | OTel `service.name` 资源属性；业务侧注入（如 `"my-agent"`） |
| `service_version` | `str` | `taifeng.__version__` | OTel `service.version` 资源属性 |
| `otlp_endpoint` | `str \| None` | `None` | OTLP exporter 端点；`None` 时走 OTel SDK 环境变量默认（`OTEL_EXPORTER_OTLP_ENDPOINT`） |
| `protocol` | `Literal["grpc", "http/protobuf"]` | `"grpc"` | OTLP 传输协议 |
| `resource_attributes` | `dict[str, str]` | `{}` | 额外资源属性；业务侧注入；**SHALL NOT** 包含任何业务字段（R1） |
| `sampler` | `str \| None` | `None` | 采样器名称；`None` 走 OTel SDK 默认（`ParentBased(AlwaysOn)`） |

系统 SHALL 提供 `OtelSinkConfig.from_env() -> OtelSinkConfig` 类方法：从标准 `OTEL_*` 环境变量构造实例。`OTEL_SERVICE_NAME` 未设置时 SHALL 抛 `ValueError`（不静默默认）。

#### Scenario: 默认构造
- **WHEN** 调用 `OtelSinkConfig(service_name="my-svc")`
- **THEN** SHALL 返回实例
- **AND** `service_version` SHALL 等于 `taifeng.__version__`
- **AND** `protocol` SHALL 等于 `"grpc"`
- **AND** `resource_attributes` SHALL 是空 dict

#### Scenario: from_env 缺 service_name 报错
- **WHEN** 环境变量 `OTEL_SERVICE_NAME` 未设置
- **AND** 调用 `OtelSinkConfig.from_env()`
- **THEN** SHALL 抛 `ValueError`，错误信息明确指引设 `OTEL_SERVICE_NAME`

#### Scenario: 冻结不可变
- **WHEN** 已构造的实例尝试赋值 `config.service_name = "x"`
- **THEN** SHALL 抛 `dataclasses.FrozenInstanceError`

---

### Requirement: OtelTelemetrySink 实现 TelemetrySink 协议

系统 SHALL 提供 `taifeng.telemetry.OtelTelemetrySink` —— 实现 `taifeng.telemetry.TelemetrySink` 协议的类。

构造签名 SHALL 为：

```python
OtelTelemetrySink(
    config: OtelSinkConfig,
    *,
    tracer_provider: TracerProvider | None = None,  # 测试可注入 InMemorySpanExporter
    meter_provider: MeterProvider | None = None,
)
```

- 调用方未注入 provider 时，系统 SHALL 用配置构造默认 `TracerProvider` + `BatchSpanProcessor(OTLPSpanExporter(...))` 与 `MeterProvider` + `PeriodicExportingMetricReader(OTLPMetricExporter(...))`。
- 系统 SHALL 预建 2 个 metric counter：`taifeng.compaction.attempts` / `taifeng.cache.breaks`。（注：原计划 `taifeng.provider.retries` 暂搁 —— TF 当前未在主路径打 `ProviderRetry` 事件，待该事件落地后再补 counter。）
- 系统 SHALL 维护 `_span_stack: dict[str, Span]` —— key 是 turn_id / tool_call_id，用于跨方法维护 turn → tool span 嵌套关系；**SHALL NOT** 使用模块级全局变量。

#### Scenario: 默认构造创建本地 provider
- **WHEN** 调用 `OtelTelemetrySink(OtelSinkConfig(service_name="x"))`
- **THEN** SHALL 自动创建本地 TracerProvider / MeterProvider
- **AND** 不向外发任何网络请求（exporter 在 BatchSpanProcessor 内异步排队）

#### Scenario: 测试注入 InMemorySpanExporter
- **WHEN** 调用 `OtelTelemetrySink(cfg, tracer_provider=tp)` 且 `tp` 已绑定 `InMemorySpanExporter`
- **THEN** SHALL 使用注入的 provider，不创建默认 OTLP exporter

---

### Requirement: optional dependency 与延迟导入

`opentelemetry-api` / `opentelemetry-sdk` / `opentelemetry-exporter-otlp` SHALL 作为 `taifeng[telemetry-otel]` optional extra 而非默认依赖。

- `taifeng.telemetry.otel_sink` 模块顶部 SHALL 用 `try/except ImportError` 守护 OTel 包 import；缺包时 SHALL 在 `OtelTelemetrySink.__init__` 抛 `RuntimeError`，错误信息 SHALL 包含字符串 `"install taifeng[telemetry-otel]"`。
- `taifeng.telemetry.__init__` 与 `taifeng.__init__` SHALL 通过 PEP 562 `__getattr__` 钩子延迟暴露 `OtelTelemetrySink` / `OtelSinkConfig` —— 缺包时 `from taifeng.telemetry import *` 不抛错，只在真访问这两个名字时抛错。

#### Scenario: 未装 extra 时延迟报错
- **WHEN** 环境未安装 `opentelemetry-*`
- **AND** 调用 `from taifeng import OtelTelemetrySink`
- **THEN** SHALL 成功 import（延迟解析）
- **WHEN** 真调用 `OtelTelemetrySink(cfg)`
- **THEN** SHALL 抛 `RuntimeError` 含 `"install taifeng[telemetry-otel]"` 指引

---

### Requirement: EventMsg → OTel 映射

系统 SHALL 实现以下 `EventMsg` 类型到 OTel 信号的映射（其余 [src/taifeng/loop/event.py](../../../../../src/taifeng/loop/event.py) 中定义但本表未列的类型走通用 fallback：`event` 名 `taifeng.event.<kind>`，data dict 转 attribute）。**EventMsg 结构**：`EventMsg` 是 wrapper `{submission_id: str, msg: Msg (discriminated union), timestamp: datetime}`，业务字段在 `event.msg.data: dict[str, Any]` 里，按各事件类 docstring 约定（如 `ToolCallStarted.data = {"call_id", "name", "arguments"}`）。本规格映射表中的 attribute 取值路径以此结构为准。

| EventMsg 类（kind） | OTel 信号 | Attribute / Label 取值（从 `event.msg.data` 与 `event.submission_id`） |
|---|---|---|
| `TurnStarted` (`turn_started`) | `taifeng.turn` span 开始 | `submission_id`（来自 `event.submission_id`） |
| `TurnCompleted` (`turn_completed`) | `taifeng.turn` span 结束 | `iterations`, `duration_ms`, `end_reason`, `usage.*`（来自 `data`） |
| `TurnFailed` (`turn_failed`) | `taifeng.turn` span 结束 + status=ERROR | `error`, `kind`, `iterations` |
| `ToolCallStarted` (`tool_call_started`) | `taifeng.tool.<name>` child span 开始 | `tool.name`, `tool.call_id`（**SHALL NOT** 复制 `arguments` 字段进 attribute —— PII） |
| `ToolCallCompleted` (`tool_call_completed`) | `taifeng.tool.<name>` child span 结束 | `tool.is_error`, `tool.duration_ms`（**SHALL NOT** 复制 `output` 正文） |
| `SkillDispatched` (`skill_dispatched`) | `taifeng.skill.<id>` child span 开始 | `skill.id`, `skill.depth`, `skill.call_id` |
| `SkillReturned` (`skill_returned`) | `taifeng.skill.<id>` child span 结束 | `skill.success`（**SHALL NOT** 复制 `summary` 正文） |
| `AssistantText` (`assistant_text`) | 当前 turn span 的 `event=assistant_text` | `bytes`（来自 `len(data["delta"])`；**SHALL NOT** 含正文） |
| `CompactionStarted` (`compaction_started`) | event + counter `taifeng.compaction.attempts` +1 | `strategy`, `phase` |
| `CompactionCompleted` (`compaction_completed`) | event + 当 `cache_invalidated=True` 时 counter `taifeng.cache.breaks` +1 | `success`, `reason`, `removed_count` |
| `CacheBreakDetected` (`cache_break_detected`) | event + counter `taifeng.cache.breaks` +1 | `unexpected`, `reason` |
| `ThreadResumed` (`thread_resumed`) | event `thread_resumed` | `thread_id`, `item_count` |
| 其他 kind | event `taifeng.event.<kind>` | data dict 全量转 attribute（**SHALL NOT** 含 `arguments` / `output` / `delta` / `summary` 等正文字段） |

- 系统 SHALL NOT 在任何 attribute / event 中携带 prompt / tool input / tool output / assistant 正文（PII 风险）。
- 系统 SHALL 在每个 span 上注入 `taifeng.submission_id`（来自 `event.submission_id`）作为关联 attribute，方便跨 span 串联。
- `handle` SHALL fire-and-forget：任何映射异常 SHALL 被吞掉 + 打 `logger.warning`，**SHALL NOT** 向上传播。

#### Scenario: turn 与 tool span 嵌套关系
- **WHEN** 按顺序派 `TurnStarted` → `ToolCallStarted(data={"call_id":"c1","name":"file_read","arguments":"..."})` → `ToolCallCompleted(data={"call_id":"c1","name":"file_read","is_error":False,"duration_ms":12,"output":"..."})` → `TurnCompleted`（全部带相同 `submission_id`）
- **THEN** SHALL 产生 2 个 span
- **AND** `taifeng.tool.file_read` span 的 `parent_span_id` SHALL 等于 `taifeng.turn` span 的 span_id
- **AND** tool span attribute SHALL 含 `tool.name="file_read"` 与 `tool.call_id="c1"`，**SHALL NOT** 含 `arguments` 与 `output` 字段

#### Scenario: cache_breaks counter 累计
- **WHEN** 派 3 次 `CacheBreakDetected(data={"unexpected":True,"reason":"head_modified","token_drop":120})`
- **THEN** metric `taifeng.cache.breaks{reason="head_modified"}` SHALL 等于 `3`

#### Scenario: 映射异常被吞掉
- **WHEN** `_EventMapper.dispatch` 抛 `RuntimeError`
- **AND** 调用 `await sink.handle(event)`
- **THEN** SHALL 不向上传播异常
- **AND** SHALL 在 logger 打 WARNING 级别记录

---

### Requirement: 显式 close 与 timeout

系统 SHALL 提供 `async def close(self, *, timeout_millis: int = 5000) -> None`：

- SHALL 调用 TracerProvider 与 MeterProvider 的 `force_flush(timeout_millis)` 与 `shutdown()`。
- `close()` SHALL 在 `timeout_millis` 内返回，**SHALL NOT** 无限阻塞（R4）。

#### Scenario: close 在指定超时内返回
- **WHEN** 调用 `await sink.close(timeout_millis=10)`，BatchSpanProcessor 队列假装挂起
- **THEN** SHALL 在 ≤ 100ms 内返回（不卡死主流程）

---

### Requirement: R1 业务零侵入静态检查

系统 SHALL 在测试套件中包含一条静态 grep 测试：

- `src/taifeng/telemetry/otel_sink.py` 文件源代码 SHALL NOT 包含子串 `tenant` / `user_id` / `audience` / `患者` / `病例` / `管家` / `匠人`。

#### Scenario: 源码无业务关键词
- **WHEN** 静态扫描 `otel_sink.py` 源代码
- **THEN** R1 黑名单关键词出现次数 SHALL 等于 `0`

