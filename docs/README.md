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
   - [Public API tiers](architecture/public-api.md): stable (`taifeng.__all__`) / experimental (`taifeng.experimental`) / internal, and the deprecation policy.
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
41. [ADR 0041: 内核默认套有界重试；`unreliable_finish` 并入默认可重试集合](decisions/0041-auto-retry-on-by-default.md): ADR 0037 把重试留给业务侧显式套，实测某接入方任何一层都没套 → 生产零自动重试，中转一次抖动直接挂起等人。决策：`with_default_retry` 唯一幂等入口，`AgentEngine` / `AgentEnginePool` / `EnginePool.create` 三处默认包装；已套过（`bounded_retry` 标记，穿透 `__getattr__` 包装）/ strict audit 适配器 / `auto_retry=False` 原样；默认策略即生产策略；`unreliable_finish` 并入默认集合。波及恰 3 条「重试已耗尽」前提的用例，各加 `auto_retry=False`。推翻 ADR 0037 该 Non-goal。
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
57. [ADR 0057: MCP 工具副作用分类默认保守，annotations 需显式信任](decisions/0057-mcp-tool-effect-classification.md): 桥接工具落默认 `pure`，崩溃恢复会引导模型重发 MCP 写操作。决策：默认 `external_non_idempotent`（挂起交人）；`trust_annotations=True` 才按 `readOnlyHint` / `idempotentHint` 细分；否决默认信任（规范称不可信提示不得据以决策）。
58. [ADR 0058: SKILL.md 严格加载](decisions/0058-strict-skill-loading.md): 坏 frontmatter 被 warning 后跳过、超长 body 被截断、`bool()` / `frozenset()` 强转把 `entry: "false"` 读成 True、多目录同名静默覆盖。决策：前三类加载期报错；覆盖保留分层语义但告警；顶层未知键仍透传。
59. [ADR 0059: 压缩后的衔接——保留最近用户原话 + 续接前言 + 摘要新分段](decisions/0059-compaction-continuity.md): 用户原话被一并转述最易走样；文档有续接提示语、代码没有；摘要缺「当前工作」「错误与修复」。决策：压缩条目 = 续接前言 + 区间内最近用户原话（20k token）+ 摘要，全在同一 `compacted` item；否决另插 user item（破坏 replaced_range 且会被当新请求）。
60. [ADR 0060: read_skill 读取 skill 目录内附属文件](decisions/0060-read-skill-auxiliary-files.md): 渐进加载缺第三层，正文引用的 references 无从读取。决策：`read_skill(skill_id, path)`，路径限定 skill 目录（含符号链接）、UTF-8、≤256KiB；否决把 `allowed-tools` 当 `tool_names` 别名（免审批 ≠ 可见白名单，工具名不通用）。
61. [ADR 0061: PostToolUse 可改写工具输出 + 工具结果统一字节上限](decisions/0061-post-tool-output-override-and-result-cap.md): PostToolUse 返回值被丢弃，宿主无处清洗工具输出；MCP / 业务工具输出无上限进入历史。决策：`output_override` 链式改写并打事件标记；`ContextBudget.max_tool_result_bytes` 默认 128KiB 保头尾截断，配 offload 时让位；否决钩子异常 fail-closed 与逐工具上限。
62. [ADR 0062: Anthropic 尾部滚动缓存断点 + TTL 透传](decisions/0062-anthropic-rolling-cache-breakpoint-and-ttl.md): 只在 cache anchor 打一个标记，工具循环的尾部每轮全价重复计费；`ttl_seconds` 是死字段。决策：客户端默认在最后一条消息再打标记；300 / 3600 映射 5m / 1h，其他值报错；否决在 `build_api_request` 加断点与默认 1h。
63. [ADR 0063: MCP 客户端结果无损投影、elicitation 注入口与 2025-06-18 版本协商](decisions/0063-mcp-client-content-elicitation-version.md): tools/call 结果把图片降级成无信息占位、丢弃 structuredContent；server 发来的 elicitation/create / ping 无人应答（stdio 还会与本端请求撞号误结算）；stdio / HTTP / server 三处协议版本各写各的且从不校验。决策：客户端声明 2025-06-18，只接受 `SUPPORTED_PROTOCOL_VERSIONS`（含 2025-03-26 / 2024-11-05）内的协商结果、否则断开抛 `McpProtocolVersionError`，HTTP 带协商后的 `MCP-Protocol-Version`；structuredContent 进 `ToolResult.data` 并在缺等价 text 块时补 JSON；图片默认为带 MIME 与字节数的占位、`attach_images=True` 时走工具附件 admission；`ServerMessageRouter` 统一应答 server 请求，注入 `ElicitationHandler` 才声明能力、未注入回 -32601；否决只收最新版、`attach_images` 默认开（默认策略下会让带图调用整次失败）、内核设 handler 超时、structuredContent 取代 content。
64. [ADR 0064: opt-in 的 glob / grep 搜索工具与 memory 薄工具](decisions/0064-opt-in-search-and-memory-tools.md): 模型找文件只能猜路径或借 `shell_exec` 跑 find/grep（放开整条 shell 权限、崩溃后挂起交人），记忆也只能被动接受内核 page-in。决策：新增 opt-in 的 `glob` / `grep`（纯 Python、只读 pure 可并行、沙盒判定同 `file_read`、`file_read` 效果每次调用审批一次、结果按路径排序）与 `memory` 薄工具（search→`prefetch`、save→`writeback`，不带后端不扩协议，含 save 时取 `external_non_idempotent`）；否决外部 rg、逐文件审批、内置记忆后端与 delete 动作。
65. [ADR 0065: pinned 状态周期重注](decisions/0065-pinned-state-periodic-reinjection.md): 清单只在压缩后钉回，未压缩的长会话里工作记忆失焦。决策：`PeriodicPinnedStateSource.reinject_every_turns` 按 source 声明节奏，计数从 history 推导，pre-turn 压缩后尾追加；否决 engine 级全局旋钮与按迭代计数。
66. [ADR 0066: 公共 API 分稳定 / 实验 / 内部三层 + 弃用策略](decisions/0066-public-api-tiers-and-deprecation.md): 顶层 ~160 个符号无分层、零弃用机制。决策：顶层 `__all__` 为稳定层并以快照守护；`taifeng.experimental` 承载 🧪 入口；`DEPRECATED_ALIASES` + `__getattr__` 发 `DeprecationWarning`，弃用期 ≥ 两个版本且 ≥ 30 天；否决挪动已发布符号。
67. [ADR 0067: 预留协议——skill 战绩聚合、文件输入、多模型路由组合](decisions/0067-reserved-protocols-fitness-file-input-routing.md): 三项「先定协议」候选。决策：战绩聚合落代码（`SkillFitnessStore` + TelemetrySink 适配，实验层，只沉淀不决策）；文件输入只写预留契约（避免死代码）；路由 / 回退写包装器组合契约（回退在断路器外、零产出才回退、能力取交集）。
68. [ADR 0068: 用户消息文件（PDF）输入——与图片输入同构落地](decisions/0068-user-file-input.md): 预留契约转正式实现。决策：`FileAttachmentV1` / `FilePart` / `FileInputPolicy` 与图片三件套同构（字段名对齐 `ImageAttachmentV1` 的 `content`/`size`，不用预留稿的 `data`/`size_bytes`），同一 `UserMessage.attachments` 入口，入队准入与 prompt 重建共用一道能力门与 admission；`"file"` 能力由 OpenAI Chat / Responses、Codex、Anthropic、Gemini 显式声明，OpenAICompat / LiteLLM 序列化前拒绝；按 PDF 页数给保守 token 上界；strict audit 显式拒绝文件；入口进实验层（Supersedes ADR 0067 决策 2）；否决 LiteLLM 透传、按字节推页数、引 PDF 解析库、降级为文件名文本、随本切片扩 Journal schema。
69. [ADR 0069: MCP 协议补全——分页、outputSchema、取消通知、HTTP 断流续传与 server 侧 elicitation 能力门控](decisions/0069-mcp-protocol-completeness.md): tools/list 只取首页、忽略 outputSchema、本端放弃请求不通知 server、HTTP SSE 断流即失败、server 向未声明能力的客户端发 elicitation。决策：list_tools 跟完 nextCursor（页数上限 / 重复游标 → `McpPaginationError`，不截断）；outputSchema 按官方 SDK 口径校验（isError 不校验，违例判 `mcp_output_schema_violation`、原文不进模型，变化触发 sync 替换）；放弃请求以后台任务发 `notifications/cancelled`，原因经取消消息传递（`call_tool` 签名不变），桥接工具响应 turn 取消；POST 流按 `Last-Event-ID` 续传（≤`max_stream_resumptions`，无 id / 405 / 用尽显式失败），推送流连续无事件才放弃重连；`server_initiated_request` 按客户端能力门控，`McpPrompter` 立即 fail-closed deny；否决截断返回、引入 jsonschema、改 `call_tool` 签名、断流重发 POST、门控只放 prompter。sampling / roots / resources / prompts / OAuth 维持 ADR 0017 规则④。
70. [ADR 0070: 审计 resume 按副作用收敛结果未知的工具调用；Journal 回放支持 Responses 协议](decisions/0070-audit-resume-reconcile-and-responses-replay.md) (Amends #0053, #0054): 审计 resume 遇悬空 intent / durable unknown outcome 一律拒绝，Responses 录制因采样 id 随运行变化无法回放。决策：持写者锁后按 ADR 0045 同序分流（回查 / 幂等可重发 / `AuditConfig.tool_outcome_resolver` 人裁决），结论写成新的 `tool_recovery_committed` 原子追加、全有或全无、已有结果只接受「未执行」或人接受未知；回放以「采样 id 一致双射重命名下逐字节相同」为判据，定位后按录制 preimage 复核完整摘要，并按录制还原密文 / thinking 签名 / `extra_content`；否决只比安全投影（放宽匹配）、部分落账、复用挂起交人与自动回填 unknown。
71. [ADR 0071: grep 上下文行 / 跨行 / .gitignore，memory 可选删除协议，max_output_tokens 联动输出预留](decisions/0071-grep-context-memory-forget-output-reserve.md) (Amends #0064, #0043, #0056): grep 只给命中行、找不到跨行片段、不认 .gitignore；记忆只能增不能删；skill 声明大输出上限后 soft / hard 仍只按预算预留算，压缩太晚直接超窗。决策：grep 加 `context_*`（仅 content、占结果名额、重叠合并、`--` 分隔）与 `multiline`（整文件 MULTILINE|DOTALL、起止行号 + JSON 片段）；glob / grep 纯 Python 解析 .gitignore，`respect_gitignore` 默认开（未发布无兼容成本、与 rg 一致、挡本地密钥文件，跳过数尾注告知）；可选协议 `ForgettableMemoryStore.forget(target, *, thread_id) -> int`，memory 工具仅在 store 可遗忘时出现 `delete`；生效输出预留 = max(`output_reserve_tokens`, entry skill `max_output_tokens`)，每个 runner 按自己的 entry skill 派生，≥ 窗口显式 `turn_failed`；否决上下文行不占名额、调用 git、第三方 regex、扩 `MemoryStore`、按 id 删除与装配期校验。
72. [ADR 0072: codex 协议把历史中段 system 注记原位改写为带标签 user 消息](decisions/0072-codex-mid-history-system-in-place.md) (Amends #0026): 中段注记被折叠进顶层 `instructions`，丢失位置（pinned 周期重注排到对话之前，真实场景 5 次挂 3 次）且每次注入改写缓存前缀。决策：原位改写为 `<system-reminder>` 包裹的 user `input_text`，`instructions` 只含 system prompt，与 ADR 0055 同一处置；否决保持折叠与改用 developer item。
73. [ADR 0073: apply_patch 按路径申请 file_write；`ApplyPatch` 别名并入 `FileWrite`](decisions/0073-apply-patch-file-write-permission.md) (Amends #0028): `apply_patch` 以 `tool_use` 申请权限且不带路径，按路径写的写禁令拦不住它。决策：每个被改动的路径一条 `file_write`（含删除），解析 → 审批 → 内容校验 → 应用，任一被拒整组不执行；`ApplyPatch` 成为 `FileWrite` 的同义别名。
74. [ADR 0074: Anthropic / Gemini 回放写坏的 tool call 参数时用显式标记，不静默改成 `{}`](decisions/0074-tool-arguments-replay-marker.md) (Amends #0036, #0037): 回放历史 tool call 时非法 JSON 被静默改成 `{}`，非对象 JSON 原样穿透被 provider 以 400 拒绝。决策：统一入口 `replay_tool_arguments`，解析失败回放带错误分类与原始文本的标记对象并记 warning，不抛异常、输出确定。
75. [ADR 0075: 审计 resume 收敛「模型回复已落账、意图尚未登记」的工具调用](decisions/0075-audit-resume-undispatched-tool-calls.md) (Amends #0070): 进程死在模型回复与意图两个 batch 之间时，resume 能通过但 history 末尾留着没有结果的 `function_call`。决策：没有意图即没有执行，恢复时追加 `tool_call_undispatched` + 「未执行」结果，不回查、不征求人裁决；无法构成 identity 的 call id 交人。
76. [ADR 0076: 审计 resume 沿 skill 派发树自底向上收敛子 thread 的工具调用](decisions/0076-audit-resume-dispatch-tree-recovery.md) (Amends #0070): 崩溃发生在同步 `call_skill` 的子 skill 执行途中时 Session 只能废弃。决策：把派发层的未结算拆解到其内部的工具调用，子调用 → 派发终态 → 父调用同批自底向上收敛；悬空 `call_skill` 意图按派发谱系结算；被中断的执行不记战绩、不续跑。
77. [ADR 0077: 按战绩算分与工作集规划先以影子模式上线](decisions/0077-skill-fitness-shadow-mode.md): 相位 5 连影子模式都未开始，无从积累可核对的数据。决策：Wilson 置信下界算分（放弃不进分母、可选成本折减、不读选择置信度）；工作集与隔离集无状态重算；影子评估做成 TelemetrySink，内核不持有其引用，「不生效」由结构保证。
78. [ADR 0078: spawn 拒绝带稳定分类，对模型与事件流可见](decisions/0078-spawn-reject-classification.md): detached spawn 的准入拒绝以普通异常冒出，经工具时被当作工具故障（`reason="exception"` + traceback），事件流无拒绝事件。决策：`SpawnRejectReason` 稳定分类；`SpawnRejectedError(ValueError)` 与 `SpawnLimitError.reject_reason`；`spawn_skill` 把准入拒绝当作结果返回并 emit 统一形状的 `skill_spawn_rejected`。
79. [ADR 0079: 并行批次里的 retry_tool 按批次截断，只重跑目标调用](decisions/0079-retry-tool-in-parallel-batch.md) (Amends #0014, #0016): 一次采样发出多个调用时 retry_tool 会截掉同批其他调用的结果（或整个调用）。决策：`plan_retry_cut` 按批次规划，保留到批次末尾、只去掉目标调用的旧结果；marker 记 `drop_index` 供冷重建重放；单调用批次行为逐位不变。
80. [ADR 0080: 挂起态下允许 rewind，挂起随截断一并作废](decisions/0080-rewind-while-suspended.md) (Amends #0014, #0018): 挂起时 rewind 一律被拒，人想改主意只能取消整个 turn。决策：截断把挂起 record 连同等待的调用一起带出逻辑 history；守卫判定截断后是否自洽——同批还有调用等人时对已结算调用的 `retry_tool` 被拒（`sibling_calls_pending`）；被拒的 rewind 不改动状态。
81. [ADR 0081: 压缩作为回访节点——可回到某次压缩之前](decisions/0081-compaction-as-rewind-node.md) (Amends #0014, #0016): 压缩是对 history 影响最大的内核动作却不可寻址，摘要有损时无法回头。决策：每个仍在的 `compacted` 条目是 `compaction` 节点；rewind 它 = 从 transcript 重放还原到压缩之前（去掉压缩写下的旁路项）；新增 `restore` 模式只还原不重推，`re_reason` 仅在轮到模型说话时可用；仅 root thread。
82. [ADR 0082: 多模态重载荷驱逐——旧附件换成描述](decisions/0082-multimodal-payload-eviction.md): 用户消息上的附件没有任何压缩策略处理，带图会话在文本远未触顶时就被迫整体摘要。决策：独立策略 `MultimodalEvictionStrategy`，只看附件不动文本，用含类型 / 大小 / 文件名 / sha256 前缀的描述替换；「最近」按带附件的条目计数；无候选不触发；实验层。
83. [ADR 0083: 压缩增量基线——上次压缩后没长多少就不再压](decisions/0083-compaction-growth-baseline.md): 压缩腾不出多少空间时估算停在软阈值之上，每次预算检查都再压一次，反复破坏缓存、对摘要再做摘要。决策：基线记在 `compacted` 条目的 metadata 并随条目落 transcript；`recompact_min_growth_ratio` 设闸，到硬阈值、手动与 overflow 压缩不设闸；默认关闭。
84. [ADR 0084: 失败恢复配方可由业务声明](decisions/0084-declarable-recovery-recipes.md): 事件里的恢复配方来自内核写死的表，业务只能另查一张，两张表各说各话。决策：`RecoveryRecipeBook` 按失败分类覆盖并在构造期校验；经失败处置 policy 的可选能力注入；`custom_steps` 承载业务动作；声明的配方带 `source: "declared"`；内核仍只透出不执行。
85. [ADR 0085: 输入来源标记——内核记来源、汇总透出，不做裁决](decisions/0085-input-origin-tagging.md): 上下文里外部内容越来越多，内核却不记录内容是谁给的，业务无从实现「读过外部内容后先问人」。决策：送入方声明来源（Op 的 `origin`、`ToolSpec.output_trust`），派生内容继承不可信标记，汇总经 `input_taint` 交给工具与 hook；标记不进 prompt，内核不裁决。
86. [ADR 0086: 出站消息归一化——最终回答经 hook 归一后再交给业务](decisions/0086-outbound-message-normalization.md): 内核没有「这一轮的最终回答」事件，也没有改写它的入口，业务只能自己拼接增量再处理。决策：hook 类型 `outbound_message`（`text_override` 链式改写、不可否决）+ 同名事件（先于 `turn_completed`）；只改出站文本不改 history；只对 root turn 真终态；内核自带 opt-in 的渠道无关归一化。
87. [ADR 0087: 后台延迟压缩——先在后台算摘要，下一轮开始时应用](decisions/0087-background-deferred-compaction.md): 摘要类压缩发生在用户提交消息之后、模型回答之前，长会话里首字延迟多出十几秒。决策：包装策略在 history 快照上后台计算，下一次 pre_turn 核对前缀后应用；逼近硬阈值或后台失败过则同步；只在 pre_turn 起后台与应用；须为唯一策略；pool 关闭时收尾。
88. [ADR 0088: 按选择置信度分流——低置信候选不可直接派发](decisions/0088-skill-selection-confidence-gate.md): 相位 2 只把置信度交给模型自行掂量，低置信误派发要到子 skill 跑完才暴露。决策：`proceed` / `trial` / `escalate` 三档；`search_skills` 标注 `route`，`call_skill` / `spawn_skill` 在派发处强制；`trial` 须先 `read_skill` 或试用门放行，`escalate` 本轮不可派发；判定由 history 推导；只约束本轮经召回看到的 skill；默认不启用。
89. [ADR 0089: 白名单外 skill 的派发授权](decisions/0089-skill-authorization-outside-whitelist.md): 白名单同时是工作集和授权边界，调用方够不到作者没列出的 skill。决策：`DispatchPolicy.authorization` 注入 `SkillAuthorizationPolicy`（`discoverable` / `authorize`）；召回池并入白名单外可发现的 skill；授权只豁免白名单一层；参考实现复用权限门（范围 `skill_authorization`）；`call_skill` 获批后在续跑的 turn 内重跑（修复此前恢复时 `call_skill misconfigured`）。
90. [ADR 0090: 按战绩规划的工作集生效，并引入 skill 来源信任分层](decisions/0090-working-set-enforcement-and-trust-tiers.md): ADR 0077 的结论只记录不生效，`trust_tier` 自 v1 起恒为空。决策：`DispatchPolicy.working_set` 注入 `SkillWorkingSet`，战绩落定后直接重算；结论在 turn 开始时取快照；召回模式下直接列出工作集里的 child；隔离作用范围 `flag` / `hide` / `block`；来源信任三层由加载目录决定，只调门槛不进战绩分，同时供分流门与授权使用。
91. [ADR 0091: peer 拓扑路径寻址——按对方跑的 skill 指代它](decisions/0091-peer-topology-addressing.md): peer 消息只能按运行时才产生的 id 寻址，兄弟专家互发消息得靠协调者转告句柄。决策：`sibling:<skill_id>` / `child:<skill_id>` 可加 `#<n>`；关系名须与发送方相符；多实例不猜；失败实例不参与；解析是读句柄表的纯函数；事件留痕原始地址。
92. [ADR 0092: 预热——在用户输入到来之前做掉首轮采样的准备工作](decisions/0092-prewarm.md): 首轮采样承担全部冷启动开销，内核没有入口利用用户开口前的空闲。决策：`Prewarm` Op 三步（指令层 / 工作集 / 模型侧）；模型侧是协议 `ModelPrewarmer`，参考实现用一次输出极短的采样；预热请求与真实采样同一套组装；持 root gate 但给用户消息让路；不留痕迹；失败不传染；消耗记进会话账。
93. [ADR 0093: ContextEngine 可插拔槽位——history 不动，只改发出去的视图](decisions/0093-context-engine-slot.md): 发给模型的内容恒等于 history，非破坏性的上下文管理没有入口。决策：槽位只管视图装配与轮后通知，不接管压缩；视图可含 history 外的条目；内核只做结构校验，不合法即失败不退回；预算与压缩触发按视图估算；同一 history 版本只装配一次；缓存影响由引擎声明；引擎挂在压缩协调器上。
94. [ADR 0094: 审计模式放开折叠式上下文压缩与预算提示](decisions/0094-audit-mode-compaction.md): 审计模式拒绝任何压缩，长会话只能跑到溢出；预算提示绕过 Journal 直写投影。决策：只放开折叠式策略并由策略声明；`context_compacted` 与摘要条目同批；摘要的 LLM 调用经内核提供的会话落账，失败也落；先落账后改 hot history；只在采样之间压缩；回写时排除被折叠的条目；预算提示一并落账。
95. [ADR 0095: 审计模式接受文件附件与工具结果里的图片](decisions/0095-audit-mode-attachments.md): 带文件的用户消息在审计模式被拒，工具返回图片会冻结 Session。决策：文件附件另立 DTO（不改图片附件的字节形状），按 `kind` 区分；准入与非审计路径同一口径外加 Session 字节上限；工具图片随结果对话项落账、outcome 只记摘要；不合格时结果变成错误而不冻结。
96. [ADR 0096: 审计模式放开 hook 与不挂起的权限裁决](decisions/0096-audit-mode-hooks-and-permission.md): 审计模式拒绝任何 hook 与权限策略，而需要审计的部署最需要它们。决策：业务的 handler 与策略原样运行，内核按 turn 绑定一层，裁决先落账再生效；每个 handler 每次裁决一条 `hook_evaluated`，改写内容落账、其余 metadata 只记键名；`permission_decided` 记请求与裁决；上下文无法落账的请求被拒；挂起式审批另行立项。
97. [ADR 0097: 审计模式放开挂起与恢复](decisions/0097-audit-mode-suspension-and-resume.md): 需要审计的部署里审批人很少在线等着，而审计模式拒绝一切挂起。决策：只放开「在工具调用处停下等人作答」的挂起（挂起式审批，及声明 `can_suspend` 的工具发问）；等人的调用保持未结算，结果记在原来的 operation 下；`Resume` 先准入落账再入队，且须答复全部请求；`resume_applied` 写在续跑之前；等待期间释放写 `session_detached` 而不终结，可被接管；子 skill 内的挂起、带到期时间的挂起不做。
98. [ADR 0098: 审计模式放开分离式派发](decisions/0098-audit-mode-detached-spawn.md): 审计 Session 里直接调 `engine.spawn_skill()` 时子 skill 照常运行却不进 Journal。决策：发起是 `spawn_started` 与子 thread 创建、种子同批，终态是 `spawn_settled` 与 `thread_terminal`；不写锚点条目（避免两个写者打乱投影顺序）；句柄表由记录重建；接管时没有终态的派发落 `cancelled` 不续跑；子 thread 上的调用不能停下等人；等待工具的时长设上限；barrier 与 peer 消息另行立项。
99. [ADR 0099: 审计模式放开 join-barrier](decisions/0099-audit-mode-join-barrier.md): 只有派发没有 barrier，「并行铺开、全部跑完后汇总」在审计模式下要靠模型自己轮询。决策：`barrier_registered` / `barrier_fired` / `barrier_settled` 三条记录，点火记下各成员的终态与聚合输入，聚合 turn 的终态要落账；登记了没点火的在接管后点火，被中断的聚合 turn 落 `cancelled` 不再点火；两种模式下终态顺序改为「持久化 → 句柄状态 → 事件」。
100. [ADR 0100: 审计模式放开 peer 消息](decisions/0100-audit-mode-peer-messages.md): 消息要进别人的 thread，而那个 thread 有自己的写者，且 Journal 顺序就是接管时的对话顺序。决策：发出与进入对话是两条记录、由两个写者各写一条；进入对话的时刻是目标 runner 的迭代边界；root 的收件队列跟着 Session 走；已经结束的子 thread 不接受消息、不被唤醒；接管时未进入对话的消息回到收件队列；`SendToPeer` Op 不放开。
101. [ADR 0101: 审计模式下用户消息的对话项在应用时落账](decisions/0101-audit-mode-application-time-conversation-item.md): 对话项在准入时落账、在拿到 root gate 时才进入对话；消息排在运行中的 turn 后面时 Journal 顺序与对话顺序不一致——投影判序号回退后停止更新，接管重建出的 history 顺序错误。决策：准入只落 `submission_accepted`，对话项与 `submission_applied` 在应用时落账；应用的落账与取消无关；接管时应用已准入未应用的消息。修正 ADR 0025 的用户入口记录语义。
102. [ADR 0102: 审计 Session 关闭时先协作取消在飞的 turn](decisions/0102-audit-mode-cooperative-shutdown.md): 释放时对 operation 直接 raw cancel，截断了意图落账与收敛之间的窗口，Journal 留下没有结果的意图而 Session 以 complete 终结。决策：先取消持有 root gate 的 turn 的 token、在 2 秒宽限期内等它自行退出，再根取消与 raw cancel；收敛期间 gate 不再放行新 turn，排队的消息在在飞 turn 收尾之后只应用不运行。
103. [ADR 0103: 接管时作废没有 checkpoint 的 LLM 请求](decisions/0103-audit-resume-abandons-interrupted-llm-requests.md): 进程死在 LLM 调用途中是最常见的崩溃时刻，而这种 Session 恰恰无法接管。决策：可收敛 thread 上没有 checkpoint 的请求落 `llm_request_abandoned` 视为已结算——回复没进过对话，作废不重复任何事情；那个 turn 到此为止不补跑；有 checkpoint 的不作废。
104. [ADR 0104: Journal Phase 5——Timeline 投影、脱敏、旧 transcript 导入与投影重建](decisions/0104-journal-timeline-redaction-import-rebuild.md): ADR 0025 的第五阶段。决策：Timeline 直接映射领域记录、按 `after_seq` 接力；脱敏按字段名而不是按内容，三种视图，事实源不动；旧 transcript 导入成历史标 `legacy_unverified` 的新 Session，损坏的行让导入失败，不认识的条目记下来；投影重建只补不改，分叉的只报告。
105. [ADR 0105: replay 模式——工具结果回放与整条 Session 重放](decisions/0105-replay-mode.md): ADR 0054 只回放了 LLM，工具仍要真的执行。决策：工具结果按（名字，有效参数）从 Journal 匹配、内核编排工具照常运行、派发句柄由（turn，调用 id）派生、按录制的提交序列驱动新 Engine 且分叉即停；重放不比较对话内容。
106. [ADR 0106: 超长文件按「方法体外置、类里按原名赋值」拆分](decisions/0106-file-size-split.md): engine.py 2092 行，协作者手法已用尽而白盒寻址名不能变。决策：方法定义为模块级函数、类里按原名赋值（绑定与打桩语义不变）；事件类按主题分文件并原名再导出；Journal 记录按层分文件；零行为变更。
107. [ADR 0107: 重放重现录制里的挂起与 writer 接管](decisions/0107-replay-reproduces-suspension-and-takeover.md): 真实 LLM 录制跨过「挂起 → 释放 → 接管 → Resume」时重放分叉。决策：等过人的调用结果带 `origin_llm_sample_id`；回放工具重现录制的待答请求而不是直接交回结果；`writer_takeover` 是录制序列的一步，`replay_session(reopen=)` 在此换新 Engine；不剔除缓存断点、不重现崩溃式接管。Amends #0105。

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
