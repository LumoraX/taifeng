# 公共 API 分层与弃用策略

> 决策记录：[ADR 0066](../decisions/0066-public-api-tiers-and-deprecation.md)、[ADR 0110](../decisions/0110-stable-layer-platform-surface.md)。本篇是现状，变更时直接更新。

taifeng 是被业务仓库按 PyPI 版本钉住使用的内核，公共 API 的边界必须明确：哪些可以放心依赖、哪些随时可能变、哪些根本不该碰。

## 三层

| 层 | 在哪里 | 兼容承诺 | 如何变更 |
| --- | --- | --- | --- |
| **稳定层** | `taifeng` 顶层 `__all__` | 不做不兼容修改；移除必须经过弃用期 | 增删都要同步改 `tests/public_api_snapshot.txt`（快照测试失败即提醒） |
| **实验层** | `taifeng.experimental.__all__` | 可在任意发布中不兼容地变化，变化记入 ADR | 契约（`docs/architecture/capabilities/`）标 🧪 的能力入口放这里；契约转 ✅ 后晋升顶层，并在本层保留同名导出至少一个发布版本 |
| **内部** | 其余子模块中未经上述两处导出的符号 | 无 | 随时可改；子包自己的 `__all__`（如 `taifeng.tool.builtins`）只表示模块内的组织，不构成稳定承诺，除非同时出现在顶层 |

当前实验层：strict audit Session 与 durable Journal（`AuditConfig`、`AuditCapabilityError`、`JsonlSessionJournalCore`、
审计 resume 人裁决 `AuditToolOutcomeRequest` / `AuditToolOutcomeResolution`、
审计模式下不适用的 `Resume` 抛出的 `AuditedResumeRejectedError`；Timeline 投影 `JournalTimelineProjector` /
`TimelineFilter` / `TimelineItem` / `TimelinePage`、脱敏 `redact_payload` / `RedactedPayload`、旧 transcript 导入
`import_legacy_transcript` / `LegacyImportResult` / `LegacyImportError`、投影重建 `rebuild_projections` /
`ProjectionRebuildResult`）、
Journal 确定性回放（`JournalReplayClient` 等；工具回放 `replay_tools` / `recorded_tool_calls` / `RecordedToolCall` /
`ToolReplayLedger` / `KERNEL_TOOLS`，整条重放 `replay_session` / `recorded_submissions` / `RecordedSubmission` /
`ReplayReport` / `ReplayStep`）、工具崩溃对账回查结果（`ReconcileVerdict`）、skill 战绩聚合（`SkillFitnessStore`、`SkillFitnessCatalog`、`SkillFitnessLedger`、`SkillFitness`、
`SkillFitnessRecorder`、`InMemorySkillFitnessStore`）、按战绩算分与影子评估（`FitnessScorer`、`WilsonFitnessScorer`、
`SkillFitnessScore`、`WorkingSetPolicy`、`WorkingSetPlan`、`plan_working_set`、`SkillFitnessShadow`、
`ShadowEvaluation`、`ShadowObserver`）、工作集生效与来源信任分层（`SkillWorkingSet`、`WorkingSetView`、`WorkingSetChange`、`TierRule`、`SkillTrustPolicy`、`SourceTrustPolicy`）、上下文引擎（`ContextEngine`、`AssembleRequest`、`AssembledContext`、`TurnUpdate`、`ContextEngineError`、`TailWindowContextEngine`）、预热（`Prewarm`、`ModelPrewarmer`、`PrewarmOutcome`、`CachePrimingPrewarmer`）、白名单外 skill 授权（`SkillAuthorizationPolicy`、`SkillAuthorizationRequest`、`SkillAuthorizationDecision`、`CallbackSkillAuthorization`、`PermissionSkillAuthorization`）、按选择置信度分流（`SkillSelectionGate`、`SelectionConfidencePolicy`、`ThresholdSelectionPolicy`、`SelectionCandidate`、`RoutedCandidate`、`TrialJudge`、`TrialVerdict`、`VerifierTrialJudge`）、输入来源标记（`InputOrigin`、`InputTaint`、`origin_of`、`summarize_taint`、`taint_from_extras`）、可声明的失败恢复配方（`RecoveryRecipeBook`、`RecipeDeclaringPolicy`、`RecoveryRecipeProvider`）、后台延迟压缩（`BackgroundCompactionStrategy`）、多模态重载荷驱逐（`MultimodalEvictionStrategy`）、用户文件输入（`FileAttachmentV1`、`FileInputPolicy`、`FilePart`）。

## 稳定层里有什么

稳定层覆盖「用内核搭一个产品」与「给内核写适配包」两类用法所需的全部入口：

- **会话与引擎**：`EnginePool`、`AgentEngine`、操作（`UserMessage` / `CancelTurn` / `Resume` / `SendToPeer` /
  `UpdateInstructions`）、事件；
- **可替换的协议及其签名类型**：`MessageStore`（`ThreadInfo`）、`AtomicBatchMessageStore`（`BatchAppendAck` /
  `BatchConflictError`，配 Responses 协议的模型客户端时必须实现）、`ThreadDirectory`、`MemoryStore`、
  `ModelClient`（`ModelClientSession`、`ModelCapabilities`）、`CompressionStrategy`（`CompressionTrigger`）、
  `CommandExecutor`、`ScriptExecutor`、`TelemetrySink`、`InputCostEstimator` 等；
- **内置工具工厂**：`make_*_tool` 全部在稳定层，包括 `EnginePool.create` 默认注册的四个；
- **模拟器**：`SimClient` / `SimTurn` / `RoutingSimClient` 及故障注入、断言类型，下游写测试不必自造模型客户端；
- **遥测**：`TelemetrySink`、`ConsoleSink`、`JsonlSink`、`attach_console_sink` / `attach_jsonl_sink`、OTel sink；
- **MCP**：stdio 与 streamable HTTP 客户端、`McpClient` 协议、`bind_mcp_tools` / `register_mcp_tools_async`、
  elicitation 类型与错误类型。

**规则**：稳定协议公开方法签名里出现的 taifeng 类必须在公共 API 里（协议公开即承诺了输入输出形状）。
`tests/test_public_api.py` 逐个协议核对，漏导出即红。

**晋升**：实验层名字转稳定时从 `taifeng.experimental.__all__` 移除，该模块的 `__getattr__` 保留同名入口
至少一个发布版本，访问时发 `DeprecationWarning` 提示改从顶层导入（当前：`McpHttpClient`、`McpToolBinding`、
`bind_mcp_tools`）。

## 类型标记

包内带 `py.typed`（PEP 561），下游 mypy / pyright 直接使用 taifeng 的类型注解，实现 taifeng 协议的适配包可以在 strict 模式下检查。
`tests/test_package_metadata.py` 守护源码中的标记；`scripts/verify_release_artifacts.py` 在干净环境安装 wheel 与 sdist 后再验一次，保证发出去的产物里也有。

## 弃用流程

1. 在 `src/taifeng/_deprecation.py` 的 `DEPRECATED_ALIASES` 登记旧名 → `DeprecatedAlias(target="模块:属性", since, removal_not_before, replacement)`，
   并从顶层 `__all__` 与快照中移除该名。
2. 旧名经 `taifeng.__getattr__` 解析：照常返回新位置的对象，同时发 `DeprecationWarning`，写明起始版本、最早移除日期与替代写法。
3. 弃用期至少跨**两个发布版本**且**不少于 30 天**；期满后删除登记项。
4. 每次弃用 / 移除在对应 ADR 或发布说明中记录。

`tests/test_public_api.py` 守护：快照一致、两层导出都可解析、实验层与稳定层不重叠、弃用别名告警且指向真实对象。
