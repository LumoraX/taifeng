# Taifeng 能力总览矩阵

> **这份文档回答一个问题：taifeng 到底实现了什么、怎么接、去哪看。**
>
> 面向**使用 taifeng 的业务方**（如 qiuben）：扫一眼就能确定「这个功能 tf 有没有 / 落地了没 / 入口是哪个 API / 有没有现成示例」，不必翻源码或猜测。
>
> 三层文档分工：
> - **本文档**＝能力清单（能做什么 + 落地状态 + 入口 + 示例）——先看这里。
> - [`usage.md`](usage.md)＝怎么写代码接进来（安装 + 三层使用粒度 + 各能力代码骨架）。
> - [`configurable-knobs.md`](configurable-knobs.md)＝所有可配置参数的字段级清单。
> - [`architecture/capabilities/`](architecture/capabilities/README.md)＝每个能力的字段级稳定契约（数据结构 / 协议签名 / 事件 / 约束）。

## 状态图例

| 标记 | 含义 |
| --- | --- |
| ✅ | 已落地，有测试 + 可跑示例，可直接在生产接入 |
| 🧪 | 已落地，验证以 mock 为主（真实场景受限已如实记录，见契约文档） |

> 截至当前分支，下表能力**全部 ✅/🧪 已落地**。能力完善度进度（P0/P1/P2 清零）见 [`architecture/hermes-gap-roadmap.md`](architecture/hermes-gap-roadmap.md)，内核原语进度（K1–K4 已落、K5 待补）见 [`architecture/kernel-gap-analysis.md`](architecture/kernel-gap-analysis.md)。

---

## 一、Skill 系统（让 LLM 按需调度文档化技能）

| 能力 | 一句话 | 入口（API / 工具 / Op） | 示例 | 契约 |
| --- | --- | --- | --- | --- |
| **Skill = markdown** | skill 是 SKILL.md 文档，不是 function tool；LLM 是调度器 | `FilesystemSkillRegistry.load(dir)` / SKILL.md frontmatter | [basic/minimal_chat.py](../examples/basic/minimal_chat.py) | [skill-system.md](architecture/skill-system.md) |
| **read_skill 懒加载** | 子 skill 列表只给 id+description，LLM 按需 `read_skill` 拉 body（cache 友好） | `read_skill` 工具 | [read_skill_lazy/](../examples/read_skill_lazy/) | [skill-system.md](architecture/skill-system.md) |
| **call_skill 派发** | composite skill 递归派发子 skill，独立子 turn；深度 + 环检测 | `call_skill` 工具 / `DispatchPolicy` | [basic/composite_skill.py](../examples/basic/composite_skill.py) | [skill-dispatch.md](architecture/capabilities/skill-dispatch.md) ✅ |
| **声明式编排** | SKILL.md `orchestration:` 声明 parallel/serial/when，执行器不采样 LLM、确定性跑 | SKILL.md `orchestration` 字段 | [orchestration/demo.py](../examples/orchestration/demo.py) | [skill-orchestration.md](architecture/capabilities/skill-orchestration.md) ✅ |
| **并发 fan-out** | LLM 在一条消息里发多个 call_skill，`max_parallel_tool_calls>1` 时真并发 | `max_parallel_tool_calls` | [concurrent_fanout/](../examples/concurrent_fanout/) | [skill-dispatch.md](architecture/capabilities/skill-dispatch.md) ✅ |
| **分离式并发 spawn + join-barrier** | `spawn_skill` 立即返回句柄不阻塞、各 child 独立 HITL；`await_skills` 全终态自动聚合；**spawn 目标可为 entry**（与 call_skill 不同） | `spawn_skill` / `await_skills` / `join_skill` / `kill_skill` 工具 | [multi_expert_consult/](../examples/multi_expert_consult/) | [detached-spawn.md](architecture/capabilities/detached-spawn.md) ✅ |
| **嵌套专科错峰 HITL 续跑** | 被 spawn 的 composite 专科其**子 skill** HITL 挂起（CHILD_SKILL）→ `Resume` 走 `resume_spawn_nested` 续跑链恢复（真实 MDT 拓扑） | `Resume(thread_id=<spawn 子 thread>)` | [nested_hitl_demo.py](../examples/multi_expert_consult/nested_hitl_demo.py) | [detached-spawn.md](architecture/capabilities/detached-spawn.md) ✅ |
| **SKILL.md scripts 运行时** | SKILL.md `scripts:` 声明的脚本经 `run_script` 工具执行，权限 + hook 双门控 | `script_executors=` + `run_script` 工具 | [basic/skill_with_script.py](../examples/basic/skill_with_script.py) | [script-execution.md](architecture/capabilities/script-execution.md) ✅ |
| **SKILL.md 热更** | 文件监听，改 SKILL.md 不重启进程即生效 | `auto_watch_skills=True` | — | [skill-system.md](architecture/skill-system.md) |
| **运行时资格门控** | composite 子 skill 按 `requires` / `exposure` × 业务 `RuntimeCapabilities` 动态可见 | SKILL.md `requires` / `exposure` | [subagent_isolation/](../examples/subagent_isolation/) | [skill-system.md](architecture/skill-system.md) |

## 二、主循环 / 工具（turn 怎么跑、怎么调度、怎么取消）

| 能力 | 一句话 | 入口（API / 工具 / Op） | 示例 | 契约 |
| --- | --- | --- | --- | --- |
| **turn 主循环** | Submission/EventMsg 双总线 actor；turn 跑在独立 task 不阻塞主循环 | `AgentEngine` / `engine.submit` / `engine.subscribe` | [basic/minimal_chat.py](../examples/basic/minimal_chat.py) | [agent-loop.md](architecture/agent-loop.md) |
| **多 engine 复用 + resume** | 一进程多会话 engine 缓存；`resume_thread_id` 续接历史 | `EnginePool.create` / `get_or_create(resume_thread_id=)` | [persistence/](../examples/persistence/) | [agent-loop.md](architecture/agent-loop.md) |
| **取消 turn** | 协作式取消，父子 token 级联 | `CancelTurn` Op / `CancellationToken` | [web_ui/](../examples/web_ui/) | [agent-loop.md](architecture/agent-loop.md) |
| **mid-turn 输入注入（steering）** | turn 运行中投增量用户输入，下个迭代边界并入、不中止 turn；无活跃 turn 退化落历史 | `InjectUserInput` Op / `user_input_injected` 事件 | [web_ui/](../examples/web_ui/) | [midturn-input-steering.md](architecture/capabilities/midturn-input-steering.md) ✅ |
| **注入业务 system 消息** | turn 间投 system 注记，不影响活跃 turn | `InjectSystemMessage` Op | — | [agent-loop.md](architecture/agent-loop.md) |
| **turn 回访重跑（rewind）** | 把 root turn 拆成可寻址节点表，回退到任意 call_skill / 采样圈重跑 | `Rewind` Op（re_reason / retry_tool） | [turn_rewind/](../examples/turn_rewind/) | [turn-rewind.md](architecture/capabilities/turn-rewind.md) ✅ |
| **HITL 挂起 / 跨实例恢复** | 表单采集等待用户输入 → 释放实例 → 跨进程重建 → `Resume` 续跑 | `request_user_input` 工具 / `Resume` Op / `SuspensionResolver` | [suspend_resume/](../examples/suspend_resume/) | [suspend-resume.md](architecture/capabilities/suspend-resume.md) ✅ |
| **HITL 权限审批** | 动作级 HITL（skill 派发 / script / network），typed `PermissionRequest`，可超时 | `permission_policy=` / `PermissionPolicy` / `CallbackPrompter` | [permission/](../examples/permission/) · [selective_approval/](../examples/selective_approval/) | [permission-gate.md](architecture/capabilities/permission-gate.md) ✅ |
| **Hooks（claw-code 范式）** | 8 种 hook（Pre/Post ToolUse / SkillDispatch / ScriptUse、PreTurn、PreCompact）拦截关键路径 | `hooks=` / `HookRunner` | [hooks_showcase/](../examples/hooks_showcase/) | [hooks.md](architecture/capabilities/hooks.md) ✅ |
| **指令分层注入 + 热更** | engine/session/turn 三档 scope 注入 system 指令（codex AGENTS.md 角色），协议化、可热更 | `instruction_layers=` / `UpdateInstructions` Op / `InstructionSource` | [basic/instructions_basic.py](../examples/basic/instructions_basic.py) | [instructions-injection.md](architecture/capabilities/instructions-injection.md) ✅ |
| **内置工具集** | read_skill / call_skill / file_read / file_write / shell_exec / apply_patch / http_request / run_in_background / wait_for_task / run_script / request_user_input / spawn 四件套 | `extra_tools=` / `make_*_tool()` | [numeric_loop/](../examples/numeric_loop/) | [tool-builtins-extended.md](architecture/capabilities/tool-builtins-extended.md) ✅ |
| **后台任务** | LLM 发起长任务后台跑 + 轮询等待 | `run_in_background` / `wait_for_task` 工具 / `BackgroundTaskRegistry` | — | [tool-builtins-extended.md](architecture/capabilities/tool-builtins-extended.md) ✅ |

## 三、上下文压缩（溢出怎么压、cache 怎么保）

| 能力 | 一句话 | 入口（API / Op） | 示例 | 契约 |
| --- | --- | --- | --- | --- |
| **本地 budget 主动压缩** | 到达配置 `context_window` 上限即主动压缩，**不依赖 provider 报 overflow** | `budget=ContextBudget(...)` / `compressors=` | [compression_showcase/demo.py](../examples/compression_showcase/demo.py)（mock） | [context-compression.md](architecture/context-compression.md) |
| **Handoff 压缩** | codex 范式 LLM-to-LLM 接力摘要 + 质量审计 + 健康回滚 | `HandoffCompactionStrategy` | [overflow_demo.py](../examples/compression_showcase/overflow_demo.py)（mock）· [capability_matrix.py](../examples/real_llm/capability_matrix.py)（需 key） | [context-compression.md](architecture/context-compression.md) |
| **滑窗压缩** | 保尾 N 条 + 图像 marker | `SlidingWindowStrategy` | [compression_showcase/demo.py](../examples/compression_showcase/demo.py)（mock） | [context-compression.md](architecture/context-compression.md) |
| **overflow 有界自愈** | provider 判超长（本地估算偏低漏网窗口）→ 强制压缩一次 + 重采样一次，不硬失败丢 turn | 自动（配了 `compressors` 即生效）/ `provider_retry` 事件 | [overflow_demo.py](../examples/compression_showcase/overflow_demo.py)（mock） | [reactive-compaction-recovery.md](architecture/capabilities/reactive-compaction-recovery.md) 🧪 |
| **手动触发压缩** | 业务侧主动压一次 | `CompactNow` Op | — | [context-compression.md](architecture/context-compression.md) |
| **cache 友好契约** | 每次压缩显式标注 `cache_invalidated` / `anchor_preserved_until`；mid-turn 只动 tail | `CompressionResult` / `cache_break_detected` 事件 | [observability/](../examples/observability/) | [context-compression.md](architecture/context-compression.md) |
| **运行时改预算** | turn 间动态调 context_window | `UpdateBudget` Op | — | [configurable-knobs.md](configurable-knobs.md) |

## 四、LLM 客户端（多 provider 统一、事件流标准化）

| 能力 | 一句话 | 入口（API） | 示例 | 契约 |
| --- | --- | --- | --- | --- |
| **native 四件套 + LiteLLM 兜底** | OpenAICompat / Anthropic / Gemini / DeepSeek 零 SDK 直连 + LiteLLM 覆盖其余 | `model_client=` | [real_llm/e2e.py](../examples/real_llm/e2e.py) | [llm-provider-native.md](architecture/capabilities/llm-provider-native.md) ✅ |
| **统一 ResponseEvent 流** | 11 类 EventKind（text/tool_call/reasoning/prompt_cache/rate_limits/structured_output…）跨 provider 同构 | `ModelClient` 协议 / `ResponseEvent` | [observability/](../examples/observability/) | [llm-provider-native.md](architecture/capabilities/llm-provider-native.md) ✅ |
| **结构化输出** | 强类型输出 schema，provider 各自翻译 | `ResponseFormatSpec` / `structured_output` 事件 | — | [llm-structured-output.md](architecture/capabilities/llm-structured-output.md) ✅ |
| **重试 + 错误分类** | 指数退避 + 服务端 hint delay；错误分 11 桶 + 机读恢复配方 | `retry_async` / `LLMError` / `RecoveryPlan` | — | [llm-client.md](architecture/llm-client.md) |
| **精准 cache 计量** | 各 provider cache 字段直读（Anthropic / DeepSeek / Gemini） | `PromptCacheStats` / `TokenUsage` | [real_llm/e2e.py](../examples/real_llm/e2e.py) | [llm-provider-native.md](architecture/capabilities/llm-provider-native.md) ✅ |

## 五、持久化（会话怎么存、崩溃怎么 resume）

| 能力 | 一句话 | 入口（API） | 示例 | 契约 |
| --- | --- | --- | --- | --- |
| **JSONL 追加写主存** | 一 thread 一文件、首行 metadata、POSIX 原子追加、损坏行容错；source-of-truth | `JsonlMessageStore` / `MessageWriter` 协议 | [persistence/](../examples/persistence/) | [jsonl-transcript.md](architecture/capabilities/jsonl-transcript.md) ✅ |
| **SQLite 旁路索引（零配置）** | stdlib sqlite3 + WAL 索引，可从 JSONL 自愈重建 | 默认开启 / `rebuild_index()` | [persistence/](../examples/persistence/) | [thread-directory.md](architecture/capabilities/thread-directory.md) ✅ |
| **换后端（Redis / PG）** | 元数据 + list 查询走业务后端，主存 JSONL 不变 | `thread_directory=` / `ThreadDirectory` 协议 | [persistence/redis_thread_directory.py](../examples/persistence/redis_thread_directory.py) · [postgres_thread_directory.py](../examples/persistence/postgres_thread_directory.py) | [thread-directory.md](architecture/capabilities/thread-directory.md) ✅ |
| **投递审计 / ES / Kafka** | thread 生命周期事件 fire-and-forget 投递 | `index_hook=` / `IndexHook` 协议 | [observability/audit_index_hook.py](../examples/observability/audit_index_hook.py) | [index-hook.md](architecture/capabilities/index-hook.md) ✅ |
| **历史回滚** | 把 thread 截到某条之前 | `ThreadRollback` Op | — | [conversation.md](architecture/conversation.md) |

## 六、可观测 / 集成（接出去给前端、监控）

| 能力 | 一句话 | 入口（API） | 示例 | 契约 |
| --- | --- | --- | --- | --- |
| **EventMsg 事件总线** | 关键路径全打点；`subscribe(sub_id)` 单提交 / `subscribe_all()` 全局 | `engine.subscribe` / `subscribe_all` | [web_ui/](../examples/web_ui/) | [agent-loop.md](architecture/agent-loop.md) |
| **Console / JSONL sink** | 开箱即用的人读 / 机读落盘 sink | `attach_console_sink` / `attach_jsonl_sink` | [observability/](../examples/observability/) | [agent-loop.md](architecture/agent-loop.md) |
| **OTel 接入** | EventMsg → OTLP（Jaeger / Tempo / Grafana / ARMS），PII 过滤、span 嵌套、预建 counter | `OtelTelemetrySink` / `OtelSinkConfig`（`[telemetry-otel]` extra） | — | [telemetry-otel.md](architecture/capabilities/telemetry-otel.md) ✅ |
| **taifeng 作为 MCP server** | 把 skill turn 暴露成 MCP tool 给 Claude Code / Cursor，双向 elicitation | `McpStdioServer` / CLI `mcp serve` | [mcp_basic/](../examples/mcp_basic/) · [mcp_showcase/](../examples/mcp_showcase/) | [mcp-server.md](architecture/capabilities/mcp-server.md) ✅ |
| **taifeng 作为 MCP client** | 连外部 MCP server 自动注册其 tools | `McpStdioClient` | [mcp_basic/](../examples/mcp_basic/) · [mcp_hitl/](../examples/mcp_hitl/) | [mcp-server.md](architecture/capabilities/mcp-server.md) ✅ |
| **Web 实时面板（参考实现）** | FastAPI + SSE 浏览器实时看 agent 数据流，多 demo 切换 + 权限可视化 + resume | [web_ui/server.py](../examples/web_ui/) | [web_ui/](../examples/web_ui/) | — |

## 七、内核资源旋钮（把 tf 当 OS 微内核：准入 / 强制 / 流控 / 内存）

> 机制在内核、策略（上限值 / 后端）由业务注入（守 R1）。全部开箱即有安全默认，不注入也能跑。详见 [configurable-knobs.md §1.0](configurable-knobs.md)。

| 旋钮 | 维度 | 一句话 | 入口 | 示例 |
| --- | --- | --- | --- | --- |
| `max_concurrent_spawns` / `max_total_spawns` | K1 广度准入 | 并发在飞 spawn 上限（防 fork-bomb）；HITL 挂起不占额度 | `EnginePool.create(max_concurrent_spawns=)` | [kernel_knobs/](../examples/kernel_knobs/) |
| `max_session_tokens` | K2 资源强制 | 会话累计 token 硬天花板（OOM-killer），触顶拒新 turn / 停采样 | `EnginePool.create(max_session_tokens=)` | [kernel_knobs/](../examples/kernel_knobs/) |
| `memory_store` | K3 内存层级 | 长期记忆 swap/缺页（prefetch / writeback / on_pre_evict / on_session_end），后端业务自接 | `memory_store=` / `MemoryStore` 协议 | [memory/](../examples/memory/) |
| `submission_queue_size` / `event_queue_size` | K4 流控 | 入站 backpressure / 出站事件队列 | `EnginePool.create(...)` | [kernel_knobs/](../examples/kernel_knobs/) |

---

## 专题：多轨并发可观测（前端怎么展示「多个 skill 并发执行」）

> 典型场景：多专家会诊——编排 skill 在一个 turn 内并发 `spawn_skill` 起多个专家（高血压 / 肺结节 …），各专家独立跑、错峰 HITL、最后 join-barrier 聚合成会诊报告。**前端要把每条并发轨的流式输出、工具调用、HITL 状态各自分开渲染。**
>
> 内核已提供全部所需信息，**不需要业务方改内核**。归轨键就是 **`EventMsg.submission_id`**。

**分轨映射：**

| 轨道 | 该轨事件的 `submission_id` | 映射来源 |
| --- | --- | --- |
| 编排入口 turn | 用户提交返回的 `sub_id` | `engine.submit(UserMessage)` 返回值 |
| 每个并发专家 child | = 该专家的 `child_thread_id` | `spawn_started.data = {handle_id, skill_id, child_thread_id}` |
| 联合会诊聚合 turn | = `then_thread_id` | `join_barrier_fired.data = {barrier_id, then_thread_id}` |

**为什么成立（源码锚点）：**
- `_build_child_runner` 用 `submission_id=child_thread_id` 构造每个并发子 runner（[`src/taifeng/loop/engine.py`](../src/taifeng/loop/engine.py)）；
- 每条事件都封 `EventMsg(submission_id=self.submission_id, ...)`（[`src/taifeng/loop/turn.py`](../src/taifeng/loop/turn.py)）。
- ⇒ `turn_started` / `assistant_text`(增量) / `tool_call_started` / `tool_call_completed` / `skill_dispatched` 都带所属轨道的 `submission_id`，**并发专家的流式输出天然可分开**。

**前端分轨投影配方（伪码）：**

```python
tracks = {}                         # submission_id -> 轨道视图
async for ev in engine.subscribe_all():
    m, sid = ev.msg, ev.submission_id
    if m.kind == "spawn_started":   # 注册一条专家轨：child_thread_id 即该轨的 submission_id
        tracks[m.data["child_thread_id"]] = {"skill": m.data["skill_id"], "state": "running"}
    elif m.kind == "spawn_suspended":
        tracks[m.data["thread_id"]]["state"] = "hitl_waiting"   # 该轨进入错峰 HITL
    elif m.kind == "join_barrier_fired":
        tracks[m.data["then_thread_id"]] = {"skill": "consultation", "state": "running"}
    # 流式正文按 sid 归轨渲染
    elif m.kind in ("assistant_text", "tool_call_started", "tool_call_completed"):
        tracks.setdefault(sid, {"skill": "?", "state": "running"})
        render_into_track(sid, m)   # 高血压轨 / 肺结节轨 / 会诊轨各自更新
```

**完整事件清单：** 7 类 spawn/join 生命周期事件 `spawn_started / spawn_suspended / spawn_completed / spawn_failed / spawn_cancelled / join_barrier_registered / join_barrier_fired`（字段见 [`src/taifeng/loop/event.py`](../src/taifeng/loop/event.py)）。端到端时间线 demo：[multi_expert_consult/](../examples/multi_expert_consult/)；浏览器多轨实时渲染参考实现：[web_ui/](../examples/web_ui/)（含 `multi_expert_consult` 交互 demo）。契约：[detached-spawn.md](architecture/capabilities/detached-spawn.md)。

> 业务侧（如 qiuben）在 SSE 层把 `submission_id` 改名为 `track_id` 做多轨投影即可——内核不感知业务轨道语义（R1）。

---

## 接入入口速查

### EnginePool.create 全参数

签名见 [`src/taifeng/loop/pool.py`](../src/taifeng/loop/pool.py) `create()`，字段级说明见 [configurable-knobs.md §1](configurable-knobs.md)。最小必填：

```python
pool = await taifeng.EnginePool.create(
    skills_dir="./skills",        # SKILL.md 根目录（必填）
    storage_dir="./data",         # JSONL 主存 + SQLite 索引（与旧名 threads_dir 等价，二选一必填）
    model_client=client,          # ModelClient 实现（必填）
)
```

### Op 全集（运行时通过 `engine.submit(...)` 投递，共 13 种）

| Op | 作用 |
| --- | --- |
| `UserMessage` | 发起 / 续接一个 turn |
| `InjectUserInput` | turn 运行中注入增量用户输入（steering） |
| `InjectSystemMessage` | 注入 system 注记（不影响活跃 turn） |
| `CancelTurn` | 取消运行中的 turn |
| `CompactNow` | 手动触发一次压缩 |
| `Resume` | HITL 挂起后续跑 |
| `Rewind` | 回退到可寻址节点重跑 |
| `SendToPeer` | 谱系内 peer 点对点投递（与 `send_message` 工具同路径） |
| `ThreadRollback` | 历史截断回滚 |
| `UpdateBudget` | 运行时改 context 预算 |
| `UpdateInstructions` | 热更某层 instruction |
| `RefreshSnapshot` | 刷新 skill 快照 |
| `Shutdown` | 关闭 engine |

### 内置工具全集（`make_*_tool()`，按需 `extra_tools=` 注册）

`read_skill` · `call_skill` · `file_read` · `file_write` · `shell_exec` · `apply_patch` · `http_request` · `run_in_background` · `wait_for_task` · `run_script` · `request_user_input` · `spawn_skill` · `await_skills` · `join_skill` · `kill_skill` · `send_message` · `wait_peer` · `todo_write`

### 公共 API 符号

全部可 import 符号见 [`src/taifeng/__init__.py`](../src/taifeng/__init__.py) 的 `__all__`。

---

## 验证状态

- 全量回归：`PYTHONPATH=src uv run pytest tests/`（CI 内全 mock，禁真实 API）。
- 真实 LLM 能力矩阵：[`examples/real_llm/capability_matrix.py`](../examples/real_llm/capability_matrix.py) —— 10 个能力场景逐个真实 key 跑测 + R3 可观测完整性审计（每个事件 kind 是否都有专用渲染、无静默吞没）。
- 各能力的边界与受限项（如 overflow 真实触发不划算、单 mock 覆盖）在对应契约文档 §测试 / §能力边界中如实记录，不夸大。
