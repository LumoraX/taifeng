# 能力契约（Capability Contracts）

> 本目录是 Taifeng 各能力的**稳定契约**——数据结构、协议签名、行为约束、事件分类、枚举值的权威定义。
> 架构叙述文档（`docs/architecture/*.md`）讲"模块如何协作"，本目录讲"每个能力的精确契约是什么"。
> 二者配合即构成完整设计：读 overview / 模块篇了解全貌，读这里查字段级细节。

每篇契约采用 EARS 风格组织：`Requirement`（能力要求）+ `Scenario`（行为场景）+ `数据契约` / `行为契约`（字段与约束）。

## 按模块域索引

### Skill 系统（对应 [skill-system.md](../skill-system.md)）
| 契约 | 覆盖 |
| --- | --- |
| [skill-dispatch](skill-dispatch.md) | call_skill 生命周期、Permission+Hook 双门控、`subagent_approval_mode`、`_SubagentAutoDecisionPolicy`、`reason` 字段流转、CallStack / DispatchVerdict 数据契约 |
| [skill-orchestration](skill-orchestration.md) | 声明式编排（parallel / serial / when）、加载期校验、执行语义（不采样 LLM）、`orchestration_plan_resolved` 事件 |

### 主循环 / 工具 / 基础设施（对应 [agent-loop.md](../agent-loop.md)）
| 契约 | 覆盖 |
| --- | --- |
| [hooks](hooks.md) | 8 种 HookKind、各 hook 数据载荷字段、调用点映射、`PreTurnHookDenied` / `PreCompactHookSkipped` 事件 |
| [instructions-injection](instructions-injection.md) | `InstructionSource` 协议、三档 scope 缓存、热更语义、fail-fast、5 类事件 |
| [permission-gate](permission-gate.md) | `PermissionRequest` 字段、工厂方法、`prompter_timeout_seconds`、`args_match` 三态匹配、`PermissionRule.parse` / `from_dict`、内核无状态约束 |
| [suspend-resume](suspend-resume.md) | `SuspendReason` 四值、`PendingRequest` / `SuspensionRecord`（↔ `suspension` ResponseItem）、`Resume` Op、`ResolvePlan` / `SuspensionResolver`、3 类 EventMsg、四 reason resume 语义、多挂起并存 + 禁部分 resume、resolved-marker 幂等、R4 丢弃、tier-1/2 + 跨进程重建 |
| [script-execution](script-execution.md) | `ScriptDescriptor` / `ScriptExecutor` 协议、隐式发现、subprocess 隔离、timeout / cancel、5 类事件 |
| [tool-builtins-extended](tool-builtins-extended.md) | `apply_patch` 两阶段原子、`BackgroundTaskRegistry`、`http_request`、各工具 `parallel_safe` |
| [mcp-server](mcp-server.md) | `McpStdioServer`、MCP 握手 / tools / resources、双向 JSON-RPC elicitation、CLI `mcp serve` |
| [telemetry-otel](telemetry-otel.md) | `OtelSinkConfig` / `OtelTelemetrySink`、EventMsg→OTel 映射表、PII 过滤、预建 counter、fire-and-forget |
| [turn-rewind](turn-rewind.md) | turn 内可寻址回访节点表（iteration / dispatch）、`Rewind` Op（re_reason / retry_tool 两切点）、`RewindCheckpoint`、`rewind_nodes()`、3 类事件、拒绝路径、R2 expected / R5 append-only |
| [detached-spawn](detached-spawn.md) | 分离式并发 spawn + join-barrier：`spawn_skill`（立即返回句柄、不阻塞）、独立 child thread 各自独立 HITL（staggered Resume 路由）、`JoinBarrier`（全终态自动聚合）、engine keepalive 引用计数、`kill_spawn` 隔离取消、K1 slot 仅计 running、冷恢复 rebuild、7 类事件、4 个 LLM 工具 |
| [reactive-compaction-recovery](reactive-compaction-recovery.md) | overflow 有界自愈：`force_compress`（绕 should_trigger）、`_maybe_compress(phase=overflow, bypass_trigger)`、`_overflow_recovered` 有界一次、`ProviderRetry` 事件、无压缩器/压缩失败退化、R2 cache-aware / R4 取消 |
| [compaction-surgical-trim](compaction-surgical-trim.md) | 手术刀档就地剪枝：dedup → soft-trim → hard-clear 三 pass、只改写 output payload 永不删条（配对安全）、cache-TTL 对齐触发、glob deny 优先、`CompressionResult.detail` 明细透传、幂等占位符守卫 |
| [turn-resource-guards](turn-resource-guards.md) | turn 级资源护栏：`DenialBreaker`（连续/滑窗 deny 单次断路、`denial_circuit_open`）、`IterationBudget`（consume/refund/child 分层派生，父子独立）、`ToolSpec.refunds_iteration`、单点记账 `_note_tool_outcome` |
| [midturn-input-steering](midturn-input-steering.md) | 运行中 turn 注入用户输入：`InjectUserInput` Op、`_PendingTurn.pending_input` 共享队列、`_drain_pending_input` 迭代边界排空、无活跃 turn 退化（codex inject_no_new_turn）、`UserInputInjected{delivered}` 事件、tool 配对保护 / R4 取消守卫 |

### 持久化（对应 [conversation.md](../conversation.md)）
| 契约 | 覆盖 |
| --- | --- |
| [jsonl-transcript](jsonl-transcript.md) | `MessageWriter` 协议、首行 metadata、POSIX 原子追加、损坏行容错、`resume_thread_id` / `initial_history`、`thread_resumed` 事件 |
| [thread-directory](thread-directory.md) | `ThreadMetadata` / `ThreadFilter` / `ThreadPage`、SQLite 自愈、`NullThreadDirectory`、异常分类（`ThreadNotFoundError` / `DirectoryError`） |
| [index-hook](index-hook.md) | `IndexHook` 协议、fire-and-forget 调用时机、异常隔离、shutdown grace period、`index_hook_failed` / `index_hook_abandoned` 事件 |

### LLM 客户端（对应 [llm-client.md](../llm-client.md)）
| 契约 | 覆盖 |
| --- | --- |
| [llm-provider-native](llm-provider-native.md) | native 四件套同构契约、ResponseEvent 流形状、各 provider 字段映射（Anthropic / Gemini / DeepSeek）、cache 字段优先级、错误分类、`record_cache_read` |
| [llm-structured-output](llm-structured-output.md) | `ResponseFormatSpec` 字段、`structured_output` 事件、provider 翻译、解析失败策略 |

### 工程约定
| 契约 | 覆盖 |
| --- | --- |
| [test-layout](test-layout.md) | `tests/` 目录组织约定（子目录对应 src 模块） |

---

> 历史说明：这些契约最初通过 spec-driven 工作流（proposal → tasks → spec → archive）产出，
> 稳定后从工作流目录提升到本处作为活文档的契约层。本仓库不再附带该工作流的过程产物。
