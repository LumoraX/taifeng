# 业务侧可配置参数清单

> 所有配置都通过 **构造时参数** 或 **运行时 Op** 注入。引擎不读环境变量、不读全局配置文件。
> 参照范围：codex `Op::*` + claw-code `RuntimeFeatureConfig` + claude-code 用户配置项。

## 1. 构造时参数（EnginePool.create / AgentEngine.__init__）

| 参数 | 默认值 | 说明 | 对标 |
| --- | --- | --- | --- |
| `skills_dir` | (必填) | SKILL.md 加载根目录 | — |
| `threads_dir` | (必填) | JSONL 持久化目录 | — |
| `model_client` | (必填) | `ModelClient` 实现（OpenAI Chat / OpenAI Responses / OpenAICompat / Anthropic / Gemini / DeepSeek / LiteLLM / Sim 任选其一，详见 §1.3） | codex `ModelClient` |
| `extra_tools` | `[]` | 额外注册的 `ToolSpec` | claw-code custom tools |
| `compressors` | `[Handoff, Sliding]` | `CompressionStrategy` 列表；空列表禁用压缩。内置四档谱系：`OffloadStrategy`（无损落盘+stub，超大 tool 结果优先无损回收，构造参 `file_root` / `offload_bytes_threshold=8192` / `preview_head_lines·tail_lines=5`，应与 `file_read` 工具同 `root_dir`，参数见 [capabilities/compaction-offload-strategy.md](architecture/capabilities/compaction-offload-strategy.md)）/ `SurgicalTrimStrategy`（手术刀就地剪枝，推荐最高优先级，参数见 [capabilities/compaction-surgical-trim.md](architecture/capabilities/compaction-surgical-trim.md)）/ `HandoffCompactionStrategy`（LLM 摘要；构造参 `quality_max_attempts=2`、`preserve_user_message_tokens=20000`——被压缩区间最近用户原话原样保留的 token 预算，0 关闭，ADR 0059）/ `SlidingWindowStrategy`（滑窗兜底） | codex `CompactionConfig` |
| `budget` | `ContextBudget()` 默认 200k window / 0.85 soft / 0.95 hard / 4 tail | 整体 token 预算 | claw-code `auto_compaction_input_tokens_threshold` |
| `dispatch_policy` | `DispatchPolicy()` | call_skill 派发策略 | — |
| `hooks` | `None` | `HookRunner`（PreToolUse/PostToolUse/PreCompact） | claw-code hooks |
| `max_iterations` | `32` | 单 turn 内 LLM ↔ tool 最大循环 | claw-code `max_iterations` |
| **`denial_breaker_config`** | `None` | turn 内连续拒绝断路器（`DenialBreakerConfig{max_consecutive_denials, max_recent_denials, window_size}`）。None=不启用零变化；越阈值 emit `denial_circuit_open` + turn 以同名 end_reason 提前终止。详见 [capabilities/turn-resource-guards.md](architecture/capabilities/turn-resource-guards.md) | codex `guardian` 断路器 |
| **`doom_loop_config`** | `None` | turn 内重复同 `(tool,args)` 成功调用空转的先警后断守卫（`DoomLoopConfig{max_consecutive_repeats}`）。None=不启用零变化；连续 N 次同签名 → 注中性事实 + emit `doom_loop_warned`，警后到 2N → emit `doom_loop_circuit_open` + 以同名 end_reason 终止。详见 [capabilities/turn-resource-guards.md](architecture/capabilities/turn-resource-guards.md) | opencode 重复调用检测（ADR 0021） |
| **`failure_policy`** | `None` | `FailureDispositionPolicy`；失败处置裁决（挂起 vs 终态）。`None` = 内置 `ConservativeFailurePolicy`（可恢复 LLM 错误挂起、其余终态——历史行为零变化）；注入 `SuspendByDefaultPolicy` 后一切失败（含确定性 LLM 失败与三类护栏触顶）转挂起等 Resume 裁决（护栏触顶以 `RESOURCE_LIMIT` reason 落挂起，retry=重建续跑/abort=终态）。**仅适合有人值守或有自动决策器的部署**。详见 [capabilities/suspend-resume.md](architecture/capabilities/suspend-resume.md) | — |
| **`auto_retry`** / **`retry_config`** | `True` / `None` | **内核默认套有界重试**（ADR 0041）：`AgentEngine` / `AgentEnginePool` / `EnginePool.create` 三处经 `with_default_retry` 把 `model_client` 包成 `RetryingModelClient`——幂等：已套过（`bounded_retry` 标记，台账录制等 `__getattr__` 透明包装可穿透）/ strict audit 适配器 / `auto_retry=False` 均原样。`retry_config=None` → `RetryConfig()`（3 次、500ms 起指数退避封顶 30s、尊重服务端 hint、四类零产出可重试 kind，见 §7）；`auto_retry=False` 关闭（业务自管重试，或测试复现「重试已耗尽」）。池级包装同时覆盖压缩摘要 / skill recall 的 LLM 侧调用；每次重试 emit `provider_retry`（ADR 0039） | — |
| **`failure_suspend_max_auto_retries`** | `None` | TTL 自动 retry 的谱系上限（resource-limit-retry-semantics）：同一失败谱系经 N 次「到期自动 retry → 再失败再挂起」后,下次到期强制 abort 并在 `suspension_expired.data` 标注 `auto_retry_exhausted: true`。None = 不限——**配 `on_expire="retry"` 时强烈建议设置**,否则确定性失败会无界自动循环烧钱。人工 Resume 不计数 | — |
| **`failure_suspend_ttl_seconds`** / **`failure_suspend_on_expire`** | `None` / `"abort"` | 内核自产挂起（SYSTEM_RETRY / RESOURCE_LIMIT）的存活期与到期动作（suspension-ttl）。None = 永不过期；无人值守部署配 ttl + `"retry"` 实现「限流/触顶到期自动续跑」。业务挂起的 ttl 在 `make_request_user_input_tool(ttl_seconds=...)` 工厂声明（DATA 到期恒 abort）。详见 [capabilities/suspend-resume.md](architecture/capabilities/suspend-resume.md) §挂起存活期 | — |
| **`now_factory`** | `time.time` | 壁钟工厂（TTL 到期计算用）；测试注入固定时钟可让到期立即触发 | — |
| **`tool_recovery`** | `"suspend"` | 冷恢复时，执行途中崩溃、非幂等且无法回查的工具调用怎么处置：`"suspend"` 落 `TOOL_OUTCOME_UNKNOWN` 挂起交人（retry / provide / abort）；`"report"` 回填「结果未知，不重试」。pure / idempotent 工具与提供 `ToolSpec.reconcile` 的工具不受影响（ADR 0045）。只作用于非审计路径：strict audit resume 从不自动回填「结果未知」，人裁决走 `AuditConfig.tool_outcome_resolver`（ADR 0070） | ADR 0025 recovery |
| **`max_parallel_tool_calls`** | `1` | 单 turn 内**一批** tool call 的最大并发数（一条 assistant 消息里的多个 tool call / call_skill）。默认 `1` = 严格串行（等同历史行为，零回归）；设 `>1` 开启并发 fan-out。安全由 `ToolCallRuntime` 的 RwLock 兜底（parallel_safe 读类重叠、写类独占；call_skill 跳锁真并行）。结果按发起序配对回填历史，cache / resume 不受影响。声明式编排（SKILL.md `orchestration`）的 parallel 段同样受此旋钮限流，serial 段无论该值多大都强制串行。详见 `docs/architecture/agent-loop.md` 并发派发段 + 编排 turn 段 | codex `tools/parallel.rs` |
| **`reasoning_passback`** | `True` | thinking 模型 reasoning 回传开关（reasoning-content-passback）。开：prompt 重建把落史的 `reasoning` item 附回该采样轮的合并 assistant 消息，provider 组装为 `reasoning_content`（deepseek-v4 等 thinking 模型对带 tool_calls 的 assistant 消息的续传硬性要求，不回传则挂起恢复/多轮续跑被 400 拒）。回传天然自限：history 无 reasoning item 即不回传，非 thinking 模型零变化；关闭场景仅限同 thread 中途从 thinking 切换到严格拒绝未知字段的 provider。落史本身无旋钮（R5）。详见 `docs/architecture/llm-client.md` reasoning 回传节 | — |
| **`image_input_policy`** | `None`（= disabled） | 图片输入总闸与资源策略。只有显式传 `ImageInputPolicy(enabled=True, max_images=..., max_item_bytes=..., max_total_bytes=..., allowed_media_types=...)` 才接受图片；在 durable append 前验证 canonical base64、decoded size、SHA-256、MIME/header、dimensions 与单帧约束。root/child/spawn/resume runner 均继承 | — |
| **`input_cost_estimator`** | `None`（启图时走 policy ceiling） | 图片 token 估算策略。可注入 `OpenAIImageCostEstimator()`；未知模型或未注入时使用 `unknown_model_token_ceiling` 非零上界。最终 wire bytes 仍由 `ContextBudget.max_request_bytes` 精确门禁 | — |
| **`file_input_policy`** 🧪 | `None`（= disabled） | 用户文件（首批 PDF）输入总闸与资源策略（[llm-file-input](architecture/capabilities/llm-file-input.md)）。只有显式传 `FileInputPolicy(enabled=True, max_files=..., max_item_bytes=..., max_total_bytes=..., allowed_media_types=..., page_token_ceiling=..., unknown_file_token_ceiling=...)` 且 client 声明 `"file"` 能力才接受文件；在 durable append 前验证数量、MIME 白名单、canonical base64、decoded size、SHA-256 与 PDF 头尾结构。token 估算 = PDF 页数 × `page_token_ceiling`（默认 5000），页数未知或策略未启用时每个文件取 `unknown_file_token_ceiling`（默认 32768）。root/child/spawn/resume runner 均继承；strict audit 模式下带文件的消息在 acceptance 前显式拒绝 | — |
| `auto_watch_skills` | `False` | 启用 SKILL.md 文件监听热更 | — |
| `watch_poll_interval_seconds` | `1.0` | 文件监听轮询间隔 | — |
| **`instruction_layers`** | `None` | `list[InstructionLayer]`；业务侧注入的系统指令层（类似 codex 的 AGENTS.md 角色，但走协议而非读文件）。详见下方 §1.1 | codex `~/.codex/AGENTS.md` |
| **`script_executors`** | `{}` | `dict[ScriptLanguage, ScriptExecutor]`；run_script 工具按 `descriptor.language` 在此查执行器。src 默认提供 `ShellScriptExecutor / PythonScriptExecutor`；未注入对应 language → `no_executor_for_language` 错误。详见 ADR 0009 / §1.2 | — |
| **`event_queue_size`** | `65536` | 每个 `subscribe*()` 创建的事件队列 maxsize。**审计可观测 层1**：默认放大到 65536（按最慢逐条 fsync 落盘消费者 ~6500 events/s × 10 估算），有界 ⇒ 内存天花板可预测（~128MB/队列）、绝不 OOM；`<=0` 为无界 opt-in（业务自负 OOM）。满则丢弃 + 计数 + `logger.warning`，消费者凭 `(session_id, seq)` / `delivery_seq` 自检。详见 [audit-observability](architecture/capabilities/audit-observability.md) | claw-code `EventChannelConfig` |
| **`event_high_water_ratio`** | `0.75` | 有界事件队列高水位比例。`qsize` 上穿 → 一条 high-water `logger.warning`（告警走 logger 不走事件，防自放大） | — |
| **`event_low_water_ratio`** | `0.5` | 有界事件队列低水位比例。回落到此以下才重新武装下次告警（迟滞，防阈值附近刷屏） | — |
| **`event_warn_cooldown_sec`** | `5` | 高水位告警限频秒数（即便持续高位，至多每 N 秒一条） | — |
| **`terminal_replay_size`** | `256` | 晚到订阅者终态补投窗口（ADR 0031）。engine 记住每个 submission 最后一条终结事件（`turn_completed`/`turn_failed`/`turn_suspended`）；`subscribe(submission_id)` 在 submission 已终态后建立时，立即补投**那条真实事件**而不是永久挂死。有界 FIFO，超出淘汰最老的（退化回等待）；`<=0` 关闭补投。未记账的 submission 维持等待（subscribe 早于 submit 是推荐用法）。详见 [audit-observability](architecture/capabilities/audit-observability.md) | — |
| **`enable_request_capture`** | `False` | **审计可观测 层1**：LLM request 留痕开关。开启后 build 后发送前 emit `LlmRequestRecorded`（retry/重建各一条）；文字正文仍敏感，但图片 base64/Data URL 在内核中替换为 MIME/size/detail/`content_redacted` 描述。OtelSink 按 kind 跳过不外发。默认关 = 零泄漏面 + 零行为变化 | codex request 留痕 |
| **`skill_recall`** | `None`（= inline） | **skill 召回（发现）后端**：决定召回阶梯走哪一层。`None`（默认）= **inline / 工作记忆 / LLM 注意力**——全部可见 child 内联进 prompt 让 LLM 自己找，**不**注册 `search_skills`、**不**启用 deferred、**绝不**静默兜底关键词。可注入可选后端：`KeywordSkillRecall`（零依赖 BM25-lite，确定性）/ `LlmSkillRecall(model_client)`（LLM-as-recall，**非确定性**，pool 须能放进一次 prompt）/ 业务 RAG（实现 `SkillRecall` 协议，ADR 0017③）。注入后端后才启用 `search_skills` + deferred。在 `EnginePool.create`。详见 [capabilities/skill-recall.md](architecture/capabilities/skill-recall.md) | — |
| **`recall_threshold`** | `50` | **skill 召回（发现）**：**仅在注入了召回后端时生效**。composite entry 的 `child_recall: auto` 模式下，**有后端**且 G4 过滤后可见 child 数 > 本阈值 → 自动切 deferred（child 装不进一次 prompt，改 `search_skills` 按需召回），否则 inline；**无后端恒 inline**。`child_recall: inline` 强制内联、`child_recall: deferred` 显式要召回（无后端则启动期抛 `SkillValidationError`），均优先于本阈值。`0` = 有后端且 child 数 >0 即 deferred。在 `EnginePool.create`。详见 [capabilities/skill-recall.md](architecture/capabilities/skill-recall.md) | — |
| **`recall_default_top_k`** | `5` | **skill 召回（发现）**：**仅在注入了召回后端时生效**。`search_skills` 工具未显式传 `top_k` 时的默认召回候选数。必须 ≥1（否则构造期 `ValueError`）。在 `EnginePool.create` | — |
| **`recall_max_top_k`** | `20` | **skill 召回（发现）**：**仅在注入了召回后端时生效**。`search_skills` 工具 `top_k` 的上界（LLM 传更大值被夹到此）。必须 ≥ `recall_default_top_k`（否则构造期 `ValueError`）。在 `EnginePool.create` | — |
| **`enable_auto_discovery`** | `False` | **skill 自动发现 opt-in 总闸（ADR 0024）**：让「超量子 skill 自动走 LLM 召回 + 验证」开箱即用，**不**改 `skill_recall=None=inline` 零成本默认。`False`（默认）= 维持现状（inline、不验证、零额外 LLM）。`True` = 在未显式注入处自动兜底——`skill_recall=None` 自动 `LlmSkillRecall(model_client)`、`skill_verifier=None` 自动 `LlmSkillVerifier(model_client)`，deferred 仍按 `recall_threshold` 伸缩。**显式注入优先于总闸**；显式 `child_recall: deferred` 但既无注入又没开总闸 → 抛 `SkillValidationError`。在 `EnginePool.create` / `EnginePool.__init__`。详见 [capabilities/skill-recall.md](architecture/capabilities/skill-recall.md) | — |
| **`skill_verifier`** | `None` | **skill 召回后验证门后端（ADR 0024）**：与 `skill_recall` 正交。`None`（默认）= 不验证（召回直接路由），除非开 `enable_auto_discovery`（开后自动补 `LlmSkillVerifier`）。可注入 `LlmSkillVerifier(model_client)`（拉完整 SKILL.md body 判**输入要求适配**、滤误召）或业务自实现的 `SkillVerifier` 协议（规则 / 向量）。显式注入即启用验证（不依赖总闸，可「keyword 召回 + LLM 验证」）。在 `EnginePool.create`。详见 [capabilities/skill-recall.md](architecture/capabilities/skill-recall.md) | — |
| **`verify_max_candidates`** | `5` | **skill 验证（C2 护栏）**：`LlmSkillVerifier` 只对召回头部前 N 个候选精验（召回宽筛、验证精验少量，控成本 / prompt 体积）。`LlmSkillVerifier.__init__` 参数（业务构造 verifier 时传）。详见 [capabilities/skill-recall.md](architecture/capabilities/skill-recall.md) | — |
| **`verify_body_char_limit`** | `4000` | **skill 验证（C2 护栏）**：`LlmSkillVerifier` 单个 SKILL.md body 入验证 prompt 的字符上限，超出截前缀（截断标记注入候选 `reason`，审计可见）。`LlmSkillVerifier.__init__` 参数。详见 [capabilities/skill-recall.md](architecture/capabilities/skill-recall.md) | — |

#### §1.0 内核资源/内存旋钮（K1–K4，业务可注入）

> 这五个旋钮是把 taifeng 当 OS 微内核时的"资源准入 / 强制 / 流控 / 内存层级"配置。**机制在内核，上限值/后端是策略，由业务注入**（守 R1）。全部开箱即有安全默认；不注入也能跑。

| 参数 | 默认值 | 内核维度 | 说明 | 对标 |
| --- | --- | --- | --- | --- |
| **`max_concurrent_spawns`** | `16` | K1 广度准入 | 单 engine 内并发**在飞**（running）detached spawn 的上限。防 fork-bomb。超限时内核把 `SpawnLimitError` 转成 `SkillSpawnRejected` **事件** + `ToolResult.error`。⚠️ **nuance**：只统计 `runner.run()` in-flight 的 spawn；HITL 挂起的 spawn **退栈即释放 slot**（suspended 不计并发），resume / rewind 重推经统一驱动重新占用——满额时**排队等待**而非拒绝（被 kill / 根取消即放弃；ADR 0035）——HITL 等待期不消耗并发额度。详见 [detached-spawn 契约](architecture/capabilities/detached-spawn.md) §K1 | codex `agent/registry.rs::reserve_spawn_slot` |
| **`max_total_spawns`** | `1000` | K1 广度准入 | 单 engine 生命周期内累计 spawn 上限（单调递增，不回收；兜底 runaway 循环），与并发上限独立 | codex 同上 |
| **`max_session_tokens`** | `None` | K2 资源强制 | 会话累计 token 硬天花板（OOM-killer）。累计口径 = 整棵 turn 树（根 + call_skill 子 turn + detached spawn + 续跑链）每次采样实时入账（ADR 0044）。`None`=不强制（只告警）。设值后：跨 turn 累计触顶 → pre-turn 拒新 turn（`turn_refused`）；turn 内触顶且有后续 tool call → `ResourceLimitExceeded(turn_aborted)` 事件 + 停采样 | codex `UsageLimitReached` |
| **`memory_store`** | `None` | K3 内存层级 | `MemoryStore` 协议实现（长期记忆 swap/缺页接口）：`prefetch` 换入注入 prompt 尾部 / `writeback` 脏页写回 / `on_pre_evict` 换出前抢救 digest / `on_session_end` teardown。`None`=无内存层级（=`NullMemoryStore`）。全 best-effort（钩子异常不打断 turn）。后端（向量库/KV/RAG）是 **userspace**，业务自接。协议见 `src/taifeng/context/memory.py`。**最简只读接入**：继承 `NullMemoryStore` 仅覆写 `prefetch`；**多源**：`CompositeMemoryStore([知识库, 会话记忆])` fan-out 组合（单子异常不传染）。**模型主动读写**：`extra_tools=[make_memory_tool(store)]`（见 §6.8） | hermes `memory_provider.py`（剔业务字段） |
| **`memory_query_builder`** | `None` | prefetch 检索语境定制 | 同步 `(history: list[ResponseItem]) -> str`：从当前 history **拷贝**自由构造检索 query（如近 N 轮拼接，解多轮指代）。`None`=默认（最后一条用户消息文本）。builder 异常 → 记日志回退默认（best-effort 域）。demo：`examples/memory/knowledge_demo.py` | 集成工效（memory-integration-ergonomics） |
| **`pinned_state_sources`** | `None` | E1 压缩后状态保活 | `list[PinnedStateSource]`（name / max_chars / `format_for_injection()`，同步协议）：压缩成功后按注册序渲染并以 `system_injection(source="pinned:<name>")` 钉回 history 尾（R5 持久化）。`None`/空=零行为变化。运行时增删走 `engine.register_pinned_state` / `unregister_pinned_state`。详见 [capabilities/postcompact-state-reinjection.md](architecture/capabilities/postcompact-state-reinjection.md) | hermes 压缩后重注入 `todo_snapshot`（协议化） |
| **`pinned_total_max_chars`** | `8000` | pinned 注入总预算 | 单轮注入字符总预算（按注册序累计，装不下的 source 整体丢弃并记入事件 `dropped`）；per-source 上限由各 source 的 `max_chars` 控制（truncate_middle 截断） | 防 pinned 反噬压缩收益 |
| **`submission_queue_size`** | `256` | K4 入站流控 | 入站 submission 队列 maxsize（bounded backpressure）。满则 `submit()` 在 `put` 处 await（业务侧自然阻塞），不丢提交。与 `event_queue_size`（出站）成对 | codex bounded 入站队列 |

以下 13 项此前漏登记（Wave 4 补齐；必填依赖如 `store` / `tool_runtime` 不属旋钮，不列）：

| 参数 | 默认值 | 说明 | 对标 |
| --- | --- | --- | --- |
| `outcome_judge` | `None` | skill 战绩判定器（`skill-outcome-record`）；`None` 则不记战绩 | — |
| `storage_dir` | `None` | 通用存储根目录（战绩 / 索引等）；`None` 则退回 `threads_dir` 同级 | — |
| `thread_directory` | `None` | `ThreadDirectory` 实现；`None` 用默认 JSONL 目录 | — |
| `index_hook` | `None` | `IndexHook` —— 落盘后建索引的旁路钩子（`index-hook` 契约） | — |
| `sink` | `None` | `TelemetrySink` —— 事件外发后端；`None` 不外发（R3 仍在总线上） | codex telemetry |
| `permission_policy` | `None` | `PermissionPolicy` —— HITL 审批策略；`None` 则工具全放行 | claw-code permission |
| `request_metadata` | `None` | 透传给 provider 的请求级 metadata（业务侧标签） | — |
| `audit` | `None` | `AuditConfig` —— 开启审计模式（strict 下与多 attempt 客户端互斥，见 ADR 0037）。`AuditConfig.tool_outcome_resolver`（默认 `None`）：resume 时对回查也查不清的结果未知工具调用征求人裁决（`provide` / `abort`，以 operator actor 落账）；`None` = 不问人，这类调用令 resume 拒绝（ADR 0070） | — |
| `hook_runner` | `None` | `HookRunner` —— PreToolUse / PostToolUse / PreCompact / PreTurn / PostTurn 钩子编排 | claw-code hooks |
| `initial_history` | `None` | engine 构造时预置的 history（冷恢复重建用） | — |
| `compaction_degradation_threshold` | `3` | 连续压缩无进展多少次后 emit `CompactionDegradationWarning` | — |
| `capabilities` | `None` | 覆写模型输入能力探测结果（图片 / Responses 协议判定） | — |
| `has_recall_backend` | `False` | 是否已接记忆召回后端；影响 `skill-recall` 提示注入 | — |

### `get_or_create` 运行时参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `session_id` | (必填) | engine 缓存 key；同 id 重复调用返回既有 engine |
| `entry_skill_id` | (必填) | 入口 skill（必须 `entry=true`） |
| `cwd` | `None` | 业务工作目录（写入 thread 元数据，新建 thread 时生效） |
| **`resume_thread_id`** | `None` | 从已存在的 thread 续接历史。`None` → 新建 thread（默认行为）；非 None → 调 `store.load_thread` 物化历史 + 用同一 thread_id 构造 engine + emit `thread_resumed` 事件。未知 thread_id 抛 `ValueError`，不静默回退。session_id 已 cache 命中时本参数被忽略。详见 change `engine-resume-by-thread-id` |

### ContextBudget 字段
```python
ContextBudget(
    context_window=200_000,      # 模型上下文窗口
    soft_limit_ratio=0.85,       # 触发 mid-turn 压缩的占比
    hard_limit_ratio=0.95,       # 必须压缩否则报错的占比
    preserve_tail_messages=4,    # 压缩时保留尾部消息数
    max_request_bytes=None,      # 发送前请求体字节硬上限（G2b）
    output_reserve_tokens=0,     # 输出预留下限：soft/hard 按「窗口 - 生效预留」计算（ADR 0043）；
                                 # 生效预留 = max(本值, entry skill 的 inference.max_output_tokens)（ADR 0071）
    max_tool_result_bytes=128 * 1024,  # 单条工具结果进历史前的字节上限，超限保头尾；None=不限；配 OffloadStrategy 时不生效（ADR 0061）
)
```

> 占用估算以 provider 实测 usage 校准（ADR 0043）：首次采样后 `engine.estimate_tokens()` =
> 实测 prompt token + 之后新增条目粗估，不再只是 `len/3.5`。
>
> 输出预留联动（ADR 0071）：每个 turn 的 soft / hard 按 `max(output_reserve_tokens, entry skill 的
> inference.max_output_tokens)` 扣除后计算（`TurnRunner.effective_budget`）；call_skill / spawn 子 turn 各按
> 自己 entry skill 的声明。未声明时与配置值一致；声明值 `>= context_window` → 该 turn 以
> `OutputReserveExceedsWindowError` 显式 `turn_failed`（`UpdateBudget` 缩小窗口后同理）。

### §1.1 InstructionLayer 字段（instructions-injection）

```python
from taifeng import InstructionLayer

InstructionLayer(
    name="global-policy",         # 全局唯一；UpdateInstructions 用此 key 热更
    source="text or InstructionSource",  # str 静态 / Protocol 动态
    scope="engine",               # 'engine' | 'session' | 'turn'
    cache_ttl_seconds=0,          # >0 时 (name, session, entry_skill) 缓存
    priority=10,                  # 升序拼接到 prompt
    cache_volatile=True,          # snapshot 标记是否破 prompt cache
)
```

**三档 scope 生命周期**：
- `engine`：`EnginePool.create` 时 resolve 一次，进程内复用
- `session`：每个 `AgentEngine` 实例缓存；`UpdateInstructions` 可热更
- `turn`：每次 turn 启动前 resolve（受 cache_ttl 控制）

**业务侧实现 InstructionSource**：

```python
from taifeng import InstructionContext, InstructionSource

class TenantPolicySource:
    """业务侧：按租户读取合规策略文本。"""
    async def fetch(self, ctx: InstructionContext) -> str | None:
        # 业务自决：从 DB / 配置中心 / S3 / etc. 拉
        # ⚠️ 禁止在 fetch 内发起 HITL 询问（弹窗 / SSE 等）—— 见 spec D6
        # 领域上下文（如租户）走开放 metadata，taifeng 不解析（R1）
        text = await my_db.query_policy(tenant=ctx.metadata.get("tenant"))
        return text  # None 表示本次不注入
```

> **D5 fail-fast**：fetch 抛任何异常 → 包成 `InstructionFetchError` → 该 turn 失败。**禁止** silent fallback 到空字符串。
>
> **D6 fetch 不发起 HITL**：动作级 HITL 走 `PermissionPolicy + PermissionPrompter`；数据级权限在 `fetch` 内部直接 raise 或返回 None。

### DispatchPolicy 通过 SKILL.md 控制
- `max_call_depth`（每个 entry skill 自己声明，默认 6）
- `child_skills` 白名单（composite skill 声明）

### §1.3 LLM Provider 构造参数（native 四件套 + LiteLLM 兜底）

选型参见 `docs/architecture/overview.md §LLM Provider 选型`。所有 client 实现统一 `ModelClient` 协议。

#### `OpenAICompatClient`（OpenAI / vLLM / Ollama / one-api 等 OpenAI-compat gateway）

`OpenAICompatClient` 保持 text-only，不会因为模型名是 GPT-5.6 自动开启图片，也不写官方 OpenAI 新字段。官方 OpenAI 请在下面两种协议中显式选择。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | (必填) | Bearer token |
| `base_url` | (必填) | API 根地址（不含 `/v1/chat/completions` 路径） |
| `model` | `"gpt-4o-mini"` | 默认模型名 |
| `extra_headers` | `None` | 额外 header（如自部署网关 token） |
| `timeout_seconds` | `300.0` | httpx 超时 |
| `trust_finish_reason` | `True` | 端点是否如实上报 `finish_reason`。置 `False` 时零产出的 `content_filter` 归入**可重试**的 `UnreliableFinishError` 而非终态 `ContentFilterError`——用于会把上游未知 finishReason 塌缩成 `content_filter` 的兼容网关（见 llm-client.md §异常终止与空回复） |

#### `OpenAIChatClient`（官方 Chat Completions，文字 + 图片）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | (必填) | OpenAI Bearer token |
| `model` | `"gpt-5.6"` | 默认模型名 |
| `base_url` | `"https://api.openai.com/v1"` | API 根地址；固定调用 `/chat/completions` |
| `extra_headers` | `None` | 额外 header |
| `timeout_seconds` | `300.0` | httpx 超时 |

#### `OpenAIResponsesClient`（官方 Responses，文字 + 图片）

构造参数与 `OpenAIChatClient` 相同，但固定调用 `/responses`；请求始终 `store=false`、`include=["reasoning.encrypted_content"]`，不支持 `previous_response_id`。跨进程恢复由 `get_or_create(resume_thread_id=...)` 和 Taifeng JSONL 完成。

#### `AnthropicClient`（Anthropic Claude API）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | (必填) | ANTHROPIC_API_KEY |
| `model` | `"claude-haiku-4-5-20251001"` | 默认模型名（不带 LiteLLM 前缀） |
| `base_url` | `"https://api.anthropic.com"` | API 根地址 |
| `anthropic_version` | `"2023-06-01"` | API 版本 header |
| `extra_headers` | `None` | 额外 header（third-party gateway 用） |
| `timeout_seconds` | `300.0` | httpx 超时 |
| `thinking_budget_tokens` | `None` | extended thinking 预算（≥1024；请求级 `reasoning_effort` 覆盖；ADR 0046） |
| `cache_tail` | `True` | 除 cache anchor 外在最后一条消息再打 `cache_control`（尾部滚动断点）：工具循环里上一轮前缀按缓存价读取。`False` = 只打 anchor（ADR 0062） |
| `cache_ttl_seconds` | `None` | 统一覆盖所有缓存标记的 TTL，只接受 `300` / `3600`（1h 档写入价约为 2 倍，适合间隔超过 5 分钟的会话）；`None` = 按断点声明（默认 300） |

#### `GeminiClient`（Google AI Studio Gemini）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | (必填) | GEMINI_API_KEY |
| `model` | `"gemini-2.0-flash-exp"` | 默认模型名 |
| `base_url` | `"https://generativelanguage.googleapis.com"` | API 根地址 |
| `auth_via` | `"query"` | `"query"`（URL `?key=`）或 `"header"`（`x-goog-api-key`） |
| `extra_headers` | `None` | 额外 header |
| `timeout_seconds` | `300.0` | httpx 超时 |

#### `DeepSeekClient`（DeepSeek V3 / R1，OpenAICompatClient 薄子类）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | (必填) | DEEPSEEK_API_KEY |
| `model` | `"deepseek-chat"` | `deepseek-chat`（V3）或 `deepseek-reasoner`（R1） |
| `base_url` | `"https://api.deepseek.com"` | API 根地址 |
| `extra_headers` | `None` | 额外 header |
| `timeout_seconds` | `300.0` | httpx 超时 |

#### `LiteLLMClient`（Bedrock / Vertex / Azure / 其他 endpoint 兜底）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | `None` | 取决于 provider 前缀 |
| `model` | `"gpt-4o-mini"` | LiteLLM 风格前缀，如 `"anthropic/claude-..."` / `"bedrock/anthropic.claude-..."` |
| `base_url` | `None` | provider-specific |
| `extra_params` | `{}` | 透传到 `litellm.acompletion(**extra_params)` |

## 2. 运行时 Op（engine.submit(Op))

| Op | 作用 | 参数 |
| --- | --- | --- |
| `UserMessage` | 提交用户消息 | `text`, `attachments` |
| `CancelTurn` / `Interrupt` | 中止指定 submission | `submission_id` |
| **`CompactNow`** | 业务主动触发压缩 | `target_tokens` / `preserve_tail` / `strategy` / `force` |
| `InjectSystemMessage` | 注入业务 system 消息 | `text`, `source` |
| **`ThreadRollback`** | 回滚最近 N 轮对话 | `num_turns` |
| **`UserMessage(deadline_seconds=)`** | 本 turn 墙钟上限（含其全部 call_skill 子 turn）；到点 `cancel_reason="deadline_exceeded"`（ADR 0049） | `deadline_seconds: float > 0` |
| **`UpdateBudget`** | 运行时调整 ContextBudget（只覆盖显式字段，其余保留；非法组合拒绝并保持原值） | `context_window` / `soft_limit_ratio` / `hard_limit_ratio` / `preserve_tail_messages` / `output_reserve_tokens` |
| **`RefreshSnapshot`** | 拉最新 SkillSnapshot | — |
| **`UpdateInstructions`** | 热更指定 layer 的 source；缓存立即失效；下个 turn 生效 | `layer_name`, `new_source` (str 或 `InstructionSource`) |
| **`Resume`** | 续跑一个挂起的 thread（`end_reason="suspended"` 的 turn）。配对 `resolutions` → 补齐 history-gap → 续采样。详见 §8 与 [suspend-resume 契约](architecture/capabilities/suspend-resume.md) | `thread_id`, `resolutions: {request_id: payload}` |
| **`Rewind`** | 回退到某回访节点并重推。`re_reason` 截到节点采样前重采样（LLM 重决下游）；`retry_tool`（仅 dispatch 节点）保留 assistant 的 function_call、只重跑该工具。缺省作用于 root turn（配 `engine.rewind_nodes()` 取节点）；`thread_id` 指向 spawn 子 thread 时对其截断重推（失败 spawn 从失败步人工 retry，配 `engine.rewind_nodes_for(tid)` 取节点）。详见 [turn-rewind 契约](architecture/capabilities/turn-rewind.md) + ADR 0014/0018 | `node_id`, `mode ∈ {re_reason, retry_tool}`, `new_args?`, `thread_id?` |
| `Shutdown` | 关闭 engine | — |

加粗的 6 个是本轮新增。

> `UpdateInstructions` 行为：成功 → 发 `instruction_updated` EventMsg；未知 `layer_name` → 发 `instruction_update_rejected(reason='unknown_layer')` 且 layers 不修改（spec D4）。

## 3. 运行时 Engine 公开属性 / 方法

```python
engine.budget                  # 当前 ContextBudget（只读引用）
engine.snapshot                # 当前 SkillSnapshot（只读引用）
engine.max_iterations          # 单 turn 最大循环数
engine.max_parallel_tool_calls # 单 turn 一批 tool call 的最大并发数（构造期注入，只读）
engine.entry_skill             # SkillDefinition（只读）
engine.thread_id               # 当前 thread_id
engine.history_snapshot()      # list[ResponseItem] 副本（业务侧只读）
engine.rewind_nodes()          # list[RewindCheckpoint]：最近一次 root turn 的回访节点表（供 Rewind Op / UI 渲染可点节点）
await engine.rewind_nodes_for(tid)  # 按 thread_id 查节点表（spawn 子 thread 从 store 推导;根 tid 等价 rewind_nodes()）
engine.estimate_tokens()       # 当前 history 估算 token 数
engine.usage_ratio()           # 占 context_window 的比例 (0.0~1.0+)
engine.instructions_snapshot() # list[ResolvedInstruction] 副本（按 priority 升序，frozen）
engine.events_dropped          # K4：出站事件累计丢弃数（慢消费者自检漏事件；lossy-but-accounted）
engine.introspect()            # K6：/proc 式只读快照（见下）

# ── detached-spawn API（业务直调，详见 detached-spawn 契约 + ADR 0015）──────────
engine.spawn_skill(            # 分离发起：立即返回 {handle_id, child_thread_id}；门控：未知/非白名单/超限 → raise
    *, skill_id, args, reason  #   reason 必填；gates: unknown_skill(ValueError) / dispatch_rejected(ValueError) / SpawnLimitError
)
engine.set_join_barrier(       # 注册 join-barrier：全终态自动触发聚合；返回 {barrier_id}
    handle_ids,                #   每个 handle 必须已知；then_skill_id 必须在 snapshot 存在（不校验 entry 资格）
    then_skill_id,
    then_args_template=None    #   None → 聚合 args = {hid: {status, result}} for ALL handles（含 failed/cancelled）
)
engine.spawn_status(           # 非阻塞读各句柄状态；未知 handle_id → {status:"unknown",result:None}（不 raise）
    handle_ids,                #   返回 {hid: {status, result}}
)
engine.kill_spawn(handle_id)   # R4：取消单个 spawn token；terminal → no-op；unknown → KeyError
engine.has_live_spawns()       # bool：True iff 有 running/suspended 句柄，或有在飞的自有 detached task（含 barrier 聚合 turn）

await engine.submit(op)        # 入队 Op
async for ev in engine.subscribe(sub_id):    # 订阅指定 submission 事件
async for ev in engine.subscribe_all():      # 订阅全部事件
await engine.shutdown()
```

### §3.1 K6 自省面：`engine.introspect()` / `pool.introspect()`

业务侧/运维做 "ps / top" 式观测的**主入口**——纯读、无副作用。`engine.introspect()` 返回单 engine 快照，`pool.introspect()` 返回 `{session_id: snapshot}`（各活跃 engine 一览）。

```python
snap = engine.introspect()
# {
#   "thread_id", "entry_skill_id", "running",
#   "pending_submissions": [sub_id, ...],          # 在飞 turn 的纯 ID 视图（向后兼容）
#   "pending": [{"submission_id", "cancel_requested"}],  # 逐条取消态（K6 增强）
#   "turn_index",
#   "spawn":   {"active", "total", "max_concurrent", "max_total"},  # K1 配额
#   "session_tokens", "max_session_tokens",        # K2 资源（整棵 turn 树共享账，ADR 0044）
#   "usage": {"total_tokens", "by_skill": {...}, "by_thread": {...}},  # 用量归因
#   "events_dropped",                              # K4 出站丢弃
#   "context_tokens", "context_window",            # 上下文占用
#   "cache": {"hits", "misses", "unexpected_breaks"},  # G-CACHE 健康度
# }
pool_view = pool.introspect()   # {"s1": snap1, "s2": snap2, ...}
```

> **存活/卡死（staleness）判定留宿主**：`pending[].cancel_requested` 暴露"取消已请求但 turn 尚未收尾"这一事实；"卡了多久算 stalled"需要墙钟 + 阈值（策略），按 R1 由宿主跨两次 `introspect()` 采样 + 自有时钟判定。内核不内置心跳计时器（对标 claw-code `lane_board` 的存活看板，taifeng 只提供其可纯读的那一半）。

### 典型使用：业务侧主动决定何时压缩

```python
# 业务层定时检查 token 用量
async def maybe_compact_periodically(engine: taifeng.AgentEngine) -> None:
    if engine.usage_ratio() >= 0.7:    # 业务自定义阈值
        await engine.submit(CompactNow(
            preserve_tail=6,            # 多保留一些 tail
            force=True,                 # 强制（即使未达 budget soft_limit）
        ))
```

### 动态调整模型 context window

```python
# 用户从 8k 模型切到 200k 模型
await engine.submit(UpdateBudget(context_window=200_000, soft_limit_ratio=0.9))
```

### 撤销最近一次错误对话

```python
# 用户说"上一个回答错了，重来"
await engine.submit(ThreadRollback(num_turns=1))
await engine.submit(UserMessage(text="重新问：..."))
```

### 热加载新增的 skill

```python
# 选项 A：Pool 自动 watch
pool = await taifeng.EnginePool.create(
    ..., auto_watch_skills=True, watch_poll_interval_seconds=2.0,
)
# 选项 B：业务侧手动触发
await pool.skill_registry.discover()
await engine.submit(RefreshSnapshot())
```

## 4. SKILL.md frontmatter 字段（per-skill 配置）

```yaml
---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite              # atomic | composite
entry: true                  # 是否可作为会话入口
model: claude-sonnet-4-6     # entry skill 偏好模型（空 → 用 client 默认）
inference:                   # 推理参数（可选，atomic / composite 通用；未声明 → provider 默认）
  reasoning_effort: high     # none | minimal | low | medium | high
  temperature: 0             # [0, 2]
  max_output_tokens: 2048    # >= 1；非法值 / 未知键加载期报错；同时抬高本 turn 的上下文输出预留（ADR 0071）
child_skills: [...]          # composite 必填
tool_names: [...]            # 显式允许的额外工具
max_call_depth: 6            # 递归深度上限
scripts:                     # 见 ADR 0009；可省略走隐式发现
  - name: normalize
    path: scripts/normalize.sh
    language: shell           # shell | python | custom
    timeout_seconds: 30       # 显式声明必填
    description: 把 CSV 标准化
    args_schema:
      type: object
      properties: {input: {type: string}}
      required: [input]
    max_output_bytes: 16384   # 默认 16KB
---
```

> 未显式声明 `scripts:` 时，loader 自动扫描 `scripts/*.{sh,py,js,ts}` 隐式生成 `ScriptDescriptor`（默认 `timeout_seconds=60 / args_schema={"type":"object"}`）。`path` 必须落在 skill 目录下；显式 + 隐式同名以显式为准。运行入口走 `run_script` 内置工具，受 `script_executors` 与 `PermissionPolicy(scope='script_exec')` + `pre/post_script_use` hook 三层门控。

## 5. ToolSpec 字段（per-tool 配置）

```python
ToolSpec(
    name="...",
    description="...",
    input_schema={...},
    handler=...,
    parallel_safe=True,          # 决定 RwLock 读锁/写锁
    timeout_seconds=60.0,        # 单次调用超时（崩溃恢复回查同样受此限时）
    # 副作用声明（ADR 0025）：决定执行途中崩溃后怎么恢复（ADR 0045）
    effect_kind="pure",          # pure / idempotent / reconcilable / external_non_idempotent
    reconciliation="none",       # none / query / retry / manual
    reconcile=None,              # async (arguments, call_id) -> ReconcileVerdict；回查真实结局
)
```

> **有副作用的业务工具请显式声明 `effect_kind`**：默认 `pure` 的工具在崩溃恢复时会被告知模型
> 「可安全重发」。声明为 `external_non_idempotent` 后，崩溃遗留调用会挂起交人裁决；能向外部系统
> 查询结局的工具可提供 `reconcile`，恢复时自动补写真实结果。

## 6. PermissionPolicy（可选）

```python
PermissionPolicy(
    rules=[
        PermissionRule(scope="file_read", target_pattern="re:^/data/", mode="allow"),
        PermissionRule(scope="shell_exec", target_pattern="re:rm ", mode="deny"),
        # ADR 0010：skill_dispatch scope 现在受 PermissionPolicy 控制
        PermissionRule(
            scope="skill_dispatch",
            target_pattern="premium-deep-analysis",
            mode="deny",
            reason="free_tier_blocked",
        ),
    ],
    default_mode="ask",                # allow | deny | ask
    prompter=CliPrompter() | CallbackPrompter(...),
    # === ADR 0010 引入的字段 ===
    prompter_timeout_seconds=60.0,     # 0 = 不超时（默认）；生产建议显式设值
    telemetry=my_telemetry_callback,   # async (kind, payload) -> None；用于 permission_prompt_timeout 事件
)
```

### `prompter_timeout_seconds` 详解

- `0` = 不包 `anyio.fail_after`（性能不退化）；业务 prompter 自己负责超时
- `> 0` = 用 `anyio.fail_after(N)` 包裹 prompter.prompt(...)
  - 超时取消 prompter 内 await
  - 发 EventMsg `permission_prompt_timeout`（含 scope / target / call_chain）
  - 返回 `PermissionDecision.deny(reason="prompter_timeout_{N}s")`
  - **不重试** —— 业务 prompter 自决
- 生产建议：人机审批 60–300s；CLI 实时操作 5–10s

### `telemetry` 回调签名

```python
async def my_telemetry_callback(
    event_kind: str,        # permission_prompt_timeout / permission_grant_issued
                            #   / permission_grant_hit / permission_grant_expired
    payload: dict[str, Any], # scope / target / timeout_seconds / call_chain / thread_id / submission_id
) -> None:
    # 推荐：写监控系统 + 告警；超时频繁 = 业务 prompter 实现有 bug 或用户体验差
    ...
```

### 可复用审批 grant（ADR 0022，运行时 API）

把一次性预批（`preapprove`）推广为作用域化、有确定性生命周期的复用凭证——
「人会在这个 ask 上点的 yes，提前缓存」。grant 只绕过弹窗、**绝不越过 `deny` 规则**。

```python
from taifeng import PermissionGrant

# 预种 / resume 重种：让所有命中的 ask 自动放行（不再问人）
policy.issue_grant(PermissionGrant(
    scope="tool_use", target_pattern="shell_exec",
    args_match={"cmd": "glob:openspec *"},   # 复用 PermissionRule 匹配
    max_uses=10,                              # 确定性生命周期；None=不限次（禁挂钟 TTL）
    call_chain_prefix=("root", "expert"),     # ()=全树；收窄【仅 call_skill 嵌套子树】
    thread_id="",                             # ""=任意；收窄到【spawn/peer detached 子】用此键
))
policy.revoke_grant(grant_id)                 # 主动撤销（业务做挂钟 TTL 用）

# 或：prompter 答复时顺带签发（内核自动记账，后续同模 ask 复用）
return PermissionDecision.allow(grant=PermissionGrant(scope="tool_use", target_pattern="shell_exec"))
```

- **两个收窄键各管一种嵌套**：`call_chain_prefix` 仅对 call_skill 阻塞嵌套子树有效（detached spawn 的 chain 会重置，故对它永不命中）；`thread_id` 才是收窄 spawn/peer detached 子的键
- **仅 inherit 模式生效**：`auto_deny`/`auto_allow` 子 turn 有意绕过交互式审批（含 grant），grant 在其中不生效（硬墙）
- **挂钟 TTL** 不在内核（`src/` 禁 `Date.now` 保 resume 确定性）：业务注入 clock 后自行 `revoke_grant`
- **生命周期同 `rules`**：grant 是内存 policy 态，进程内跨 turn 存活；**内核不跨进程自动持久化**，业务用 `snapshot()`+`issue_grant()` 重种（与重建 rules 同理，**不**像 preapprove 由 engine 自动注入）
- id 全局唯一（自动跳过占用、显式重复 raise）+ 按匹配签名 dedup（issue 幂等、mint 不堆积）
- 命中/签发/失效发 `permission_grant_*` telemetry（审计）

### Hook lifecycle 完整集

`HookKind` 共 9 种，对应 9 个调用点（自 `hook-wiring-pre-compact-pre-turn` 起所有 kind 均已挂接，无 dead code）：

| Hook kind | 触发位置 | deny / 异常影响 |
|---|---|---|
| `pre_turn` | `AgentEngine._run_turn_for`：user_message 已持久化 + instruction resolve 完成后，TurnRunner 实例化前 | deny → emit `pre_turn_hook_denied` + `turn_failed`；TurnRunner 不实例化；`_turn_index` 仍 +1 |
| `post_turn` | `AgentEngine._fire_post_turn_hook`（`_build_and_run_runner` 收尾）：状态回写后、本 turn task 内（**收尾的同步一步**），仅 root turn 真终态（suspended/cancelled 跳过） | 审计型不可否决；deny/异常仅写日志；emit `post_turn_hook_fired`；R4 经 `ctx.extras["cancel"]` 传 token。**引擎不串行化相邻 turn**——要跨 turn 顺序须等 `post_turn_hook_fired` 再提交下一轮 |
| `pre_compact` | `TurnRunner._maybe_compress`：budget 阈值判断后，`CompactionStarted` 之前（pre_turn / mid_turn / manual 三阶段都触发） | deny → emit `pre_compact_hook_skipped`；history / cache_anchor 不动；turn 继续 |
| `pre_tool_use` | 工具执行前 | deny → ToolResult.error（reason=`hook_denied`） |
| `post_tool_use` | 工具执行后 | 仅审计 |
| **`pre_skill_dispatch`** *(ADR 0010)* | call_skill：DispatchPolicy 通过后、PermissionPolicy 之前 | deny → ToolResult.error + emit `skill_dispatch_hook_denied` |
| **`post_skill_dispatch`** *(ADR 0010)* | call_skill：sub_turn 完成后（成功 / 失败都触发） | **仅审计**（run_audit_only：deny / 异常都吞掉） |
| `pre_script_use` | run_script：args_schema 校验通过后、executor 调用前 | deny → ToolResult.error；metadata `args_override` 可改写 args |
| `post_script_use` | run_script：executor 返回后 | **仅审计** |

业务侧只订阅需要的 hook kind；未注册的 kind 不触发任何 handler。

## 6.5 持久化层（store-protocol-decoupling）

`EnginePool.create` 新增三个可选参数 + 一个共享 sink，控制 thread 持久化与索引层。

```python
EnginePool.create(
    skills_dir=...,
    storage_dir=Path("./data"),              # JSONL 主存根目录（兼容旧 threads_dir=）
    model_client=...,
    thread_directory=None,                   # 默认 SqliteThreadDirectory；业务可换 Redis/PG/Null
    index_hook=None,                         # 默认 NoopIndexHook；业务可订阅 thread 事件
    sink=None,                               # 可选 TelemetrySink，接收 hook / 持久化层事件
)
```

### 替换 ThreadDirectory（业务接管索引）

```python
from examples.redis_thread_directory import RedisThreadDirectory

pool = await EnginePool.create(
    skills_dir=...,
    storage_dir=Path("./data"),              # JSONL 仍走本地
    thread_directory=RedisThreadDirectory(url="redis://prod:6379"),
    model_client=...,
)
```

业务侧只实现 4 个 `ThreadDirectory` async 方法（`list_threads / get_metadata / update_metadata / upsert_metadata`），约 30-80 行；taifeng 内部自动透传调用。

### 订阅 IndexHook（业务事件投递）

```python
from examples.audit_index_hook import AuditLogHook

pool = await EnginePool.create(
    skills_dir=...,
    storage_dir=...,
    model_client=...,
    index_hook=AuditLogHook(audit_path=Path("/var/log/audit.log")),
    sink=my_telemetry_sink,                  # hook 失败事件由 sink 接收
)
```

- `on_thread_created / on_message_appended / on_metadata_updated` 三方法 fire-and-forget 调用
- 业务方自决投递目标（审计文件 / Kafka / ES / metric）
- 失败发 `index_hook_failed` 事件；shutdown 5s grace 后 cancel + `index_hook_abandoned`

### 显式关索引（嵌入式场景）

```python
from taifeng import NullThreadDirectory

pool = await EnginePool.create(
    ...,
    thread_directory=NullThreadDirectory(),  # list_threads 永远空；不打开 sqlite
)
```

适合：单 thread CLI / 测试 / 业务已有自己的 session 列表管理。

详见 ADR `docs/decisions/0008-store-protocol-decoupling.md`。

---

## 7. RetryConfig（LLM 重试，业务可自定义）

```python
RetryConfig(
    max_attempts=3,
    min_delay_ms=500,
    max_delay_ms=30_000,
    backoff_multiplier=2.0,
    jitter=0.2,
    respect_server_hint=True,
    retryable_kinds=frozenset(
        {"rate_limit", "transient_network", "server_error", "unreliable_finish"}
    ),
)
```

- 每次退避重试在退避**之前** emit `provider_retry`（`reason` = 错误 kind，另带 `attempt` / `max_attempts` / `delay_seconds` / `failure_class` / `error_kind` / `transport_phase` / `retry_after_seconds`），OTel `taifeng.provider.retries` 按 `reason` / `failure_class` 计数——`TurnRunner` 自动接入，业务侧无需接线（ADR 0039）。
- `retryable_kinds` 是显式白名单，只匹配 `LLMError.kind`；`unreliable_finish`（接入方声明 `trust_finish_reason=False` 时网关错标的零产出 `content_filter`）已并入默认集合（ADR 0041）。
- **无需手工套**：引擎 / 池默认已包装（上表 `auto_retry` / `retry_config`）；显式 `RetryingModelClient(client, config=...)` 仍可用，引擎探测到已套即沿用你的配置。

## 7.6 BreakerConfig（provider 断路器，默认不套）

> 业务侧显式包装才生效：`CircuitBreakingModelClient(client, config=BreakerConfig(...))`，再把它作为 `model_client=` 传给 `EnginePool.create`。**内核不默认套**——阈值与上游形态强相关（ADR 0042）。

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `trip_after` | `int` | `3` | 连续多少次「重试耗尽后的最终失败」跳闸。配 `max_attempts=3` ⇒ 跳闸前最多烧 9 次 attempt |
| `cooldown_seconds` | `float` | `30.0` | 首次跳闸后的冷却时长；期间 `stream` 不触网，直接抛 `CircuitOpenError` |
| `cooldown_multiplier` | `float` | `2.0` | 半开探测再失败时冷却的增长倍数 |
| `max_cooldown_seconds` | `float` | `300.0` | 冷却上限，防止长故障把冷却放大到不可恢复 |

- 构造参数另有 `retry_config`（inner 未套重试时自动补的策略）与 `clock`（默认 `time.monotonic`，测试注入假时钟用）。
- 三态转换 emit `provider_circuit_opened` / `provider_circuit_half_open` / `provider_circuit_closed`，OTel counter `taifeng.provider.circuit_transitions`（按 `to_state` / `failure_class`）——`TurnRunner` 自动接入，业务侧无需接线。
- `circuit_open` 是 `retryable=True` 的 `LLMError` ⇒ 保守策略落 SUSPEND；配 `failure_suspend_ttl_seconds` + `on_expire="retry"` 时，到期自动 retry 打在 open 态上不触网、成本≈0，再配 `failure_suspend_max_auto_retries` 即有界。
- 详见 [capabilities/provider-circuit-breaker.md](architecture/capabilities/provider-circuit-breaker.md)。

## 7.5 OtelSinkConfig（OTel / OTLP 出口，可选 extra）

> 业务侧 `uv pip install -e ".[telemetry-otel]"` 启用；详见 [架构总览 §R4](architecture/overview.md#r4-可观测)。

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `service_name` | `str` | 必填 | OTel `service.name` 资源属性；业务侧注入（如 `"my-agent"`） |
| `service_version` | `str` | `taifeng.__version__` | OTel `service.version` 资源属性 |
| `otlp_endpoint` | `str \| None` | `None` | OTLP exporter 端点；`None` 时走 `OTEL_EXPORTER_OTLP_ENDPOINT` env |
| `protocol` | `Literal["grpc", "http/protobuf"]` | `"grpc"` | OTLP 传输协议 |
| `resource_attributes` | `dict[str, str]` | `{}` | 额外资源属性；**SHALL NOT** 含任何业务字段（R1） |
| `sampler` | `str \| None` | `None` | 采样器名称；`None` 走 OTel SDK 默认 `ParentBased(AlwaysOn)` |

```python
from taifeng import OtelSinkConfig, OtelTelemetrySink

# 显式构造
config = OtelSinkConfig(
    service_name="my-agent",
    otlp_endpoint="http://otel-collector:4317",
    resource_attributes={"deployment.environment": "prod"},
)

# 或从环境变量引导（OTEL_SERVICE_NAME 未设置时抛 ValueError）
config_from_env = OtelSinkConfig.from_env()

sink = OtelTelemetrySink(config)
# ... attach 到 engine.subscribe_all() 后异步消费 ...
await sink.close(timeout_millis=5000)  # 应用 shutdown 时显式 flush，不会无限阻塞（R4）
```

预建 metric counter：
- `taifeng.compaction.attempts{strategy=...}` —— `CompactionStarted` 触发
- `taifeng.cache.breaks{reason=...}` —— `CacheBreakDetected` 或 `CompactionCompleted(cache_invalidated=True)` 触发

> 注：原计划 `taifeng.provider.retries` 暂搁 —— TF 当前未在主路径打 `ProviderRetry` 事件，待该事件类落地后再补。

## 7.6 LiteLLMClient 构造选项

```python
LiteLLMClient(
    api_key=...,
    model="gpt-4o-mini",
    base_url=...,
    extra_params={...},
    force_httpx_transport=True,   # 默认 True —— 见下方说明
)
```

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `force_httpx_transport` | `bool` | `True` | 每次 `stream` 调用前置 `litellm.disable_aiohttp_transport = True`，强制 LiteLLM 走 httpx 而非 1.7x 以来默认的 aiohttp transport。规避 aiohttp 在 `HTTPS_PROXY` + TLS-in-TLS 场景下 SNI 错误导致的 `TLSV1_UNRECOGNIZED_NAME`（国内本地代理 + 第三方 OpenAI-compat endpoint 必现）。如业务侧同进程混用 litellm 且确需 aiohttp，传 `False`。 |

---

## 与 claw-code / codex 的对应关系

| 配置维度 | codex | claw-code | taifeng |
| --- | --- | --- | --- |
| context window | `ModelConfig.context_window` | `auto_compaction_input_tokens_threshold` | `ContextBudget.context_window` ✓ |
| 自动压缩阈值 | hardcoded in compact.rs | env `CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS` | `ContextBudget.soft_limit_ratio` ✓ |
| 单 turn 最大迭代 | implicit | `max_iterations` | `max_iterations` ✓ |
| 手动压缩 | `Op::Compact` (空) | `compact_session(config)` | `CompactNow(target_tokens, preserve_tail, force)` ✓ 更细 |
| 回滚 N 轮 | `Op::ThreadRollback { num_turns }` | — | `ThreadRollback(num_turns)` ✓ |
| 中止 turn | `Op::Interrupt` | runtime cancel | `CancelTurn(submission_id)` ✓ (alias `Interrupt`) |
| 重载配置 | `Op::ReloadUserConfig` | runtime reload | `RefreshSnapshot()` + `UpdateBudget` ✓ |
| 权限策略 | OAuth + sandbox + approvals | `PermissionPolicy` + `Sandbox` | `PermissionPolicy` ✓ |
| 钩子 | 全 lifecycle | `PreToolUse/PostToolUse/PostToolUseFailure` | `PreToolUse/PostToolUse/PreCompact/PreTurn` ✓ |
| MCP server | mcp-client | mcp_lifecycle | `mcp.McpStdioClient` ✓ |

加粗 ✓ 表示**等价或更细**。

## 4. MCP stdio 客户端配置（config-consistency-fixes A1）

`McpStdioClient` 控制连外部 MCP server 的行为：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `command` | (必填) | 启动 server 的 argv（如 `["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]`） |
| `env` | `None` | 子进程环境变量；`None` = 继承当前进程 |
| `cwd` | `None` | 子进程工作目录 |
| **`request_timeout_seconds`** | `60.0` | 单条 JSON-RPC 请求超时；`None` = 关闭 client 层超时，完全由调用方控制。`tools/call` 途中 server 发起的 elicitation 等用户的时间也计入 |
| **`elicitation_handler`** | `None` | 可选 `ElicitationHandler`；注入则 initialize 声明 `elicitation` 能力并处理 server 的 `elicitation/create`；`None` 不声明，server 仍发时回 `-32601`（见 §4.2） |
| **`max_list_pages`** | `100` | `tools/list` 跟 `nextCursor` 翻页的页数上限；超限 / 游标重复 / 游标非字符串抛 `McpPaginationError`（不静默截断）；< 1 在拉起子进程前 `ValueError` |

协议版本不是旋钮：客户端恒声明 `2025-06-18`，接受 `SUPPORTED_PROTOCOL_VERSIONS`（`2025-06-18` / `2025-03-26` /
`2024-11-05`）内的协商结果，其余断开并抛 `McpProtocolVersionError`；协商结果读 `client.protocol_version`
（[mcp-client](architecture/capabilities/mcp-client.md)）。

**双层 timeout 协同**：`register_mcp_tools_async(..., timeout_seconds=N)` / `bind_mcp_tools(..., timeout_seconds=N)` 在 tool 调用外层再限时 N 秒（并接 `ToolContext.cancel`）。修复前内层硬编码 60s，外层调大会被静默截断；现在 `McpStdioClient(request_timeout_seconds=N)` 与外层匹配，**或** 显式设 `None` 把唯一 timeout 责任交给外层。

**放弃即通知**（不是旋钮，ADR 0069）：任一层超时、turn 取消或宿主 `task.cancel()` 放弃一个已发出的请求时，客户端向
server 发 `notifications/cancelled{requestId, reason}`（reason 如 `client timeout after 120s` /
`client cancelled the request (requested)`；宿主 `task.cancel("…")` 的消息即 reason），此后迟到的响应被忽略。
`initialize` 不发；stdio 尚未写出的请求不发。

```python
# 推荐：内外层 timeout 一致
client = await McpStdioClient.spawn(cmd, request_timeout_seconds=120)
specs = await register_mcp_tools_async(client, registry, timeout_seconds=120)

# 或：内层无限，外层独控
client = await McpStdioClient.spawn(cmd, request_timeout_seconds=None)
specs = await register_mcp_tools_async(client, registry, timeout_seconds=120)
```

### 4.1 streamable HTTP 与工具集同步（dynamic-tool-set，ADR 0048）

```python
from taifeng.mcp import McpHttpClient, bind_mcp_tools

client = await McpHttpClient.connect(
    "https://mcp.example.com/mcp",
    headers={"Authorization": "Bearer ..."},  # 鉴权由宿主注入
    request_timeout_seconds=60.0,
    listen_notifications=True,                  # GET 推送流；server 回 405 则只能手动 sync
)
binding = await bind_mcp_tools(client, pool_registry, tool_prefix="mcp_x_", watch=True)
# server 推 tools/list_changed → 自动增删 / 替换，各 engine 收到 tool_set_changed
await binding.sync()   # 也可手动同步
binding.detach()       # 卸载本绑定的全部工具
```

`trust_annotations`（默认 `False`）：`True` 时按 server 声明的 `readOnlyHint` / `idempotentHint` 把工具分为
`pure`（且可并行）/ `idempotent`，其余与默认一样按 `external_non_idempotent`（崩溃后挂起交人）。只对自己信任的
server 打开。`register_mcp_tools_async` 同名参数语义相同。

`attach_images`（默认 `False`）：默认把 MCP `image` 块降级为 `[image: <mime>, <n> bytes, not attached]` 占位文本
（模型知道有图、看不到内容，调用照常成功）。要让模型看图，传 `True` 并在
`EnginePool.create(image_input_policy=ImageInputPolicy(enabled=True, ...))` 启用策略：图片转为 `ToolResult.attachments`，
走工具图片附件的落盘前 admission；只开 `True` 不开策略，带图的调用以 `tool_attachment_rejected` 判错。`structuredContent` 不需要旋钮：恒进
`ToolResult.data["structured_content"]`，文本侧缺等价 JSON 时自动补上。`register_mcp_tools_async` 同名参数语义相同。

`McpHttpClient.connect` 同样接受 `elicitation_handler` / `max_list_pages`（语义同 §4 表），另有两个 HTTP 专属旋钮：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| **`max_stream_resumptions`** | `3` | POST 的 SSE 流在响应前断开时，按 `Last-Event-ID` 续传的次数上限；GET 推送流连续无事件重连的上限。`0` = 不续传 / 不重连；< 0 构造期 `ValueError` |
| **`stream_resume_delay_seconds`** | `1.0` | 续传 / 重连的基础间隔，按次数翻倍、单次 ≤30s；server 以 SSE `retry:` 给出的间隔优先；< 0 构造期 `ValueError` |

续传只在 server 给 SSE 事件带过 id 时发生；server 从未给 id、续传 GET 回 405 / 错误 / 非 SSE、次数用尽 → 该请求以
`McpToolError` 显式失败（全部续传计入 `request_timeout_seconds`）。

**outputSchema**（不是旋钮）：工具在 `tools/list` 声明了 `outputSchema` 时，桥按它校验非 `isError` 结果的
`structuredContent`；缺失或违例 → 该次调用判错（`reason="mcp_output_schema_violation"`，`data["violations"]`），
server 原文不交给模型；outputSchema 形状非法的工具不注册（告警）。

### 4.2 elicitation 注入口（taifeng 作为 MCP 客户端，ADR 0063）

server 在处理请求途中可发 `elicitation/create` 向用户要结构化输入。内核不做 UI，宿主注入处理器：

```python
from taifeng.mcp import ElicitationRequest, ElicitationResult, McpStdioClient

async def ask_user(request: ElicitationRequest) -> ElicitationResult:
    # request.message / request.requested_schema（扁平 object schema）/ request.server_info
    answer = await my_ui.show_form(request.server_info.get("name"), request.message,
                                   request.requested_schema)
    if answer is None:
        return ElicitationResult("cancel")          # 用户关掉了对话框
    if answer == "declined":
        return ElicitationResult("decline")         # 用户明确拒绝
    return ElicitationResult("accept", answer)      # content：键为字符串、值为原始类型

client = await McpStdioClient.spawn(cmd, elicitation_handler=ask_user)
```

| 情形 | 内核行为 |
| --- | --- |
| handler 返回 `ElicitationResult` | 回 `{"action": ..., "content"?: ...}`；accept 的 content 先按 `requestedSchema` 子集校验，违例回 `-32603` |
| handler 抛异常 / 返回其他类型 | 回 `-32603`（只含异常类型名），连接照常可用 |
| server 发 `notifications/cancelled` | handler 被取消（`CancelledError`），不回响应 |
| 客户端 `close()` | 在等的 handler 被取消 |
| 未注入 handler | 不声明能力；server 仍发 → `-32601` |

内核不给 handler 设超时（规范把请求超时归于发送方 server）；要自有时限在 handler 内限时后返回 `cancel`。

`bind_mcp_tools` 同样接受 `McpStdioClient`。运行时也可直接 `ToolRegistry.register / unregister / replace`，
变更在下一次采样生效。

## 5. MCP server mode（M3 mcp-server-mode）

把 taifeng 自身暴露为 MCP server，让 Claude Code / Cursor 等通过 stdio JSON-RPC 调用 taifeng skills 与读取 SKILL.md 资源。

### `McpStdioServer` 构造

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `pool` | (必填) | 业务侧已构造好的 `EnginePool`（含 model_client / compressors / hooks / instructions 全套配置）；server **不**自构造 |
| `server_name` | `"taifeng"` | initialize handshake 中暴露的 server 名 |
| `server_version` | `taifeng.__version__` | 同上版本号 |

### 暴露内容

| MCP 方法 | 内容 |
| --- | --- |
| `initialize` | 版本协商（客户端请求的版本在 `2025-06-18` / `2025-03-26` / `2024-11-05` 内原样回，否则回 `2025-06-18`）+ serverInfo + capabilities (`tools` + `resources`) |
| `tools/list` | 单个 meta-tool `run_skill_turn(skill_id, message, session_id?)` |
| `tools/call` | 派发到 `pool.get_or_create + engine.submit + 等 turn_completed`，返回 final assistant text |
| `resources/list` | 每个 skill 一个资源，URI = `taifeng://skill/<id>`，mimeType=`text/markdown` |
| `resources/read` | 返回 SKILL.md body 完整文本 |

### CLI 便利入口

```bash
python -m taifeng mcp serve <skills_dir> --storage <dir> [--model gpt-4o-mini]
```

CLI 默认用 `LiteLLMClient(model=args.model)`；业务侧自定义 ModelClient / hooks 须改写 entrypoint：

```python
from taifeng import EnginePool, McpStdioServer

async def main():
    pool = await EnginePool.create(
        skills_dir="./skills",
        storage_dir="./threads",
        model_client=MyCustomClient(...),
        hooks=my_hook_runner,
        instruction_layers=[...],
    )
    await McpStdioServer(pool, server_name="my-agent").run()
```

**重要约束**：CLI / server 的 **stdout 是 JSON-RPC 协议流**；任何 print / log 必须走 stderr。

**HITL 能力门控**（`--enable-hitl` / `McpPrompter`，不是旋钮，ADR 0069）：server 只向在 initialize 声明了
`capabilities.elicitation` 的客户端发 `elicitation/create`。未声明（或尚未 initialize）→ 请求不发出，审批立即
fail-closed 判 deny（`reason="elicitation_unsupported: …"`），并经 `McpStdioServer(emit=)` 发 `elicitation_unsupported`
（`{"method", "capability", "initialized"}`；未注入 emit 时记 info 日志）。要用 MCP 审批，客户端必须声明该能力；
`McpStdioServer.client_capabilities` 可读到客户端的声明。

### 错误码

| 场景 | 类别 | 行为 |
| --- | --- | --- |
| 未知 method | JSON-RPC error code -32601 | 客户端 bug |
| stdin 非 JSON | -32700 | parse error, `id=null` |
| 参数缺失 / uri 非法 | -32602 | 客户端调错 |
| unknown skill / non-entry | MCP `isError: true` content | LLM 可见，提示客户端 LLM 自适应 |
| turn_failed | MCP `isError: true` content | content.text 含错误原因 |
| tool 内 LLM hang | 600s 超时 | content 末尾追加 timeout note + isError=true |

## 6. 扩展内置工具（M4 tool-builtins-extended）

补两个 LLM agent 通用场景需要的工具。**业务侧按需 register**（不在 `EnginePool.create` 默认注入）。

### 6.1 `apply_patch` —— 结构化原子化补丁

```python
from taifeng import make_apply_patch_tool

tool = make_apply_patch_tool(
    root_dir="./workspace",          # 沙盒根
    policy=my_permission_policy,     # 可选；整组 patch 一次审批
    max_bytes=1024 * 1024,           # 单 patch new_text 上限
)
```

输入 schema：

```json
{
  "patches": [
    {"path": "src/foo.py", "old_text": "...", "new_text": "..."},
    {"path": "src/new.py", "new_text": "...", "create": true},
    {"path": "src/old.py", "delete": true}
  ]
}
```

**两阶段原子语义**：所有 patch 先 dry-run 全量校验（路径在沙盒 / `old_text` 唯一 / `create` 时 path 不存在 / `delete` 时 path 存在），任一失败 → 0 文件被改；全过才执行。

与 unified diff 的对比：结构化输入避开 diff parser 复杂度与 LLM 格式错误（缩进 / 行号偏移）；想用 unified diff 业务侧自己包装一层。

### 6.2 `run_in_background` + `wait_for_task` —— shell 长任务

`BackgroundTaskRegistry` 进程内管理 task 生命周期，与 `EnginePool` 解耦。**业务侧自管 shutdown**：

```python
from taifeng import (
    BackgroundTaskRegistry,
    make_run_in_background_tool,
    make_wait_for_task_tool,
)

registry = BackgroundTaskRegistry(max_concurrent=8)   # executor=MySandboxExecutor() 可把命令放进沙箱

extra_tools = [
    # notify_on_exit=True（默认）：任务结束把摘要投回发起 thread + emit background_task_completed
    make_run_in_background_tool(registry=registry, policy=my_policy, notify_on_exit=True),
    make_wait_for_task_tool(registry=registry, default_timeout=60.0),
]

pool = await EnginePool.create(..., extra_tools=extra_tools)
# ... 业务结束 ...
await registry.shutdown()  # kill 所有未完成 task
await pool.close()
```

**LLM 视角**：

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `run_in_background` | `{"command": "..."}` | `{"task_id": "bg_xxxx", "command": ..., "started_at": ...}` |
| `wait_for_task` | `{"task_id": "...", "timeout_seconds": 30}` | `{"status": "completed" / "timeout" / "unknown", "exit_code": int, "stdout": "...", "stderr": "..."}` |

**关键设计**：`wait_for_task` 的 `status="timeout"` 与 `"unknown"` 都返回 `ToolResult.ok`（is_error=False）—— 让 LLM 用 `data.status` 路由（再 wait / 改策略），而非被 error 中断思路。

`max_concurrent` 满时 `run_in_background` 返回 `ToolResult.error(reason="too_many_background_tasks")`，不静默丢弃。

`run_in_background` 复用 `shell_exec` 的启发式 deny list（`rm -rf` / `sudo` / fork bomb 等）。permission scope 也是 `"shell_exec"`，业务侧 PermissionRule 自动复用。

### 6.3 `http_request` —— 受审批的 HTTP 调用（P0）

填补 hermes / codex / claw-code 都有但 TF 缺失的网络访问能力。**保守要求 PermissionPolicy**（与 shell_exec 同），`policy=None` 时立即拒绝。

```python
from taifeng.tool.builtins import make_http_request_tool

tool = make_http_request_tool(
    policy=my_permission_policy,   # 必填；None 时 handler 返回 reason='no_policy'
    timeout_seconds=30.0,          # 工厂上限；LLM 可在 args.timeout_seconds 内更短覆盖
    max_response_bytes=1024 * 1024,  # 响应 body 截断上限（默认 1MB）
    max_redirects=5,
    allowed_methods=("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"),
)
```

**输入 schema**：`{"url": str (required), "method": "GET"|..., "headers": {str: str}, "body": str|dict|list, "timeout_seconds": float}`

**输出**（JSON 字符串）：`{"status": int, "headers": {...}, "body": "...截断...", "truncated": bool, "url_final": "..."}`

**关键设计**：
- 4xx / 5xx **不算 ToolResult.error** —— `is_error=False`，让 LLM 读 `status` 自决（重试 / 放弃 / 改策略）
- dict/list body 自动 JSON 序列化（带 `content-type: application/json`）；string body 走 `content=` 由 LLM 自填 header
- PermissionRequest 的 `target = f"{method} {url}"`，业务侧可用 `PermissionRule.parse("Network(GET https://api.github.com/*)")` 风格规则精确匹配
- 异常归类：`reason ∈ {bad_args, no_policy, permission_denied, timeout, redirect_limit, connect_error, unknown}`
- R4 取消：handler 入口 `ctx.cancel.raise_if_cancelled()` + `httpx.Timeout` 双保险

**parallel_safe=False**（保守：单一 ToolSpec 同时承载读写方法）。后续如需，可拆 `http_get`(True) / `http_request`(False) 两个工具。

### 6.4 分离式 spawn 工具（detached-spawn）

4 个 LLM 工具，**业务侧按需 opt-in**（不默认注入，通过 `extra_tools=` 参数传入）。均 `parallel_safe=True`，通过 `ctx.extras["spawn_coordinator"]`（engine 在构建 TurnRunner 时注入自身）接入 engine。详见 [detached-spawn 契约](architecture/capabilities/detached-spawn.md) 与 ADR 0015。

```python
from taifeng.tool.builtins import (
    make_spawn_skill_tool,
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
)

pool = await EnginePool.create(
    ...,
    extra_tools=[
        make_spawn_skill_tool(),
        make_await_skills_tool(),
        make_join_skill_tool(),
        make_kill_skill_tool(),
    ],
)
```

### 6.5 peer 通信工具（peer-mailbox-messaging）

2 个 LLM 工具，同为 **opt-in**（`extra_tools=` 注入 + entry skill `tool_names` 显式启用），均 `parallel_safe=True`、经 `ctx.extras["spawn_coordinator"]` 接入。详见 [peer-mailbox-messaging 契约](architecture/capabilities/peer-mailbox-messaging.md)。

| 工具 | 说明 |
| --- | --- |
| `make_send_message_tool()` | 谱系内点对点发消息：target = child_thread_id / handle_id / `"parent"`；mode = `queue_only`（入队/落史）/ `trigger_turn`（空闲唤醒，运行中自动降级、root 拒绝、suspended 只落史） |
| `make_wait_peer_tool()` | turn 内阻塞等单个 spawn 句柄终态；`timeout_seconds` **必填**（互等死锁的唯一保底）；超时返回 error 结果（turn 不失败） |
| `make_wait_any_tool()` | turn 内 **any-of-N** 等待：一组句柄中任一终态即唤醒，返回当时**全部**已终态 `{settled, pending}`；`timeout_seconds` 同样必填；零终态到期返回 error 结果（turn 不失败） |

业务侧程序化投递走 `engine.submit(SendToPeer(target_thread_id=…, text=…, mode=…))` 或直调 `engine.deliver_peer_message(...)`，与工具完全同一路径。

### 6.6 todo builtin（pinned-state 官方范例）

`TodoStore`（实现 `PinnedStateSource`）+ `make_todo_write_tool(store)`（`todo_write`，整表替换语义）。装配 = 同一实例双注入：

```python
store = TodoStore()                       # max_chars 可调（默认 2000）
# 可选：reinject_every_turns=N → 距上次注入满 N 轮用户消息即 pre-turn 重注清单（默认 None = 只在压缩后钉回，ADR 0065）
pool = await EnginePool.create(
    ...,
    extra_tools=[make_todo_write_tool(store)],
    pinned_state_sources=[store],         # 清单自动穿越压缩
)
```

| 工具名 | 对应 engine API | 输入 schema | 输出 |
| --- | --- | --- | --- |
| `spawn_skill` | `engine.spawn_skill(skill_id, args, reason)` | `{skill_id, args?, reason}` | `{handle_id, child_thread_id}` |
| `await_skills` | `engine.set_join_barrier(handle_ids, then_skill_id, then_args_template?)` | `{handle_ids:[...], then_skill_id, then_args_template?}` | `{barrier_id}` |
| `join_skill` | `engine.spawn_status(handle_ids)` | `{handle_ids:[...]}` | `{hid: {status, result}, ...}` |
| `kill_skill` | `engine.kill_spawn(handle_id)` | `{handle_id}` | `{killed: bool}` |

**关键设计**：
- `spawn_skill` 拒绝路径（unknown_skill / dispatch_rejected / SpawnLimitError）返回 `ToolResult.error`，不静默丢弃
- `join_skill` 非阻塞：未完成句柄返回 `{status:"running"/"suspended", result:null}`，LLM 自决何时重查
- `kill_skill` 的 unknown handle → `ToolResult.error`；terminal handle → `{killed:false}`（no-op）；running/suspended → 取消该 token，兄弟 spawn 不受影响
- 父 turn 结束后（engine keepalive 中），LLM 在下一条 `UserMessage` 的 turn 内可继续调用上述工具操作已有句柄

**K1 配额 nuance**：`max_concurrent_spawns` 只统计 running（in-flight runner）的 spawn；suspended spawn 释放 slot，不计入并发额度；resume / rewind 重推重新占用，满额排队（见 §1.0）。

### 6.7 `glob` / `grep` —— 沙盒内只读文件搜索（ADR 0064 / 0071）

**opt-in**：不在 `EnginePool.create` 默认注入，经 `extra_tools=` 显式传入，入口 skill 在 `tool_names` 声明。纯 Python 实现（不依赖 rg），只读、可并行（`parallel_safe=True`、`effect_kind="pure"`）。契约见 [tool-builtins-extended § glob / grep](architecture/capabilities/tool-builtins-extended.md)。

```python
from taifeng.tool.builtins import (
    DEFAULT_SEARCH_EXCLUDE_DIRS,
    make_file_read_tool,
    make_glob_tool,
    make_grep_tool,
)

pool = await EnginePool.create(
    ...,
    extra_tools=[
        make_file_read_tool(root_dir="./workspace", policy=my_policy),
        make_glob_tool(root_dir="./workspace", policy=my_policy),   # 与 file_read 同 root / 同 policy
        make_grep_tool(
            root_dir="./workspace",
            policy=my_policy,
            max_results=200,                  # 结果名额（content 按输出行：匹配行 + 上下文行；其余按文件）
            max_line_chars=500,               # content 单行 / multiline 单个片段的字符上限
            max_file_bytes=2 * 1024 * 1024,   # 更大的文件跳过并在尾注列出
            exclude_dirs=DEFAULT_SEARCH_EXCLUDE_DIRS | {"dist"},  # 不下探的目录名
            respect_gitignore=True,           # 按沙盒内 .gitignore 跳过路径（默认开）
        ),
    ],
)
```

| 工厂参数 | glob | grep | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `root_dir` | ✓ | ✓ | 必填 | 沙盒根；基点与被读文件都必须在内（同 `file_read`，拒绝 `..` 与符号链接逃逸） |
| `policy` | ✓ | ✓ | `None` | 每次调用审批一次：`scope="file_read"`、target=基点绝对路径（整棵子树粒度） |
| `max_results` | ✓ | ✓ | `200` | 超出截断并在输出尾告知，遍历提前停止 |
| `max_line_chars` | — | ✓ | `500` | 单行截断并注明原长度 |
| `max_file_bytes` | — | ✓ | `2MB` | 超大文件跳过并列出名字 |
| `exclude_dirs` | ✓ | ✓ | `DEFAULT_SEARCH_EXCLUDE_DIRS` | `.git` `.hg` `.svn` `node_modules` `.venv` `__pycache__` `.mypy_cache` `.pytest_cache` `.ruff_cache` `.tox` |
| `respect_gitignore` | ✓ | ✓ | `True` | 读沙盒根到基点沿途及遍历中子目录的 `.gitignore`，命中路径跳过（目录不下探）并在尾注计数；显式基点自身不受影响。支持注释 / `!` / 尾斜杠 / `**` / 锚定 `/`，POSIX 字符类等不支持的行计数告知且不生效。`False` = 不读 |
| `timeout_seconds` | ✓ | ✓ | `30.0` | ToolSpec 级超时 |

**LLM 视角**：`glob({"pattern": "**/*.py", "path"?})` → 每行一个路径；`grep({"pattern": "<re>", "path"?, "include"?, "ignore_case"?, "output_mode"?: "content"|"files_with_matches"|"count", "context_before"?, "context_after"?, "context"?, "multiline"?})` → `路径:行号:行`（行号 1 基，`file_read` 的 `offset = 行号 - 1`）；带上下文时上下文行为 `路径-行号-行`、不相邻组以 `--` 分隔（仅 `content`，占结果名额）；`multiline=true` 时整文件按 `re.MULTILINE | re.DOTALL` 匹配，每个匹配 `路径:起始行-结束行:"片段"`。结果按路径排序、路径相对沙盒根；二进制 / 非 UTF-8 / 超大文件 / 被跳过的符号链接 / 被 .gitignore 忽略的路径都以 `[skipped ...]` 尾注告知。

### 6.8 `memory` —— 模型主动检索 / 写入 / 删除长期记忆（ADR 0064 / 0071）

K3 `memory_store` 的模型侧入口：`search` 委托 `store.prefetch`、`save` 委托 `store.writeback`、`delete` 委托 `store.forget`（仅当 store 实现可选协议 `ForgettableMemoryStore`），内核不带后端。**opt-in**，同一 store 双注入：

```python
from taifeng.tool.builtins import make_memory_tool

store = MyMemory(...)                 # 业务实现的 MemoryStore
pool = await EnginePool.create(
    ...,
    memory_store=store,               # 内核被动 page-in / 写回（可选）
    extra_tools=[make_memory_tool(store)],
)

# 只读知识库（继承 NullMemoryStore 只覆写 prefetch）：只开 search
make_memory_tool(knowledge_base, actions=("search",))

# store 实现了 forget(target, *, thread_id) -> int 但不想开放删除
make_memory_tool(store, actions=("search", "save"))
```

| 工厂参数 | 默认 | 说明 |
| --- | --- | --- |
| `store` | 必填 | `MemoryStore` 实现 |
| `actions` | `None` | 启用的动作子集；`None` = `search` + `save`，store 可遗忘时再加 `delete`。显式含 `delete` 而 store 不可遗忘 → `ValueError`。决定 schema 与副作用分类（含 save / delete → 串行 + `external_non_idempotent`/`manual`；仅 search → 可并行 + `pure`） |
| `max_result_chars` | `4000` | search 返回文本的字符上限（协议返回单段文本，按字符计），超出截断并注明 |
| `max_save_chars` | `2000` | save 单条上限，超出以 `too_large` 拒绝（不截断写入） |
| `timeout_seconds` | `30.0` | ToolSpec 级超时 |

`save` 写入一条 `assistant_message`，`metadata={"source": "memory_tool", "call_id": ...}`——后端据此区分模型主动记忆与 turn 结束的脏页写回、或按 call_id 去重。`delete` 的 `target` 是后端在 search 结果里展示的记忆标识或该条记忆原文（`prefetch` 只返回文本，模型能表达的只有文本），`forget` 返回实际删除条数，0 是正常结果。`NullMemoryStore` 不实现 `forget`；`CompositeMemoryStore` 有可遗忘子时才可遗忘。后端异常以 `reason="memory_error"` 显式返回给模型（不同于被动钩子的 best-effort）。

## 7. LLM 强类型输出（structured_output / P1）

填补 hermes / codex / claw-code 都有的强类型输出 capability。业务侧传 JSON Schema dict，provider 翻译到各家 native 格式，response 解析后通过 `structured_output` 事件回流。

```python
from pydantic import BaseModel
from taifeng import ApiRequest, ApiMessage, ResponseFormatSpec

class UserProfile(BaseModel):
    id: int
    name: str
    email: str | None = None

req = ApiRequest(
    model="gpt-4o-mini",
    messages=[ApiMessage(role="user", content="Extract: alice@acme, id 42, alice")],
    response_format=ResponseFormatSpec(
        name="UserProfile",
        json_schema=UserProfile.model_json_schema(),  # 业务自己生成
        strict=True,
    ),
)

async for ev in session.stream(req):
    if ev.kind == "structured_output":
        profile = UserProfile.model_validate(ev.data["parsed"])
        ...
    elif ev.kind == "error" and ev.data["kind"] == "parse_error":
        # LLM 没按 schema 返回；业务自决重试 / 回退
        ...
```

**Provider 翻译规则**：

| Provider | native 字段 |
| --- | --- |
| openai-compat | `response_format = {"type": "json_schema", "json_schema": {"name", "schema", "strict"}}` |
| litellm | 同上 —— litellm 内部对 Anthropic / Gemini 自动桥接到各家 native 模式 |
| sim | `SimTurn(structured=...)` 字段配对回放 |

**事件流**：

- 成功：`text_delta*` → `structured_output(parsed, raw_text)` → `completed`
- 失败：`text_delta*` → `error(kind="parse_error", retryable=False)` → `completed`
- 未带 `response_format`：行为完全不变（向后兼容）

**关键设计**：
- **Taifeng 不绑定 Pydantic** —— `ResponseFormatSpec.json_schema` 是 dict，业务侧自己用 `MyModel.model_json_schema()` 生成（R1 业务零侵入）
- **parse 失败不阻断流** —— 仍 emit `completed`，业务可在事件流中决定重试/回退
- **R3 可观测**：新事件类型独立，telemetry 链路自动覆盖

## 8. 挂起 / Resume 原语（suspend-resume）

把"turn 中途阻塞等待"改造成**通用挂起原语**：turn 可在任意点以 `end_reason="suspended"` 早返回并释放实例，业务侧之后凭 `thread_id` + `resolutions` 提交 `Resume` Op 续跑。完整契约见 [suspend-resume 契约](architecture/capabilities/suspend-resume.md) + ADR 0012。

### 8.1 `SuspendingPrompter`（opt-in 挂起式审批 prompter）

`PermissionPolicy.prompter` 的一个实现：`ask` 模式**不阻塞**，而是抛 `SuspendSignal`（`reason=permission`）让 turn 退栈挂起。与 `CliPrompter` / `CallbackPrompter`（阻塞等待决定）互斥选用——选它即开启"权限审批可释放实例"。

```python
from taifeng import PermissionPolicy
from taifeng.permission.types import SuspendingPrompter

policy = PermissionPolicy(
    rules=[...],
    default_mode="ask",
    prompter=SuspendingPrompter(),   # ask → 抛 SuspendSignal，turn end_reason="suspended"
)
```

挂起后业务侧 emit / 持久化 pending（前端据 `payload_schema` 渲染审批 UI），收到决定后 `engine.submit(Resume(thread_id, {request_id: {"granted": True/False, "reason": "..."}}))` 续跑。

### 8.2 `request_user_input` 内置工具（opt-in 采集型 HITL）

LLM 向人类发起结构化问询并等待回答的内置工具。**业务侧按需 register**（不默认注入）。调用即抛 `SuspendSignal(reason=data)`，turn 挂起；resume 时该 `request_id`（= call_id）的 payload 回填成该 call 的 `function_call_output`。`parallel_safe=False`，应作为该 step 唯一的工具调用。

```python
from taifeng.tool.builtins import make_request_user_input_tool

pool = await EnginePool.create(..., extra_tools=[make_request_user_input_tool()])
# LLM 调用 request_user_input(prompt="...", response_schema={...}) → turn 挂起
# 业务侧收齐回答后：
await engine.submit(Resume(thread_id, {call_id: {"answer": "..."}}))
```

`prompt` 必填非空（系统边界校验）；`response_schema` 不透明透传（R1，内核不解析）。

### 8.3 retry-then-suspend（系统态挂起，复用 RetryConfig）

LLM 限流 / 配额 / 余额 / key 鉴权失败 / 可恢复网络错时，`_sample_once` **先**走 §7 的 `RetryConfig`（默认 `max_attempts=3`）自动退避重试；**3 次耗尽**后（或确定性"等外部介入"类 `provider_auth` / `provider_quota` / `provider_balance`）才转 `SuspendSignal(reason=system_retry)` 挂起。无独立旋钮——重试阈值即 `RetryConfig.max_attempts`。

resume payload：`{"action": "retry"}`（默认，重跑同次 sample 获全新 retry 预算）或 `{"action": "abort"}`（turn 终止不续跑）。`ContentFilter` / `ContextOverflow` / `InvalidRequest` 等确定性失败**不挂起**，照旧硬失败（`end_reason="error"`）。

### 8.4 `PermissionPolicy.preapprove(call_id)`（内部 / resume 用）

resume 一个 permission-allow 的挂起 tool 时，engine 在重新执行该 tool 前调 `policy.preapprove(call_id)` 一次性放行——否则 `SuspendingPrompter` 会再次抛 `SuspendSignal` 导致**无限挂起**。预批准是 one-shot：命中即从内部集合移除（`reason="resume_preapproved"`），不污染后续同 scope/target 的 check。业务侧通常无需直接调用（engine 内部在 `_handle_resume` → `_execute_resumed_tool` 自动处理）。

### 8.5 三个 EventMsg

| kind | 触发 | data |
| --- | --- | --- |
| `turn_suspended` | 挂起结局的契约事件类型（业务可构造转发，如 Web SSE 桥）；当前实现挂起经 `TurnCompleted{end_reason="suspended"}` 上事件流 | `{thread_id, record_id, pending[...], cache_invalidated}` |
| `suspension_resolved` | `Resume` 成功配对、turn 续跑 | `{record_id, request_ids}` |
| `suspension_resolve_rejected` | resolution 不全 / 多余、无活跃挂起、重复 resume、ResolveError | `{reason, record_id, detail}` |
