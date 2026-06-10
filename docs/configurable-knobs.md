# 业务侧可配置参数清单

> 所有配置都通过 **构造时参数** 或 **运行时 Op** 注入。引擎不读环境变量、不读全局配置文件。
> 参照范围：codex `Op::*` + claw-code `RuntimeFeatureConfig` + claude-code 用户配置项。

## 1. 构造时参数（EnginePool.create / AgentEngine.__init__）

| 参数 | 默认值 | 说明 | 对标 |
| --- | --- | --- | --- |
| `skills_dir` | (必填) | SKILL.md 加载根目录 | — |
| `threads_dir` | (必填) | JSONL 持久化目录 | — |
| `model_client` | (必填) | `ModelClient` 实现（OpenAICompat / Anthropic / Gemini / DeepSeek / LiteLLM / Mock 任选其一，详见 §1.3） | codex `ModelClient` |
| `extra_tools` | `[]` | 额外注册的 `ToolSpec` | claw-code custom tools |
| `compressors` | `[Handoff, Sliding]` | `CompressionStrategy` 列表；空列表禁用压缩。内置三档谱系：`SurgicalTrimStrategy`（手术刀就地剪枝，推荐最高优先级，参数见 [capabilities/compaction-surgical-trim.md](architecture/capabilities/compaction-surgical-trim.md)）/ `HandoffCompactionStrategy`（LLM 摘要）/ `SlidingWindowStrategy`（滑窗兜底） | codex `CompactionConfig` |
| `budget` | `ContextBudget()` 默认 200k window / 0.85 soft / 0.95 hard / 4 tail | 整体 token 预算 | claw-code `auto_compaction_input_tokens_threshold` |
| `dispatch_policy` | `DispatchPolicy()` | call_skill 派发策略 | — |
| `hooks` | `None` | `HookRunner`（PreToolUse/PostToolUse/PreCompact） | claw-code hooks |
| `max_iterations` | `32` | 单 turn 内 LLM ↔ tool 最大循环 | claw-code `max_iterations` |
| **`denial_breaker_config`** | `None` | turn 内连续拒绝断路器（`DenialBreakerConfig{max_consecutive_denials, max_recent_denials, window_size}`）。None=不启用零变化；越阈值 emit `denial_circuit_open` + turn 以同名 end_reason 提前终止。详见 [capabilities/turn-resource-guards.md](architecture/capabilities/turn-resource-guards.md) | codex `guardian` 断路器 |
| **`max_parallel_tool_calls`** | `1` | 单 turn 内**一批** tool call 的最大并发数（一条 assistant 消息里的多个 tool call / call_skill）。默认 `1` = 严格串行（等同历史行为，零回归）；设 `>1` 开启并发 fan-out。安全由 `ToolCallRuntime` 的 RwLock 兜底（parallel_safe 读类重叠、写类独占；call_skill 跳锁真并行）。结果按发起序配对回填历史，cache / resume 不受影响。声明式编排（SKILL.md `orchestration`）的 parallel 段同样受此旋钮限流，serial 段无论该值多大都强制串行。详见 `docs/architecture/agent-loop.md` 并发派发段 + 编排 turn 段 | codex `tools/parallel.rs` |
| `auto_watch_skills` | `False` | 启用 SKILL.md 文件监听热更 | — |
| `watch_poll_interval_seconds` | `1.0` | 文件监听轮询间隔 | — |
| **`instruction_layers`** | `None` | `list[InstructionLayer]`；业务侧注入的系统指令层（类似 codex 的 AGENTS.md 角色，但走协议而非读文件）。详见下方 §1.1 | codex `~/.codex/AGENTS.md` |
| **`script_executors`** | `{}` | `dict[ScriptLanguage, ScriptExecutor]`；run_script 工具按 `descriptor.language` 在此查执行器。src 默认提供 `ShellScriptExecutor / PythonScriptExecutor`；未注入对应 language → `no_executor_for_language` 错误。详见 ADR 0009 / §1.2 | — |
| **`event_queue_size`** | `1024` | 每个 `subscribe()` / `subscribe_all()` 创建的事件队列 maxsize。慢消费者环境调大可降丢事件；高频实时场景调小以早 backpressure（QueueFull 时 emit 走 logger.warning）。修复自 change `config-consistency-fixes` C2 —— 之前 kwarg 被吞，硬编码 1024 | claw-code `EventChannelConfig` |

#### §1.0 内核资源/内存旋钮（K1–K4，业务可注入）

> 这五个旋钮是把 taifeng 当 OS 微内核时的"资源准入 / 强制 / 流控 / 内存层级"配置。**机制在内核，上限值/后端是策略，由业务注入**（守 R1）。全部开箱即有安全默认；不注入也能跑。

| 参数 | 默认值 | 内核维度 | 说明 | 对标 |
| --- | --- | --- | --- | --- |
| **`max_concurrent_spawns`** | `16` | K1 广度准入 | 单 engine 内并发**在飞**（running）detached spawn 的上限。防 fork-bomb。超限时内核把 `SpawnLimitError` 转成 `SkillSpawnRejected` **事件** + `ToolResult.error`。⚠️ **nuance**：只统计 `runner.run()` in-flight 的 spawn；HITL 挂起的 spawn **退栈即释放 slot**（suspended 不计并发），resume 时重新占用——HITL 等待期不消耗并发额度。详见 [detached-spawn 契约](architecture/capabilities/detached-spawn.md) §K1 | codex `agent/registry.rs::reserve_spawn_slot` |
| **`max_total_spawns`** | `1000` | K1 广度准入 | 单 engine 生命周期内累计 spawn 上限（单调递增，不回收；兜底 runaway 循环），与并发上限独立 | codex 同上 |
| **`max_session_tokens`** | `None` | K2 资源强制 | 会话累计 token 硬天花板（OOM-killer）。`None`=不强制（只告警）。设值后：跨 turn 累计触顶 → pre-turn 拒新 turn（`turn_refused`）；turn 内触顶且有后续 tool call → `ResourceLimitExceeded(turn_aborted)` 事件 + 停采样 | codex `UsageLimitReached` |
| **`memory_store`** | `None` | K3 内存层级 | `MemoryStore` 协议实现（长期记忆 swap/缺页接口）：`prefetch` 换入注入 prompt 尾部 / `writeback` 脏页写回 / `on_pre_evict` 换出前抢救 digest / `on_session_end` teardown。`None`=无内存层级（=`NullMemoryStore`）。全 best-effort（钩子异常不打断 turn）。后端（向量库/KV/RAG）是 **userspace**，业务自接。协议见 `src/taifeng/context/memory.py` | hermes `memory_provider.py`（剔业务字段） |
| **`submission_queue_size`** | `256` | K4 入站流控 | 入站 submission 队列 maxsize（bounded backpressure）。满则 `submit()` 在 `put` 处 await（业务侧自然阻塞），不丢提交。与 `event_queue_size`（出站）成对 | codex bounded 入站队列 |

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
)
```

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

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | (必填) | Bearer token |
| `base_url` | (必填) | API 根地址（不含 `/v1/chat/completions` 路径） |
| `model` | `"gpt-4o-mini"` | 默认模型名 |
| `extra_headers` | `None` | 额外 header（如自部署网关 token） |
| `timeout_seconds` | `300.0` | httpx 超时 |

#### `AnthropicClient`（Anthropic Claude API）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | (必填) | ANTHROPIC_API_KEY |
| `model` | `"claude-haiku-4-5-20251001"` | 默认模型名（不带 LiteLLM 前缀） |
| `base_url` | `"https://api.anthropic.com"` | API 根地址 |
| `anthropic_version` | `"2023-06-01"` | API 版本 header |
| `extra_headers` | `None` | 额外 header（third-party gateway 用） |
| `timeout_seconds` | `300.0` | httpx 超时 |

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
| **`UpdateBudget`** | 运行时调整 ContextBudget | `context_window` / `soft_limit_ratio` / `hard_limit_ratio` / `preserve_tail_messages` |
| **`RefreshSnapshot`** | 拉最新 SkillSnapshot | — |
| **`UpdateInstructions`** | 热更指定 layer 的 source；缓存立即失效；下个 turn 生效 | `layer_name`, `new_source` (str 或 `InstructionSource`) |
| **`Resume`** | 续跑一个挂起的 thread（`end_reason="suspended"` 的 turn）。配对 `resolutions` → 补齐 history-gap → 续采样。详见 §8 与 [suspend-resume 契约](architecture/capabilities/suspend-resume.md) | `thread_id`, `resolutions: {request_id: payload}` |
| **`Rewind`** | 回退到 root turn 内某回访节点并重推。`re_reason` 截到节点采样前重采样（LLM 重决下游）；`retry_tool`（仅 dispatch 节点）保留 assistant 的 function_call、只重跑该工具。配 `engine.rewind_nodes()` 取节点。详见 [turn-rewind 契约](architecture/capabilities/turn-rewind.md) + ADR 0014 | `node_id`, `mode ∈ {re_reason, retry_tool}`, `new_args?` |
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
engine.has_live_spawns()       # bool：True iff 有 running/suspended 句柄（keepalive 引用计数）

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
#   "session_tokens", "max_session_tokens",        # K2 资源
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
    timeout_seconds=60.0,        # 单次调用超时
)
```

## 6. PermissionPolicy（可选）

```python
PermissionPolicy(
    rules=[
        PermissionRule(scope="file_read", target_pattern="re:^/data/", mode="allow"),
        PermissionRule(scope="shell_exec", target_pattern="re:rm ", mode="deny"),
        # ADR 0010：skill_dispatch scope 现在受 PermissionPolicy 控制
        PermissionRule(
            scope="skill_dispatch",
            target_pattern="oncology-deep-analysis",
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
    event_kind: str,        # 当前仅 "permission_prompt_timeout"
    payload: dict[str, Any], # scope / target / timeout_seconds / call_chain / thread_id / submission_id
) -> None:
    # 推荐：写监控系统 + 告警；超时频繁 = 业务 prompter 实现有 bug 或用户体验差
    ...
```

### Hook lifecycle 完整集

`HookKind` 共 8 种，对应 8 个调用点（自 `hook-wiring-pre-compact-pre-turn` 起所有 kind 均已挂接，无 dead code）：

| Hook kind | 触发位置 | deny / 异常影响 |
|---|---|---|
| `pre_turn` | `AgentEngine._run_turn_for`：user_message 已持久化 + instruction resolve 完成后，TurnRunner 实例化前 | deny → emit `pre_turn_hook_denied` + `turn_failed`；TurnRunner 不实例化；`_turn_index` 仍 +1 |
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
    retryable_kinds=frozenset({"rate_limit", "transient_network", "server_error"}),
)
```

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
| **`request_timeout_seconds`** | `60.0` | 单条 JSON-RPC 请求超时；`None` = 关闭 client 层超时，完全由调用方控制 |

**双层 timeout 协同**：`register_mcp_tools_async(..., timeout_seconds=N)` 在 tool 调用外层包了一层 `wait_for(..., timeout=N)`。修复前内层硬编码 60s，外层调大会被静默截断；现在 `McpStdioClient(request_timeout_seconds=N)` 与外层匹配，**或** 显式设 `None` 把唯一 timeout 责任交给外层。

```python
# 推荐：内外层 timeout 一致
client = await McpStdioClient.spawn(cmd, request_timeout_seconds=120)
specs = await register_mcp_tools_async(client, registry, timeout_seconds=120)

# 或：内层无限，外层独控
client = await McpStdioClient.spawn(cmd, request_timeout_seconds=None)
specs = await register_mcp_tools_async(client, registry, timeout_seconds=120)
```

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
| `initialize` | 协议版本 `2024-11-05` + serverInfo + capabilities (`tools` + `resources`) |
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

registry = BackgroundTaskRegistry(max_concurrent=8)

extra_tools = [
    make_run_in_background_tool(registry=registry, policy=my_policy),
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
from taifeng import make_http_request_tool

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

**K1 配额 nuance**：`max_concurrent_spawns` 只统计 running（in-flight runner）的 spawn；suspended spawn 释放 slot，不计入并发额度（见 §1.0）。

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
| mock | `MockTurn(structured=...)` 字段配对回放 |

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
