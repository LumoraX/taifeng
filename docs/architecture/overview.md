# 架构总览

> Taifeng 的 5 维度抽象 + 6 核心包 + 5 基础设施包。
>
> 本篇讲"模块如何协作"；各能力的**字段级精确契约**（数据结构 / 协议签名 / 事件分类 / 枚举 / 约束）见
> [`capabilities/`](capabilities/README.md)。两者配合即构成完整设计。

## 设计目标

Taifeng 是一个**与业务解耦**的通用 LLM Agent 引擎，目标覆盖 5 个维度：

| 维度 | 关键问题 | 模块 |
| --- | --- | --- |
| §1.1 Skill 系统 | 如何让 LLM 按需调用「文档化的技能」？ atomic / composite 如何递归组合？ | `skill/` |
| §1.2 主循环 | turn 怎么跑？并发怎么调度？取消怎么传？ | `loop/` + `tool/` |
| §1.3 持久化 | 会话怎么存？崩溃后怎么 resume？ | `conversation/` |
| §1.4 上下文压缩 | 上下文溢出时怎么压？怎么保 prompt cache？ | `context/` |
| §1.5 LLM 调用 | 多 provider 怎么统一？事件流怎么标准化？ | `llm/` |

非目标（**显式不做**）：
- ❌ 多租户 / 计费 / 权限治理 —— 业务层职责
- ❌ RAG / 向量检索 —— 业务层职责
- ❌ 工具实现（bash / file_ops / web_search）—— 应用层职责，Taifeng 只定义 `ToolSpec` 协议
- ❌ 前端 SSE 协议适配 —— 应用层在 `EventMsg` 上再做一层翻译
- ❌ Agent 概念 —— 见 ADR 0006，统一为 Skill 一个抽象，composite skill 替代 agent

## 模块切分（6 核心包 + 5 基础设施包）

> 遵循 ADR 0006「统一 Skill 模型」—— 没有 `agent/` 包；skill-to-skill 派发归入 `skill/dispatch.py`。
> 5 维度对应 6 个核心包（skill/tool/conversation/context/llm/loop）；其余 5 个为基础设施包
> （hooks/instructions/mcp/permission/telemetry），§1.6 指令注入对应 `instructions/`（详见 agent-loop.md）。

```
src/taifeng/
├── skill/        # §1.1
│   ├── definition.py     # SkillDefinition（type/entry/child_skills/max_call_depth/orchestration/requires/exposure）
│   ├── loader.py         # SKILL.md frontmatter + body 解析 + 静态环检测
│   ├── registry.py       # discover / get / snapshot(含 version) / reachable_graph
│   ├── dispatch.py       # CallStack / DispatchPolicy / 动态环检测（call_skill 入口在 tool/builtins/call_skill.py）
│   ├── eligibility.py    # G4 运行时资格门控（RuntimeCapabilities × SkillRequirements/SkillExposure）
│   ├── orchestration.py  # 声明式编排：解析 / 校验 / condition 提取
│   ├── watcher.py        # SKILL.md 热更 FileWatcher
│   └── scripts/          # SKILL.md scripts 执行器（executor / python / shell / types）
│
├── tool/         # §1.2 工具部分
│   ├── spec.py           # ToolSpec（含 parallel_safe 字段）
│   ├── registry.py       # ToolRegistry
│   ├── runtime.py        # ToolCallRuntime —— RwLock 并行 / 独占调度
│   └── builtins/         # 可选内置工具（业务侧按需 register）
│       │                 # core:   read_skill / call_skill（skill-as-context 范式）
│       │                 # io:     file_io（read/write）/ shell / apply_patch
│       │                 # net:    http_request（受 PermissionPolicy[scope=network] 审批）
│       │                 # bg:     background（run_in_background / wait_for_task）
│       │                 # script: run_script（SKILL.md scripts 执行）
│       └── ...           # 详见 docs/configurable-knobs.md §6
│
├── conversation/ # §1.3（store-protocol-decoupling 重写：三协议分离）
│   ├── models.py         # ResponseItem 统一消息单元
│   ├── protocols.py      # MessageWriter / ThreadDirectory / IndexHook 三协议
│   ├── store.py          # MessageStore（聚合便利门面）
│   ├── transcript.py     # JsonlMessageWriter —— JSONL append-only 主存（source-of-truth）
│   ├── sqlite_directory.py # SqliteThreadDirectory —— stdlib sqlite3 derived 索引（可重建）
│   ├── rebuild.py        # 索引从 JSONL 自愈重建
│   ├── hook_runner.py    # IndexHook 触发
│   └── errors.py         # DirectoryError / ThreadNotFoundError 等
│
├── context/      # §1.4
│   ├── budget.py         # ContextBudget（含 max_request_bytes 硬护栏 / G2b）
│   ├── compressor.py     # CompressionStrategy 协议 + CompressionContext/Trigger/Result（均 dataclass(frozen)）
│   ├── injection.py      # InitialContextInjection 枚举
│   ├── cache_stats.py    # PromptCacheStats + CacheBreakReason taxonomy
│   ├── memory.py         # K3 MemoryStore swap 协议（prefetch / writeback / on_pre_evict / on_session_end）
│   ├── truncate.py       # truncate_middle 中段截断（G6b）
│   └── strategies/
│       ├── handoff.py    # codex 范式：LLM-to-LLM 接力 + G1a 摘要质量审计 + 健康回滚 + 降级告警
│       └── sliding.py    # 滑窗 + 图像 marker
│
├── llm/          # §1.5
│   ├── client.py         # ModelClient (session 级) + ModelClientSession (turn 级)
│   ├── events.py         # ResponseEvent —— 11 类 EventKind（含 structured_output / prompt_cache / rate_limits）
│   ├── types.py          # ApiRequest / ApiMessage / ResponseFormatSpec（强类型输出 schema）
│   ├── retry.py          # 指数退避 + 服务端 hint delay
│   ├── errors.py         # LLMError 子类 + FailureClass（11 桶）+ suggested_action（G3）
│   ├── recovery.py       # G3 recommend_recovery → RecoveryPlan（机读恢复配方）
│   └── providers/        # native 五件套（OpenAICompat / Anthropic / Gemini / DeepSeek / LiteLLM 兜底）
│                         #   + mock + _shared.py（错误分类 / SSE / usage 统一）
│
├── loop/         # §1.2 主循环部分
│   ├── submission.py     # Submission / Op（12 种，全集见 docs/capability-matrix.md）
│   ├── event.py          # EventMsg（输出事件总线）
│   ├── engine.py         # AgentEngine —— 主 actor（注入 entry_skill）
│   ├── pool.py           # EnginePool —— 多 thread engine 复用 + resume
│   ├── turn.py           # TurnRunner —— 单轮采样 + tool 调度 + 压缩
│   ├── tool_batch.py     # dispatch_batch —— 一批 tool call 三段式并发派发
│   ├── orchestration_exec.py # 声明式编排执行器（检测到 orchestration 则跳过 LLM 采样）
│   ├── spawn.py          # K1 SpawnSlotRegistry —— 广度准入（fork-bomb 防护）
│   ├── prompt.py         # build_prompt + history ↔ api messages 转换
│   └── cancellation.py   # 父子 CancellationToken
│
├── hooks/        # PreToolUse / PostToolUse / PreCompact / PreTurn / Pre|PostSkillDispatch / Pre|PostScriptUse
├── instructions/ # §1.6 指令分层注入（InstructionResolver + InstructionSource 协议 + engine/session/turn 三档 scope）
├── mcp/          # MCP stdio client（连外部 server 注册 tools）+ server（taifeng 作为 MCP server）+ prompter
├── permission/   # HITL 审批：PermissionPolicy + Rule + Decision（per-builtin 权限模型，无中央门）
└── telemetry/    # TelemetrySink 协议 + Console / Jsonl / OTel 三 sink
```

## 数据流（一次 turn 的生命周期）

```
0. 业务层 build_engine_for(session, tenant, entry_skill_id)
   │  ├─ 订阅校验：entry_skill_id ∈ tenant.allowed_entry_skills
   │  ├─ 取 entry SkillDefinition（type=composite, entry=true）
   │  └─ AgentEngine(entry_skill=..., snapshot=..., model_client=..., store=...)
   │
0. EnginePool.get_or_create(resume_thread_id=...?)
   ├─ resume_thread_id=None → store.create_thread(...) 新开
   └─ resume_thread_id=非空 → store.load_thread(...) 物化历史 + emit thread_resumed
   │
   外部入站路径 (M3 mcp-server-mode)：
     MCP 客户端 (Claude Code / Cursor) → stdio JSON-RPC
       → McpStdioServer.run() → tools/call(run_skill_turn) → pool.get_or_create
       → engine.submit(UserMessage) → 等 turn_completed → 返回 final_text
   │
1. 用户提交 Submission(op=UserMessage)
   │
   ▼
2. AgentEngine 入队：user_message 持久化 + instruction resolve + 【pre_turn hook】
   │   └─ hook deny → emit pre_turn_hook_denied + turn_failed；不创建 TurnRunner
   │
   ▼
3. TurnRunner.run_turn(turn_ctx, cancel):
   │
   ├─ pre-sampling 压缩检查（动 head 允许）→ 【pre_compact hook】
   │   ├─ hook deny → emit pre_compact_hook_skipped；跳过本轮压缩；继续
   │   └─ CompressionStrategy.should_trigger("pre_turn") ?
   │       └─ if yes → strategy.compress(InitialContextInjection.BEFORE_LAST_USER_MESSAGE)
   │
   ├─ build_prompt(entry_skill, history, tool_specs)
   │   ├─ <entry_skill> body 注入 system prompt
   │   └─ <available_child_skills> 子 skill 列表（仅 id + description，不含 body）
   │
   ├─ ModelClientSession.stream(prompt) → AsyncIterator[ResponseEvent]
   │   ├─ TextDelta → emit EventMsg.AssistantText
   │   ├─ ToolCallDone(read_skill) → 取子 skill body 回流（独占）
   │   ├─ ToolCallDone(call_skill) → DispatchPolicy.check → 派子 TurnRunner
   │   │   ├─ 深度 / 环检测 / 白名单
   │   │   ├─ args 可含 ``reason``（LLM 自陈意图）→ PermissionRequest.reason
   │   │   │       → HITL UI / skill_dispatched.data["reason"] / telemetry 全链路可见
   │   │   └─ 子 turn 完成后结果 append 到 prompt.function_call_output
   │   ├─ ToolCallDone(其他) → ToolCallRuntime.dispatch（parallel_safe ? 读锁 : 写锁）
   │   ├─ ReasoningDelta → emit EventMsg.Reasoning
   │   └─ Completed → break inner loop
   │
   ├─ mid-turn 压缩检查（只能动 tail，保 cache anchor）→ 【pre_compact hook】
   │   ├─ hook deny → emit pre_compact_hook_skipped；history / anchor 不动
   │   └─ if token_limit_reached && needs_follow_up
   │       └─ strategy.compress(InitialContextInjection.DO_NOT_INJECT)
   │
   └─ MessageStore.append(ResponseItem[...])  ← JSONL flush
   │
   ▼
4. AgentEngine emit EventMsg.TurnComplete
```

## 关键设计红线

### R1 业务零侵入
Taifeng 引擎层**禁止 import 任何业务概念**。审 PR 红线：
- ❌ 业务子模块导入（无论何种命名空间）
- ❌ `tenant_id` / `audience` 参数（业务侧通过 `AgentPolicy` 钩子注入）
- ❌ 领域名词（任何语言；如医疗 / 金融 / 法律业务术语）

**权限策略也无状态**：`PermissionPolicy` 仅评估业务侧传入的 rules —— 不持久化、不自动学习 `remember="always"` 决策。业务侧若想 session/DB 级记忆，在自己的 prompter 包装层读 `decision.remember_until` 后自管。规则支持 args 级精确匹配（`PermissionRule.args_match`）+ Claude Code 风格语法糖（`PermissionRule.parse("Bash(openspec --help)", mode="allow")`）+ JSON/dict 直接加载（`PermissionPolicy.from_dict({"allow": [...], "deny": [...]})`）。

### R2 Cache 友好
所有压缩动作必须显式标注是否破坏 prompt cache anchor。`CompressionStrategy.compress()` 返回 `CompressionResult { cache_invalidated: bool, anchor_preserved_until: int }`。

### R3 可观测
关键路径打点：`turn_started` / `tool_call_started` / `tool_call_completed` / `compaction_started` / `compaction_completed` / `cache_break_detected`（事件类定义见 [src/taifeng/loop/event.py](../../src/taifeng/loop/event.py)）。Taifeng 不绑定具体后端，提供 `TelemetrySink` 协议。

#### Production: OTLP 接入

业务侧若想直接复用既有 OTel 后端（Jaeger / Tempo / Grafana / DataDog / 阿里云 ARMS / 腾讯云 APM 等），按以下步骤启用 `OtelTelemetrySink`：

```bash
uv pip install -e ".[telemetry-otel]"  # 默认依赖不含 OTel SDK
```

```python
import asyncio
from taifeng import AgentEngine, OtelSinkConfig, OtelTelemetrySink

# service_name / resource_attributes 由业务方决定；Taifeng **不内置任何业务字段**（R1）
config = OtelSinkConfig(
    service_name="my-agent",
    otlp_endpoint="http://otel-collector:4317",
    protocol="grpc",
    resource_attributes={"deployment.environment": "prod"},
)
sink = OtelTelemetrySink(config)

engine = AgentEngine(...)


async def _forward() -> None:
    async for ev in engine.subscribe_all():
        await sink.handle(ev)


asyncio.create_task(_forward())
# 应用 shutdown 时显式 flush
await sink.close(timeout_millis=5000)
```

行为要点（更详细的 EARS 规格见 `docs/architecture/capabilities/telemetry-otel.md`）：

- **不带正文**：`arguments` / `output` / `delta` / `summary` / `user_text_preview` 等正文字段**永不**落 OTel attribute（PII 风险）；业务侧要看正文走 `JsonlSink` + 自管脱敏。
- **Span 嵌套**：`taifeng.turn` 为根，`taifeng.tool.<name>` 与 `taifeng.skill.<id>` 为 child。
- **Counter**：预建 2 个 —— `taifeng.compaction.attempts{strategy=...}` 与 `taifeng.cache.breaks{reason=...}`。
- **fire-and-forget**：`sink.handle(ev)` 任何异常吞掉 + WARNING log，不破坏主 turn（R3）。
- **`from_env()`**：可从 `OTEL_SERVICE_NAME` / `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_PROTOCOL` 引导配置；`OTEL_SERVICE_NAME` 未设置时显式抛 `ValueError`，不静默默认。

### R4 可取消
每个 `TurnRunner.run_turn()` 必须接受 `CancellationToken`。子 agent 派发通过 `cancel.child()` 派生子 token，父取消→子级联取消。

### R5 可 resume
`MessageStore` 默认实现是 JSONL 追加写。业务侧如要落 DB，自行实现 `MessageStore` 协议并接 Taifeng 的 `AgentEngine(store=...)`。

## LLM Provider 选型

Taifeng 提供 **native 四件套 + LiteLLM 兜底** 的双层 provider 架构。native client 直连上游 HTTP API（httpx + SSE，零 SDK 依赖），LiteLLM 覆盖非主流 provider。

| 场景 | 推荐 client | 说明 |
| --- | --- | --- |
| OpenAI / 自部署 OpenAI-compat gateway (vLLM, Ollama, one-api, new-api) | `OpenAICompatClient` | native httpx SSE，含 `reasoning_content` 流式支持 |
| Anthropic Claude（含 cache_control 精准控制 / messages API 流式） | `AnthropicClient` | native messages API，零 anthropic-sdk 依赖；cache 元数据直接从 `message_start.usage` 取 |
| Google Gemini（AI Studio） | `GeminiClient` | native `streamGenerateContent`，零 google-genai-sdk 依赖；支持 query / header 双鉴权 |
| DeepSeek（V3 / R1） | `DeepSeekClient` | OpenAI-compat 薄子类，预设官方 base_url；`prompt_cache_hit_tokens` 精准映射 |
| AWS Bedrock / GCP Vertex / Azure OpenAI / Kimi / 其他自定义 endpoint | `LiteLLMClient` | 多 provider 兜底，依赖 LiteLLM SDK |

四家 native client + LiteLLM 共享统一 `ModelClient` 协议 + `ResponseEvent` 流形状（`created → server_model → text_delta* → tool_call_done* → prompt_cache → completed`）。错误分类共享 `providers/_shared.py::classify_http_error`（基于 HTTP status code，比 LiteLLM 的 message 关键字匹配精准）。

native 路径优势：
- 错误分类基于 httpx 异常类型（`ConnectError` / `ReadTimeout` 直接 → `TransientNetworkError`，可被 `retry_async` 重试），不再被 LiteLLM 黑盒包成 `InternalServerError` 误判
- cache 元数据精准（Anthropic `cache_creation_input_tokens` / DeepSeek `prompt_cache_hit_tokens` / Gemini `cachedContentTokenCount` 各自直读）
- 零额外 SDK 维护成本（不引入 `anthropic` / `google-generativeai` 等大型依赖包）

## 与既有项目的关系

| 项目 | 关系 |
| --- | --- |
| **codex / Claude Code / claw-code** | 范式参照。Taifeng 移植 `compact.rs` / `prompt_cache.rs` / `ModelClient` 的设计思想 |
| **LangGraph / AutoGen / Letta** | 不替代 —— 它们是图 / Actor / 记忆范式，Taifeng 是 codex 范式 |
| **LiteLLM** | 依赖 —— 可选 backend，统一 OpenAI / Anthropic / Gemini |

## 里程碑

| 里程碑 | 范围 | 状态 |
| --- | --- | --- |
| **M0** | 架构 + 决策文档 | ✅ 完成（2026-05-22） |
| **M1** | `llm/` + `conversation/` 可运行 | ✅ 完成（2026-05-23）—— MockClient + LiteLLM, JsonlMessageStore |
| **M2** | `skill/` + `tool/` | ✅ 完成（2026-05-23）—— Filesystem registry, RwLock dispatch, 内置工具 |
| **M3** | `context/` + `loop/` | ✅ 完成（2026-05-23）—— handoff 压缩, Engine + Pool |
| **M4** | 首个生产接入 | 🟡 待开始 —— 宿主业务把既有编排改造为 Taifeng 客户端 |
| **M5** | Telemetry / 增强 | ✅ 完成 —— OTel sink、SKILL.md 文件 watcher 热更、MCP server、声明式编排均已落地 |

> 当前测试：全量 `PYTHONPATH=src uv run pytest tests/` **759 passed**。能力完善度（P0/P1/P2 清零）见 `hermes-gap-roadmap.md`，内核子系统进度（K1–K4 已落地、K5 待补）见 `kernel-gap-analysis.md`。
>
> **业务方接入先看 [`docs/capability-matrix.md`](../capability-matrix.md)**——能力总览矩阵（能做什么 / 落地状态 / 入口 API / 示例 / 契约一表打尽）。
