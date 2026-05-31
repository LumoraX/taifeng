# 主循环

> §1.2 —— Submission / Event 双总线、并发调度、cancellation 父子化。

## 设计目标

把主循环抽象成 **「actor + 双向消息总线」**：
- 用户意图通过 `Submission` 入队
- 引擎输出通过 `EventMsg` 出队
- 二者通过 `AgentEngine` actor 解耦

参照：
- `codex` `Codex` actor + `Submission { id, op }` / `Event { msg }` 双 channel
- `claw-code` `ConversationRuntime` + `AssistantEvent` enum
- `openclaw` `AcpSessionManager.runTurn` + 单 actor 队列

不参照：
- `LangGraph` `StateGraph` —— 图调度范式
- `AutoGen v0.4` actor —— 多 agent 拓扑，过度设计

## 核心抽象

```python
# src/taifeng/loop/submission.py

from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class Submission:
    id: str
    op: "Op"

@dataclass(frozen=True)
class UserMessage:
    kind: Literal["user_message"] = "user_message"
    text: str
    attachments: list[dict] = ()

@dataclass(frozen=True)
class CancelTurn:
    kind: Literal["cancel_turn"] = "cancel_turn"
    submission_id: str

@dataclass(frozen=True)
class CompactNow:
    kind: Literal["compact_now"] = "compact_now"

Op = UserMessage | CancelTurn | CompactNow
```

```python
# src/taifeng/loop/event.py

@dataclass(frozen=True)
class EventMsg:
    submission_id: str
    msg: "Msg"

@dataclass(frozen=True)
class AssistantText:
    kind: Literal["assistant_text"] = "assistant_text"
    delta: str

@dataclass(frozen=True)
class ToolCallStarted:
    kind: Literal["tool_call_started"] = "tool_call_started"
    call_id: str
    name: str

@dataclass(frozen=True)
class ToolCallCompleted:
    kind: Literal["tool_call_completed"] = "tool_call_completed"
    call_id: str
    result: dict

@dataclass(frozen=True)
class TurnComplete:
    kind: Literal["turn_complete"] = "turn_complete"
    usage: dict

@dataclass(frozen=True)
class CompactionAttempted:
    kind: Literal["compaction_attempted"] = "compaction_attempted"
    phase: Literal["pre_turn", "mid_turn", "manual"]
    success: bool
    cache_invalidated: bool

Msg = AssistantText | ToolCallStarted | ToolCallCompleted | TurnComplete | CompactionAttempted | ...
```

```python
# src/taifeng/loop/engine.py

class AgentEngine:
    """主 actor：消费 Submission，产出 EventMsg。

    单进程内单 actor，串行处理 Submission 保证顺序；
    内部 TurnRunner 可派生异步任务做工具并行。

    按 ADR 0006 统一 Skill 模型：会话级注入 entry skill，不再有 AgentInstance。
    """
    submissions: asyncio.Queue[Submission]
    events: asyncio.Queue[EventMsg]

    def __init__(
        self,
        *,
        entry_skill: SkillDefinition,        # 必须 type=composite, entry=true
        skill_snapshot: SkillSnapshot,        # 共享，含可达子图
        tool_registry: ToolRegistry,
        model_client: ModelClient,
        store: MessageStore,
        compressors: list[CompressionStrategy],
        dispatch_policy: DispatchPolicy | None = None,
    ) -> None: ...

    async def run(self, cancel: CancellationToken) -> None:
        """actor 主循环。"""
        while not cancel.is_cancelled:
            sub = await self.submissions.get()
            await self._dispatch(sub, cancel.child(name=f"sub:{sub.id}"))

    async def submit(self, op: Op) -> str:
        """业务侧入队接口。返回 submission_id。"""

    async def subscribe(self) -> AsyncIterator[EventMsg]:
        """业务侧订阅出队接口。"""
```

## TurnRunner —— 单轮执行

参考 codex `run_turn`：

```python
async def run_turn(turn_ctx: TurnContext, cancel: CancellationToken) -> TurnOutcome:
    # 1. pre-sampling 压缩（允许动 head）
    for strategy in turn_ctx.compressors:
        if trig := strategy.should_trigger(turn_ctx.compression_context("pre_turn")):
            result = await strategy.compress(turn_ctx, injection=BEFORE_LAST_USER_MESSAGE)
            await turn_ctx.emit(CompactionAttempted(phase="pre_turn", ...))

    # 2. 注入 skill / tool / env 上下文（保留 cache anchor）
    prompt = build_prompt(
        history=turn_ctx.history,
        skills=turn_ctx.skill_snapshot,
        tools=turn_ctx.tool_specs,
    )

    # 3. 主循环：采样 → 处理事件 → 决定是否继续
    while not cancel.is_cancelled:
        async with turn_ctx.model_client.session(cancel=cancel) as session:
            async for event in session.stream(prompt):
                match event.kind:
                    case "text_delta":
                        await turn_ctx.emit(AssistantText(delta=event.text))
                    case "tool_call_done":
                        result = await turn_ctx.tool_runtime.dispatch(event, cancel)
                        prompt.append_function_call_output(result)
                        await turn_ctx.emit(ToolCallCompleted(...))
                    case "completed":
                        if not event.data["needs_follow_up"]:
                            return TurnOutcome.done(usage=event.usage)

        # 4. mid-turn 压缩（只能动 tail，保 cache anchor）
        if token_status(prompt).limit_reached:
            result = await compress_mid_turn(
                prompt,
                injection=InitialContextInjection.DO_NOT_INJECT,
            )
            if not result.success:
                return TurnOutcome.error(reason="context_overflow_unrecoverable")
            continue
```

## §1.6 Instructions 注入（instructions-injection）

参照：codex `core/src/agents_md.rs` —— 但只学"分层 + 合并"范式，不抄文件读取（taifeng 是 infra 库，没有 cwd / 文件假设）。

### 数据流（增量）

```
EnginePool.create(instruction_layers=[...])
  → pool 缓存 layers + 透传到 AgentEngine
  → engine.warmup_engine_scope()
      → resolver.resolve('engine', ctx) → 缓存到 engine 实例

每次 TurnRunner.run 启动前：
  → resolver.resolve(('engine','session','turn'), ctx)
      → engine scope 走缓存
      → session scope 走 (name, session, skill) 缓存
      → turn scope 看 cache_ttl
  → 失败 → InstructionFetchError → 发 turn_failed (fail-fast)
  → 成功 → 传 TurnRunner.instructions
  → build_api_request(instructions=[...])
  → render_system_prompt 输出 <system_instructions> 块（按 priority 升序）放在 <entry_skill> 前

UpdateInstructions Op：
  → resolver.replace_layer(name, new_source)
      → 找到 → 替换 + 立即失效该 layer 缓存 → instruction_updated 事件
      → 未知 → instruction_update_rejected(reason='unknown_layer') 事件
  → 下一个 turn 立即看到新文本
```

### 三档 scope 生命周期

| Scope | 解析时机 | 缓存生命周期 | 典型场景 |
|---|---|---|---|
| `engine` | `EnginePool.create` → `warmup_engine_scope()` | 进程生命周期 | 全局合规策略 / 产品级 system prompt |
| `session` | 每次 turn 启动前（含缓存） | engine 实例 + ttl | 租户级覆盖 / 人格设定 |
| `turn` | 每次 turn 启动前（ttl=0 必拉） | 单 turn / ttl | trace_id / 当前请求上下文 |

### 关键约束

- **R1 业务零侵入**：库内不内置 `FileInstructionSource` / `EnvInstructionSource` / `HttpInstructionSource`；业务侧实现 `InstructionSource` 协议自带 IO。
- **R2 cache 友好**：`InstructionLayer.cache_volatile` 字段显式标注是否破 prompt cache；`ResolvedInstruction.cache_volatile` 暴露给业务侧观测。
- **R3 可观测**：5 个新 EventMsg —— `instruction_fetched / cache_hit / updated / fetch_failed / update_rejected`，通过 TelemetrySink。
- **R4 可取消**：fetch 接收 `InstructionContext.cancel: CancellationToken`；turn cancel 时 fetch 抛 `asyncio.CancelledError`（透传，不包成 `InstructionFetchError`）。
- **R5 可 resume**：指令文本不入 JSONL 主存；engine 重建时业务侧重新提供 layers。

### HITL 边界（spec D6）

- **数据级权限**（"这个租户能不能读这段指令"）→ `InstructionSource.fetch` 内部完成，直接 raise 或返回 None
- **动作级权限**（"LLM 想跑这个 tool / call_skill"）→ `PermissionPolicy + PermissionPrompter`

两套机制**严格不重叠**。fetch 内**严禁**自行发起 HITL 询问机制（弹窗 / SSE / Slack bot）。详见 ADR 0007。

## 并发：RwLock 工具调度

参照 codex `ToolCallRuntime`：

```python
class ToolCallRuntime:
    """工具调用执行器。

    - parallel_safe=True 工具取读锁：可并行
    - parallel_safe=False 工具取写锁：独占执行
    """
    async def dispatch(
        self,
        call: ToolCall,
        cancel: CancellationToken,
    ) -> ToolResult:
        spec = self._registry.get(call.name)
        lock = self._rwlock
        async with (lock.read() if spec.parallel_safe else lock.write()):
            return await self._execute(call, spec, cancel)
```

**parallel_safe 字段约定**：
- 只读工具（read_file / grep / list_dir / read_skill）→ `True`
- 写工具（write_file / bash / mcp_action）→ `False`
- LLM 调用工具（如子 skill 派发）→ `False`（避免 token 压力突刺）

## 并发 fan-out：一批 tool call 的三段式派发

当 LLM 在**一条 assistant 消息**里吐出多个 tool call（含多个 `call_skill`），`TurnRunner._sample_once` 通过 `loop/tool_batch.py::dispatch_batch` 并发派发，而非逐个串行 `await`。并发度由构造期旋钮 `max_parallel_tool_calls` 控制（默认 `1` = 严格串行，等同历史行为，零回归；`>1` 开启并发）。

```
阶段 1（顺序、按发起序）：解析 arguments + 计算 parallel_safe + emit ToolCallStarted + 建 ToolCallRequest（暂不写历史）
阶段 2（并发）：emit ToolBatchDispatched{count, max_parallel}
             → asyncio.gather(_dispatch_one ...) + asyncio.Semaphore(max_parallel) 限流
             → 每个分支 cancel.child；RwLock 在 runtime.dispatch 内兜底（读类重叠 / 写类独占）；
               call_skill 跳锁 → 子 turn 真并行；完成即按真实顺序 emit ToolCallCompleted
阶段 3（顺序、按发起序）：以 (function_call, function_call_output) 配对追加 history + store
```

**硬不变量**：执行可并发，但历史**必须按发起序、以配对形式追加**——因 `prompt.py::history_to_api_messages` 是 1:1 保序转换，配对追加才能维持 provider 要求的 tool_use↔tool_result 结构；并发度=1 时与历史 transcript **字节级一致**。

**R 线落实**：R2（不触发压缩，子 turn 历史隔离，按序回填 → cache 稳定）/ R3（`ToolBatchDispatched` + 逐 call `ToolCallStarted/Completed`）/ R4（每分支 `cancel.child`，父取消级联）/ R5（按发起序回填 → JSONL 回放确定）。

"顺序" vs "并发"由两个天然机制表达，内核无需额外编排标志：① LLM 把相互独立的调用放进同一条消息 → 并发；有数据依赖时它自然一个一个发、等结果再发 → 串行。② 写类工具即便被并发派发，也在写锁上排队。

## 编排 turn：entry 声明了 orchestration（声明式编排 B）

当 entry skill 在 SKILL.md 声明了 `orchestration`（见 `skill-system.md`），`TurnRunner.run()` 检测到后**跳过 LLM 采样主循环**，改走 `loop/orchestration_exec.py::run_orchestrated_turn`（纯编排器：不采样 LLM，每个子 skill 内部仍各自走 LLM）：

```
emit orchestration_plan_resolved{skill_id, groups}
for step in spec.steps（段间串行）：
  段边界 raise_if_cancelled
  parallel/serial 叶子 → 合成 call_skill 批 → 复用上文的 dispatch_batch
                         （parallel 用 max_parallel_tool_calls，serial 强制 Semaphore(1)）
  when → 读上一步 child 结构化输出的布尔 flag → 选 then/else 叶子
         （flag 缺失/非布尔 → emit orchestration_condition_missing + 抛错硬失败）
  历史按发起序 (function_call, function_call_output) 配对回填（同三段式的硬不变量）
final_text = 最后一步各 child 输出 join；run() 照常 emit TurnCompleted{is_root}
```

**复用而非另起**：编排不写并发代码——并行组直接调上文的 `dispatch_batch`，仅把声明翻译成「一串 dispatch_batch 调用」。`OrchestrationConditionError` 不单独 catch，propagate 到 run() 通用 `except` → `TurnFailed`（end_reason=error）。R 线随 dispatch_batch 继承（R2/R4/R5），新增 R3 两个编排事件。

## Cancellation 父子化

参照 codex `tokio_util::CancellationToken`：

```python
# src/taifeng/loop/cancellation.py

class CancellationToken:
    """父子级联取消 token。

    - parent.cancel() → 所有 child 同步标记 cancelled
    - child.cancel() → 不影响 parent
    """
    def __init__(self, *, parent: "CancellationToken | None" = None) -> None: ...

    def child(self, name: str = "") -> "CancellationToken":
        """派生子 token，绑定父子关系。"""

    def cancel(self) -> None: ...

    @property
    def is_cancelled(self) -> bool: ...

    async def wait_cancelled(self) -> None:
        """供 anyio task group 监听。"""
```

**使用约定**：
- `AgentEngine.run(cancel)` 收到根 token，整体取消
- 每个 Submission 派生 `cancel.child(f"sub:{sub.id}")`
- 工具调用派生 `cancel.child(f"tool:{call.id}")`，配合 `anyio.fail_after(spec.timeout_sec)` 超时
- 子 agent 派发派生 `cancel.child(f"agent:{spawn_id}")`

## 与 codex 双 channel 的差异

| codex | Taifeng |
| --- | --- |
| Rust `tokio::mpsc` unbounded | Python `asyncio.Queue`（默认 bounded=1024，可配） |
| `Submission` 含 `id: SubmissionId` | 同 |
| `Event` 含 `id: EventId` + `submission_id` | 简化为 `EventMsg(submission_id, msg)` |
| `Op::*` 枚举 ~20 种 | 实现 9 种（UserMessage / CancelTurn / CompactNow / InjectSystemMessage / ThreadRollback / UpdateBudget / RefreshSnapshot / UpdateInstructions / Shutdown），见 `loop/submission.py` |

## 测试用例（M3 验收）

- [x] `AgentEngine.submit(UserMessage)` → 收到完整 `[AssistantText..., ToolCallCompleted?, TurnComplete]` 序列 —— `tests/loop/test_engine_e2e.py::test_pool_engine_basic_turn`
- [x] `CancelTurn` 发出后，主 turn 在 100ms 内停止；下游工具 task 同步取消 —— `tests/loop/test_cancellation.py`
- [x] 多 `parallel_safe=True` 工具并发执行（实测 N 个 read_file 总耗时 ≈ 单个）—— `tests/loop/test_concurrent_dispatch.py::test_concurrent_when_cap_gt_one`
- [x] `parallel_safe=False` 工具强制独占（实测 N 个 write_file 串行）—— `tests/loop/test_concurrent_dispatch.py::test_serial_when_cap_one`

## Hook + Permission lifecycle 完整图

完整的拦截位点（按调用顺序，ADR 0010 落地）：

```
Submission(UserMessage)
  ↓
pre_turn hook ────────────────── 业务侧改 user_text 起步控制
  ↓
[ TurnRunner 循环开始 ]
  ↓
pre_compact hook ─────────────── 压缩前快照（可观察 token_estimate）
  ↓
LLM stream → tool_call_done
  ↓
─── 普通工具（含 file_read / file_write / shell_exec）─────
   ↓
   pre_tool_use hook ─────────── 业务侧动态拦截
   ↓
   PermissionPolicy.check ────── 业务策略 + prompter（可选 timeout）
   ↓
   ToolCallRuntime.dispatch ──── 实际执行（read 锁 / write 锁）
   ↓
   post_tool_use hook ────────── 审计 + 业务侧后置动作
   ↓
─── call_skill（子 skill 派发）─────────────────────────
   ↓
   DispatchPolicy.check ──────── 结构性（白名单 / 深度 / 环 / unknown_skill）
   ↓ (失败立即 return；不进入后续)
   pre_skill_dispatch hook ───── 业务侧动态拦截（free tier 黑名单等）
   ↓ (deny → return + emit skill_dispatch_hook_denied)
   PermissionPolicy.check ────── 业务策略（租户级 child skill 配额）
   ↓ (deny → return + emit skill_dispatch_permission_denied)
   dispatcher.run_sub_skill ──── 嵌套 TurnRunner（递归本表）
   ↓
   post_skill_dispatch hook ──── 仅审计（run_audit_only；不能改 ToolResult）
   ↓
─── run_script（SKILL.md scripts 执行 · ADR 0009）─────────
   ↓
   skill / script_name / args_schema 校验 ────── unknown_skill / unknown_script / invalid_args
   ↓
   executor 查找 ──────────────── script_executors[descriptor.language]
   ↓ (找不到 → no_executor_for_language)
   pre_script_use hook ───────── 支持 metadata['args_override'] 改写 args
   ↓ (deny → ToolResult.error)
   PermissionPolicy.check(scope='script_exec') ──── deny → 不调 executor
   ↓
   executor.execute ──────────── subprocess + env 白名单 + process group + cancel + timeout
   ↓
   post_script_use hook ──────── 仅审计（hook 异常 / deny 不影响 ToolResult）
   ↓
   ToolResult + EventMsg ─────── 5 类 EngineLog：started / completed / failed / timeout / killed
   ↓
─── 循环回到 LLM stream，直到 completed 或 max_iterations ────
  ↓
[ TurnRunner 循环结束 ]
  ↓
TurnCompleted event
```

**关键约束（ADR 0010）**：

| 阶段 | hook 异常 / deny 是否影响主流程 |
|---|---|
| `pre_tool_use` | deny → ToolResult.error；异常 → ToolResult.error |
| `post_tool_use` | deny / 异常 → 只记日志，不影响 ToolResult |
| `pre_skill_dispatch` | deny → ToolResult.error + emit；异常 → ToolResult.error |
| `post_skill_dispatch` | **deny / 异常都被吞掉** —— `run_audit_only` 语义 |
| `pre_script_use` | deny → ToolResult.error；异常 → ToolResult.error；metadata['args_override'] 替换 args |
| `post_script_use` | **deny / 异常都被吞掉**（仅审计） |
| `permission_policy.check` | timeout → deny + emit `permission_prompt_timeout` |
- [x] `cancel.child()` 派生层级正确，父取消级联 —— `tests/loop/test_cancellation.py::test_child_cancel_propagates_from_parent`
