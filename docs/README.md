# Taifeng Documentation Index

> Entry point for Taifeng research notes, architecture documents, capability contracts, and ADRs.

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
