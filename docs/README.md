# Taifeng Documentation Index

> Entry point for Taifeng research notes, architecture documents, capability contracts, and ADRs.

## Brand resources

[Logo, avatar, favicon, and social preview assets](assets/brand/README.md).

## For Integrators

If you are integrating Taifeng into a host system rather than changing the kernel, start here:

1. [Capability matrix](capability-matrix.md): what exists, current status, entry APIs, examples, and contracts.
2. [Usage guide](usage.md): installation, usage levels, and code skeletons for each major capability.
3. [Configurable knobs](configurable-knobs.md): construction-time arguments, runtime ops, and kernel control fields.
4. [Real LLM ledger](real-llm-ledger.md): generated regression ledger for real-provider scenarios. Do not edit it manually.

## Reading Order for Kernel Contributors

### First Pass: What Taifeng Is and Why

1. [Architecture overview](architecture/overview.md): the five-dimensional abstraction, six core packages, and infrastructure packages.
2. [ADR 0001: Naming Taifeng](decisions/0001-naming-taifeng.md).
3. [ADR 0002: Choosing Python](decisions/0002-python-language.md).

### Second Pass: How Taifeng Differs from Mainstream Frameworks

4. [Hermes capability gap roadmap](architecture/hermes-gap-roadmap.md): comparison across codex, claw-code, openclaw, and hermes.
5. [ADR 0003: Skill is context, not a tool](decisions/0003-skill-as-context.md).
6. [ADR 0004: Cache-aware compaction](decisions/0004-cache-aware-compression.md).
7. [ADR 0005: Submission / Event dual bus](decisions/0005-submission-event-bus.md).
8. [ADR 0006: Unified skill model, no agent package](decisions/0006-unified-skill-model.md).

### Third Pass: Implementation Details

9. [Skill system](architecture/skill-system.md): §1.1, aligned with ADR 0003 / 0006 / 0009.
10. [Agent loop](architecture/agent-loop.md): §1.2 plus instruction injection, aligned with ADR 0005 / 0007 / 0010.
11. [Conversation persistence](architecture/conversation.md): §1.3, aligned with ADR 0008.
12. [Context compression](architecture/context-compression.md): §1.4, aligned with ADR 0004.
13. [LLM client](architecture/llm-client.md): §1.5.

Later ADRs:

14. [ADR 0007: Instructions as host-side injection](decisions/0007-instructions-as-injection.md).
15. [ADR 0008: Store protocol decoupling and stdlib SQLite index](decisions/0008-store-protocol-decoupling.md).
16. [ADR 0009: SKILL.md scripts runtime](decisions/0009-scripts-runtime.md).
17. [ADR 0010: Permission gate completeness](decisions/0010-permission-gate-completeness.md).
18. [ADRs 0011-0015](decisions/): empty API keys, suspend/resume, composite-tool-only, turn rewind, and detached skill spawn.
19. [ADR 0016: Cold rewind rebuild](decisions/0016-cold-rewind-rebuild.md).
20. [ADR 0017: Kernel positioning criteria](decisions/0017-kernel-positioning-criteria.md).
21. [ADR 0018: Thread-addressable rewind](decisions/0018-thread-addressable-rewind.md).
22. [ADRs 0019-0022](decisions/): post-turn hook, budget-awareness hint, doom-loop detection, and reusable approval grants.
23. [ADR 0023: Skill discovery via search](decisions/0023-skill-discovery-via-search.md): deferred exposure, whitelist-scoped recall, and confidence-as-data.
24. [ADR 0024: Skill recall/verify pipeline](decisions/0024-skill-recall-verify-pipeline.md) (Amends #0023): opt-in auto-discovery toggle, plus a post-recall verification gate judging input-requirement fit.
25. [ADR 0025: SessionJournal as the session source of truth](decisions/0025-session-journal-source-of-truth.md): canonical session history, complete Timeline projection, HITL/approval records, and session-scoped fail-closed durability.
26. [ADR 0026: Codex proxy as an independent provider](decisions/0026-independent-codex-provider.md): explicit `codex-responses-v1` identity, no Chat fallback, and isolated provider state. Its living contract is [llm-codex-provider](architecture/capabilities/llm-codex-provider.md).
27. [ADR 0027: Sensitive LLM request audit](decisions/0027-sensitive-llm-request-audit.md) (Amends #0025): safe projection, redaction manifest, and full canonical request digest without duplicating image or reasoning ciphertext.
28. [ADR 0028: Effect-based permission model](decisions/0028-effect-based-permission-model.md): scope = effect type, target = normalized object; `tool_use` is only the fallback; Style A aliases map to effect scopes. Living contract is [permission-gate](architecture/capabilities/permission-gate.md).
29. [ADR 0029: Root-turn serialization and single writer](decisions/0029-root-turn-serialization-single-writer.md) (Amends #0025): one root turn at a time via a FIFO root gate, runner is the only root-history writer while in flight, every submission gets a terminal event, and audited application is deferred until the token holds the gate.
30. [ADR 0030: Codex SSE noise tolerance](decisions/0030-codex-sse-noise-tolerance.md) (Amends #0026): unregistered or malformed top-level SSE frames — relay-injected keepalives included — are counted and skipped instead of failing the attempt; protocol-internal violations stay fail-closed and the terminal guarantee remains with the completed gate. Living contract is [llm-codex-provider](architecture/capabilities/llm-codex-provider.md) §5.3.
31. [ADR 0031: Terminal replay for late subscribers](decisions/0031-late-subscriber-terminal-replay.md): the engine remembers each submission's last terminal event so a filtered subscription created after the turn already finished gets that real event instead of hanging forever; bounded FIFO, unknown submissions still wait. Living contract is [audit-observability](architecture/capabilities/audit-observability.md).
32. [ADR 0032: Usage metadata must not kill a successful turn](decisions/0032-usage-metadata-tolerance.md) (Amends #0026): only the usage detail keys the extractor actually reads are validated; unknown detail fields and non-object detail containers are ignored instead of failing the attempt. Living contract is [llm-codex-provider](architecture/capabilities/llm-codex-provider.md) §5.2.
33. [ADR 0033: Normalize in-stream Responses failures](decisions/0033-responses-stream-failure-normalization.md) (Amends #0026): `error` / `response.failed` / `response.incomplete` are mapped to typed `LLMError`s from their official fields instead of collapsing into `InvalidResponseError`, so failure_class/retryable/recovery match the real cause and the provider's own code and message survive. Shared by the codex and OpenAI Responses providers.
34. [ADR 0034: Usage accounting never fails a turn](decisions/0034-usage-accounting-never-fails-a-turn.md) (Supersedes the detail-key half of #0032): only the three spec-required counts (`input`/`output`/`total_tokens`) fail closed, and they now raise a classified `InvalidResponseError` instead of a bare `ValueError`; every other accounting value (`cache_read`/`cache_creation`/`prompt_cache_hit`/`cached_tokens`/`reasoning_tokens`) falls back to 0 when unusable, so a relay's serialization differences cannot kill a turn that already produced content. Living contract is [llm-codex-provider](architecture/capabilities/llm-codex-provider.md) §5.2.
35. [ADR 0035: spawn 二次驱动单入口 + 子 thread 终态持久锚 + 续跑链取消即停](decisions/0035-spawn-redrive-single-entry-settled-anchor.md) (Amends #0015, #0018): `_load_thread_items` 成为子 thread 逻辑 history 的单一入口；五条 detached 驱动路径收敛到 `SpawnDriver._drive`（K1 排队、状态 CAS、token/running/live 同一同步步、投递与重载互斥）；终态以 `spawn_settled` 锚落子 thread，suspended-kill 落 resolved marker 并撤销 TTL；续跑链取消即停并逐层解链，根以 `turn_failed{cancelled}` 终结。活文档 [detached-spawn](architecture/capabilities/detached-spawn.md)。
36. [ADR 0036: cache anchor 真值化](decisions/0036-cache-anchor-truth.md): anchor 统一为「最后一条已缓存 history 下标（含）、-1 无缓存」，采样成功后推进到发出时末项，三策略窗口与 prompt 映射自 `anchor+1` / `<= anchor` 起；overflow 自愈分两档（先只动 tail，不够救再动 head 并标 `compaction_overflow`）；坏 JSON / 非对象工具参数以 `invalid_arguments` 拒绝执行而非退化为 `{}`。活文档 [cache-anchor](architecture/capabilities/cache-anchor.md)。
37. [ADR 0037: 旧 provider 路径对齐主路径不变量](decisions/0037-legacy-provider-path-alignment.md) (Amends #0026, #0030, #0033, #0034): gemini / anthropic / litellm 补齐流终止真相（terminal_seen + 异常 finish_reason 保护）与 `TransportError` 归一；`completed` 透传原生 `stop_reason`（不跨家归一）；gemini `functionResponse.name` 用真实函数名；`retry_async` 经 `RetryingModelClient` 真正接线（零产出才重试，刻意与 strict audit 的 one-attempt 契约互斥）；子进程 env 白名单成为默认；journal payload 补 `extra_content` / `attachments`；压缩孤儿 output 可降级处理。活文档 [llm-provider-native](architecture/capabilities/llm-provider-native.md)。

11. [ADR 0011: Empty API key omits the auth header](decisions/0011-empty-api-key-omits-auth.md): 空 API key 视作「不发 Authorization 头」而非发一个空头——本地网关与代理常以「有无该头」分流，发空头会被判成鉴权失败。
12. [ADR 0012: Suspend / Resume as a kernel primitive](decisions/0012-suspend-resume-primitive.md): HITL 挂起是内核原语而非业务回调：turn 以 `SuspensionRecord` 落盘中断，`Resume` 携 resolutions 续跑，跨进程可恢复（R5）。
13. [ADR 0013: Composite skills are tool-only](decisions/0013-composite-tool-only.md): composite skill 只经 `call_skill` 工具派发，不额外引入 agent 概念——统一到 ADR 0006 的 Skill 抽象。
14. [ADR 0014: Turn rewind](decisions/0014-turn-rewind.md): turn 内以回访节点（iteration / dispatch）为切点回退重推，支持 `re_reason` 重采样与 `retry_tool` 换参补跑。
15. [ADR 0015: Detached skill spawn](decisions/0015-detached-skill-spawn.md): `spawn_skill` 立即返回句柄、子 skill 在独立 thread 后台跑完，父 turn 不阻塞；join-barrier 负责全终态聚合。
19. [ADR 0019: Post-turn hook](decisions/0019-post-turn-hook.md): turn 收尾钩子，为自我 review / 记忆固化等认知回路提供跨 turn 的顺序保证落脚点。
20. [ADR 0020: Budget awareness hint](decisions/0020-budget-awareness-hint.md): 穿越 soft limit 时注入中性的预算事实，让模型自知剩余空间，而非由内核代它裁剪。
21. [ADR 0021: Doom-loop detection](decisions/0021-doom-loop-detection.md): 识别工具调用的原地打转并注入提示，避免无进展的循环烧完预算。
22. [ADR 0022: Reusable approval grants](decisions/0022-reusable-approval-grants.md): 一次人工批准可在其作用域内复用，避免同一操作反复弹审批。
38. [ADR 0038: engine / turn 按职责切分为协作者模块](decisions/0038-loop-core-module-split.md): 切分手法按**内聚度**二分（内聚序列成协作者类、独立处理成模块函数），协作者不自持运行态；宿主是**唯一白盒寻址面**（薄委托 + 兄弟调用回弹，兼顾「打宿主属性」与「打模块级符号」两类 monkeypatch 注入点）；行为零变化的判据是「既有测试一行未改即全绿」。`engine.py` 2100 行与 `sample_once` 三段为两处具名红线例外，理由与上限均可核算。活文档 [loop-core-module-structure](architecture/capabilities/loop-core-module-structure.md)。
39. [ADR 0039: 网络重试上事件总线——经 session 可选观察者，而非流内事件](decisions/0039-network-retry-observable.md): `RetryingModelClient` 此前每次退避只写 `logger.info`，事件总线上看不到任何一次网络重试（实测 3 次 attempt、0 条事件），R3 点名的 `provider_retry` 只服务 overflow 自愈。决策：复用 `ProviderRetry` 以 `reason` 区分来源；观察者经 session 可选协议 `set_retry_observer` 注入、宿主 `getattr` 探测（协议签名不动、透明包装靠 `__getattr__` 接通）；**不**往 `ResponseEvent` 流塞新 kind（金样形状会随重试次数漂移）；退避**之前** emit；OTel `taifeng.provider.retries` 落地。断路器与 `retryable_kinds` 两套真相另案。
40. [ADR 0040: `response.incomplete` 未知 reason 按可重试默认处置，不再判协议违规](decisions/0040-incomplete-unknown-reason-retryable.md): ADR 0033 对 `code` 不做闭集（官方枚举演进、网关自造），却把 `incomplete_details.reason` 集合外的值判协议违规 → `InvalidResponseError` 不可重试，保守策略下 turn 直接判死——实测 `new_reason_2027` 与缺失 details 均如此。决策：与认不出的 code 同一默认，→ `ServerError`（可重试：退避重试 → 仍败才挂起），原文 reason 留在异常消息；已知值处置不变。部分推翻 ADR 0033。
41. [ADR 0041: 内核默认套有界重试；`unreliable_finish` 并入默认可重试集合](decisions/0041-auto-retry-on-by-default.md): ADR 0037 把重试留给业务侧显式套，实测接入方（qiuben api）任何一层都没套 → 生产零自动重试，中转一次抖动直接挂起等人。决策：`with_default_retry` 唯一幂等入口，`AgentEngine` / `AgentEnginePool` / `EnginePool.create` 三处默认包装；已套过（`bounded_retry` 标记，穿透 `__getattr__` 包装）/ strict audit 适配器 / `auto_retry=False` 原样；默认策略即生产策略；`unreliable_finish` 并入默认集合。波及恰 3 条「重试已耗尽」前提的用例，各加 `auto_retry=False`。推翻 ADR 0037 该 Non-goal。
42. [ADR 0042: provider 级断路器——跨 turn 的上游健康度，open 态快速失败](decisions/0042-provider-circuit-breaker.md): 分类/重试/挂起/TTL 四层闭合，但跨 turn 的 provider 失败记忆为零（`denial_breaker` 只管权限拒绝）——中转宕 10 分钟时每个 turn 各烧满 `max_attempts` 再挂起，N 路并发 = 3N 次注定失败的请求。决策：`CircuitBreakingModelClient` 状态挂装饰器实例（一个 endpoint 一个，进程内共享；per-engine 会让 N 个 engine 各学一遍）；构造时经 `with_default_retry` 保证叠在重试层外，**只计最终结局**（取消 / 不可重试类不计）；open 态不触网抛 `circuit_open`（`retryable=True` → SUSPEND，但不进 `retryable_kinds`）；半开只放行 1 个探测、失败则冷却翻倍封顶；三态各一个事件 kind + OTel `taifeng.provider.circuit_transitions`。默认阈值 3 / 30s / ×2 / 300s，但**默认不套** ⇒ 既有部署零行为变化。
43. [ADR 0043: 上下文 token 计数以 provider 实测校准 + usage 口径归一](decisions/0043-token-accounting-calibration.md): 压缩 / 预算提示 / 预检只靠 `len/3.5` 粗估，看不到 system / 工具开销、CJK 严重低估；Anthropic `input_tokens` 不含缓存致 K2 与 `cached_ratio` 失真。决策：`TokenUsage.input_tokens` 统一为含缓存的完整 prompt；采样成功记 `TokenCalibration` 锚点，估算 = 实测 + 锚点后增量粗估（codex 范式）；前缀被改写时锚点失效但保留 overhead；overhead 不做负修正；新增 `ContextBudget.output_reserve_tokens`（默认 0）。顺带修 `UpdateBudget` 重置 `max_request_bytes` 的 bug。
44. [ADR 0044: 会话用量整棵 turn 树共享记账，采样即入账](decisions/0044-usage-tree-accounting.md): 会话累计唯一入账点是根 turn 收尾，call_skill / spawn / 续跑子树 usage 从不回灌，K2 可被子树绕过。决策：engine 持 `SessionUsageMeter` 注入整棵树、采样即入账并按 skill / thread 归因；`turn_completed.usage` 语义不变，另加 `subtree_usage` / `thread_id` / `skill_id`；detached spawn 不并入父 subtree。
45. [ADR 0045: 工具执行途中崩溃的冷恢复——写前意图 + 按副作用类型分流](decisions/0045-tool-crash-reconciliation.md): Chat 路径 fc 在执行后才成对落盘，执行中崩溃则意图丢失、resume 后副作用静默重复；Responses 一刀切「未知」；`effect_kind` 恢复时无人读。决策：派发前落记账类 `tool_intent`（同进 hot history，不改交错结构）；冷恢复按副作用分流（pure/idempotent 告知可重发、`reconcile` 回查、其余 `TOOL_OUTCOME_UNKNOWN` 挂起交人 retry/provide/abort）；恢复从不自动执行工具；`tool_recovery="report"` 保留旧行为。
46. [ADR 0046: 原生 provider 的 thinking 块与签名回传](decisions/0046-thinking-signature-passback.md): Anthropic provider 文件头宣称支持 extended thinking 却只解析 text/tool 增量、不能开启；Gemini 不处理 thoughtSignature——两家工具续传都要求原样回传带签名的思考内容。决策：新增不透明 `reasoning_state` 事件 / `ApiMessage.reasoning_state` / `provider_reasoning` 落史通道（内核只搬运）；Gemini 复用 `extra_content.google.thought_signature`；redacted-only 也落史；thinking 与 temperature / 过小 max_tokens 冲突显式报错；None 时不参与序列化保审计 digest 不变。真实端点验证未执行（无 key）。
47. [ADR 0047: 工具参数派发前按 input_schema 校验](decisions/0047-tool-argument-schema-validation.md): 派发层只查合法 JSON 对象，缺字段 / 类型错直进 handler 或被 `args.get` 静默兜底（`call_skill` 必填 `reason` 即如此）。决策：不合 schema 不执行 handler，返回违例 + schema 让模型改参；内置子集校验器（不引 jsonschema，不认识的关键字放过）；主派发 / retry / 编排 / resume / 子 thread 续跑共用单一入口；不设关闭开关。
48. [ADR 0048: 工具集运行时增删 + MCP list_changed 同步 + streamable HTTP 传输](decisions/0048-dynamic-tool-set-and-mcp-http.md): 注册表只能 register、MCP 忽略服务端通知且只有 stdio、指纹只比工具名。决策：`unregister` / `replace` / `version` / `subscribe` + `tool_set_changed` 事件，变更在下一次采样生效（不冻结到 turn）；指纹含描述与 schema；`McpClient` 协议 + `bind_mcp_tools` 只管自己的工具；`McpHttpClient` 用 httpx 实现 streamable HTTP，不做 OAuth。
49. [ADR 0049: 取消 token 携带原因与墙钟截止时间，采样流与取消竞速](decisions/0049-cancel-reason-and-deadline.md): token 只有一个比特，终态分不清中止 / 超时 / 关停，也没有墙钟截止时间；采样循环只在收到流事件时查取消，provider 首字节前阻塞时取消迟迟不生效。决策：`CancelReason` 随取消级联；`child(deadline_seconds=)` 挂在子树根、只收紧；`UserMessage.deadline_seconds` / `spawn_skill(deadline_seconds=)` 入口；`interrupt_on_cancel` 让取消原地打断阻塞中的流读取；`turn_completed.cancel_reason`。
50. [ADR 0050: 后台任务完成时唤醒发起方，而非只能轮询](decisions/0050-background-completion-wake.md): `run_in_background` 只能靠 `wait_for_task` 轮询。决策：注册表 `on_complete` 回调；工具经 peer mailbox 把中性摘要投回发起 thread（根 queue_only、spawn 子 trigger_turn、call_skill 子改投根）+ `background_task_completed` 事件；默认开。
51. [ADR 0051: shell 类工具经 CommandExecutor 启动进程](decisions/0051-command-executor-seam.md): 脚本有 `ScriptExecutor`，`shell_exec` / `run_in_background` 却直接起子进程，沙箱无统一 seam；`shell_exec` 还不响应取消。决策：只抽「启动」一步为 `CommandExecutor.start(CommandSpec)`，审批 / env / 超时 / 截断 / 取消留在工具；内核只给本机默认实现；`shell_exec` 在 `interrupt_on_cancel` 内等待、取消即 kill。
52. [ADR 0052: cache 失效分段归因](decisions/0052-segmented-cache-break-attribution.md): 指纹只有 skill / 工具名 / system，模型切换、同名 schema 变化、前缀被 rollback 都落 `unknown_drop`。决策：工具段含描述与 schema；新增 `model` 与消息前缀（发出时长度 + id 序列哈希）段；新增 `model_changed` / `message_prefix_changed`；旧指纹缺键不误判。
53. [ADR 0053: 审计 Session resume 与跨进程写者接管](decisions/0053-audited-session-resume-and-writer-takeover.md) (Amends #0025): writer fencing 只在进程内、append 前重扫只能事后发现并发写，崩溃的审计 Session 无法恢复。决策：`flock` 真互斥（锁 fd 持有到关闭，非 POSIX 显式报错）；`open_existing` 以 epoch+1 写 `writer_takeover`，verify 强制 epoch 单调；`session_ended` 后不可重开；resume 遇未结算 effect 一律 fail closed 并列出 record，失败只释放 lease 不写终态。
54. [ADR 0054: 用审计 Journal 做确定性回放](decisions/0054-journal-deterministic-replay.md): Journal 已记请求摘要与最终响应，却无消费者可回放。决策：`JournalReplayClient` 以 Journal 为数据源、按 canonical 请求摘要匹配录制调用（并发子 turn 稳定）；分叉显式 `ReplayDivergenceError`；只支持 Chat 协议录制。
55. [ADR 0055: 原生 Anthropic / Gemini 把历史中段 system 消息原位改写为带标签 user 文本](decisions/0055-mid-history-system-as-tagged-user.md): 两家 provider 直接丢弃 messages 里的 system，压缩摘要 / pinned / 预算提示 / 记忆 / 业务注入全部静默消失，压缩即删历史。决策：原位改写为 `<system-reminder>` 包裹的 user 文本并与相邻 user 合并；否决并入顶层 system（破坏 cache 前缀）与 prompt 层统一改写（改变 OpenAI 系 wire 与审计 digest）。
56. [ADR 0056: SKILL.md `inference` 块声明 skill 级推理参数](decisions/0056-skill-inference-params.md): `ApiRequest` 早有 reasoning_effort / temperature / max_output_tokens 且各 provider 都会翻译，内核却从未设置，skill 无法按任务定参数。决策：frontmatter 嵌套 `inference` 块、atomic / composite 通用、加载期严格校验、经 `build_api_request` 按 entry skill 下发；否决平铺顶层键与 Pool 级默认值。

### Fourth Pass: Gap Tracking

22. [Hermes capability gap roadmap](architecture/hermes-gap-roadmap.md): feature-level progress.
23. [Kernel gap analysis](architecture/kernel-gap-analysis.md): kernel primitive progress.

The two gap documents are complementary: the roadmap answers "which features exist?", while the kernel gap analysis answers "which kernel mechanisms are complete?".

## Documentation Categories

| Directory | Purpose | Lifetime |
| --- | --- | --- |
| `architecture/` | Current architecture and gap analysis, including the capability contract layer | Long-lived; updated as implementation changes |
| `architecture/capabilities/` | Stable field-level contracts for data structures, protocols, events, and constraints | Long-lived; updated with capability changes |
| `decisions/` | ADR decision records | Permanent; append-only |

Use `architecture/` for the current system shape. Use `decisions/` for why a decision was made.

## Maintenance Rules

- Do not rewrite existing ADRs. If a decision changes, write a new ADR and mark the superseded record.
- Keep architecture docs synchronized with implementation changes.
- Capability changes must update the relevant contract and [capability matrix](capability-matrix.md).
- Generated ledgers such as [real-llm-ledger.md](real-llm-ledger.md) should be updated by their generation scripts, not by hand.
- The example tier lists (which demo needs a real LLM key) are owned by `scripts/verify_examples.py`; docs must follow the script, not a hand-maintained list. `.github/workflows/ci.yml` runs the full test suite and the example smoke on every push to main and every PR; real-LLM regression stays manual (see the ledger red line in `AGENTS.md`).
