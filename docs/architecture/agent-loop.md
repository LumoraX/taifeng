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

## 事件总线审计可观测（audit-observability 契约）

层1 在不破坏 EventMsg「可丢 / 不阻塞主 actor」语义（R4）前提下补三件事，契约见 [audit-observability](capabilities/audit-observability.md)：

- **全局序号 `seq`**：每条 `EventMsg` 带单调 `seq`，在 `engine._emit` 入口同步分配（无 await 让出点 → 原子不重不漏）。作用域单 engine（= 单 session）；落库主键用 `(session_id, seq)`，`session_id` 由订阅方在 sink 边界提供、**不**盖在事件上（`engine.session_id` 只读属性）。全局 seq 连续性自检**仅** firehose（`subscribe_all`）成立。
- **per-subscriber 投递序号 `delivery_seq`**：`subscribe_all_envelopes` / `subscribe_envelopes` 产出 `DeliveredEvent {event, delivery_seq}`；每订阅各自从 0 连续，队列满丢弃也烧号 → 收方跳号 = 它自己漏了（过滤订阅亦可精确自检）。`subscribe_all` / `subscribe` 仍向后兼容产出裸 `EventMsg`。
- **队列有界大容量 + 高/低水位告警**：`event_queue_size` 默认 `65536`（有界 ⇒ 内存可预测、绝不 OOM；`<=0` 为无界 opt-in 自负 OOM）。`qsize` 上穿高水位（75%）打 `logger.warning`，回落低水位（50%）才重新武装（迟滞）+ `event_warn_cooldown_sec` 限频；告警走 logger 不走事件（防自放大）。
- **LLM request 留痕**：`enable_request_capture`（默认关）开启后，`turn.py` 在 build 后发送前 emit `LlmRequestRecorded`（retry/重建各一条）；文字正文仍敏感，图片 base64/Data URL 则在事件生成前结构化替换为 `content_redacted` 描述。`OtelTelemetrySink` 按 kind 整条跳过不外发。

> 「可靠 fail-stop 审计真相源」是独立的层2 课题（留 ADR 0019），不改造 EventMsg emit 路径。

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

### 图片 admission 与 Responses durable gate

`EnginePool.create(image_input_policy=..., input_cost_estimator=...)` 把业务策略传播到 root、call_skill、detached spawn、child Resume 与 manual compaction runner。`UserMessage.attachments` 在 actor enqueue 和 conversation append 之前完成 count、base64、decoded bytes、digest、MIME、dimensions/frame 检查；默认禁用或 client capability 不匹配时不留下脏历史。

prompt 层只走一条 history 转换路径：普通 Chat 由 canonical items 派生 messages；Responses 保留 provider state、function call/output 的严格顺序。Responses attempt 必须观察到恰好一次 `normalized_output → completed`，随后按 `llm_sample_id` 原子提交 reasoning/assistant/function-call group；commit ack 之后才允许 tool dispatch。工具结果携带 `origin_llm_sample_id`，下一轮和冷恢复都按 sample closure 重放。

### 失败处置数据流（failure-suspension-policy）

turn 内两类失败点的「挂起 vs 终态」由注入的 `FailureDispositionPolicy`（`loop/failure_policy.py`）裁决，turn 层只构造 `FailureContext` 不做判断：

```
_sample_once except LLMError(重试耗尽)
  → policy.decide(origin="llm_error", failure_class, retryable, ...)
       ├─ SUSPEND  → SuspendSignal(SYSTEM_RETRY) → 既有挂起落盘
       └─ TERMINAL → 上抛 → TurnFailed(硬失败,带 G3 recovery 配方)

run() 四个护栏 break 点(max_iterations / resource_limit_exceeded / denial_circuit_open / doom_loop_circuit_open)
  → policy.decide(origin="guard_trip", end_reason, ...)
       ├─ SUSPEND  → SuspendSignal(RESOURCE_LIMIT, detail={end_reason, 护栏快照})
       └─ TERMINAL → 既有 end_reason break(默认 policy 恒走此路,零变化)
```

内置 `ConservativeFailurePolicy`（默认 = 历史行为）与 `SuspendByDefaultPolicy`（失败一律挂起等人裁决）；注入链 `EnginePool.create(failure_policy=...)` → engine → 全部 TurnRunner 构造点，子 runner（call_skill / spawn）继承。resume 语义与 spawn 链交互见 [capabilities/suspend-resume.md](capabilities/suspend-resume.md)。

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

**可见才可执行（tool-whitelist）**：`dispatch_batch` 必填 `visible_tools`——本轮实际注入请求的工具名集（由 `SkillDefinition.visible_tool_names()` ∩ registry 派生，与请求严格同源）。LLM 幻觉调用集合外的工具在 PreToolUse hook **之前**被拒：is_error 的 `function_call_output` 核销 call_id（`tool_not_offered`），不消耗 hook / 权限 / 锁，turn 不中断；engine 的 resume 重放与业务直发 Op 不经此层（豁免）。详见 `capabilities/tool-whitelist.md`。

```
阶段 1（顺序、按发起序）：解析 arguments + 计算 parallel_safe + emit ToolCallStarted + 建 ToolCallRequest（暂不写历史）
阶段 2（并发）：emit ToolBatchDispatched{count, max_parallel}
             → asyncio.gather(_dispatch_one ...) + asyncio.Semaphore(max_parallel) 限流
             → 每个分支 cancel.child；RwLock 在 runtime.dispatch 内兜底（读类重叠 / 写类独占）；
               call_skill 跳锁 → 子 turn 真并行；完成即按真实顺序 emit ToolCallCompleted
阶段 3（顺序、按发起序）：以 (function_call, function_call_output) 配对追加 history + store
```

**硬不变量**：执行可并发，但历史**必须按发起序、以配对形式追加**——`prompt.py::history_to_api_messages` 是保序的**同轮合并**转换（一次采样的 assistant_message + 全部 function_call 归并回一条 assistant 消息，fco 照序输出 tool 消息；thinking 模型 reasoning 附在合并消息上，见 llm-client 篇 reasoning 回传节），配对追加才能维持 provider 要求的 tool_use↔tool_result 结构；并发度=1 时与历史 transcript **字节级一致**。

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

**子挂起传递与重放**（orchestration-suspension-propagation）：批内子 skill 挂起时，完成子照常配对回填、挂起子只留悬空 fc（占位文本不入史），整批 pending 抛 `_BatchSuspend` → 编排 turn 以 suspended 终结（CHILD_SKILL 上浮，走既有嵌套 resume 链）。resume 重入按确定性 call_id（`orch_{entry}_{step}_{sid}_{idx}`）重放 history 中已配对的子——已完成段零派发跳过、when 判定由重放输出重建；`tool_batch_dispatched.count` 只计实际派发数。契约见 [capabilities/skill-orchestration.md](capabilities/skill-orchestration.md)。

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
| `Op::*` 枚举 ~20 种 | 实现 11 种（UserMessage / CancelTurn / CompactNow / InjectSystemMessage / ThreadRollback / UpdateBudget / RefreshSnapshot / UpdateInstructions / Resume / Rewind / Shutdown），见 `loop/submission.py` |

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
TurnCompleted event（runner 内 emit）
  ↓
[ engine 状态回写：history / cache_anchor / rewind 节点表 / 指纹 / token ]
  ↓
post_turn hook ───────────────── 仅审计（root turn 真终态触发；suspended/cancelled 跳过；
                                  本 turn 收尾的同步一步=回写之后触发；自我 review / 记忆固化落脚点。
                                  注:引擎不串行化相邻 turn——跨 turn 顺序须宿主等 post_turn_hook_fired
                                  再提交下一轮,而非等 turn_completed）
  ↓ (emit post_turn_hook_fired)
```

**关键约束（ADR 0010）**：

| 阶段 | hook 异常 / deny 是否影响主流程 |
|---|---|
| `pre_tool_use` | deny → ToolResult.error；异常 → ToolResult.error |
| `post_tool_use` | deny / 异常 → 只记日志，不影响 ToolResult |
| `pre_skill_dispatch` | deny → ToolResult.error + emit；异常 → ToolResult.error |
| `post_skill_dispatch` | **deny / 异常都被吞掉** —— `run_audit_only` 语义 |
| `post_turn` | **deny / 异常都被吞掉**（仅审计；turn 已终结无可改）—— `run_audit_only` 语义 |
| `pre_script_use` | deny → ToolResult.error；异常 → ToolResult.error；metadata['args_override'] 替换 args |
| `post_script_use` | **deny / 异常都被吞掉**（仅审计） |
| `permission_policy.check` | timeout → deny + emit `permission_prompt_timeout` |

## turn-rewind：turn 内任意节点可寻址 rewind（turn-rewind 契约）

一次 root turn 的执行轨迹被拆成一张**可寻址回访节点表**，业务侧可对任意节点直接 retry——既能重跑某一次 LLM loop 采样，也能重跑某一次工具 / `call_skill` 派发。子 skill 全程 `entry: false`，**绕开 entry/call_skill 互斥**（不放松 `cannot_call_entry_skill`）。详见 [turn-rewind 契约](capabilities/turn-rewind.md) 与 ADR 0014、ADR 0016。

### 节点 ID 格式

node_id 为 turn 限定格式：`t{k}:it{n}`（iteration）/ `t{k}:disp{m}`（dispatch），其中 `k` = 到本 turn 为止的累积 `user_message` 数（1-based），由 `count_turns(history)` helper 从逻辑 history 算出，**不依赖 engine._turn_index**（后者冷加载不回填）。`RewindCheckpoint` 新增 `turn_index: int`（= k 值）。

### 节点表产生（`derive_rewind_log` 为唯一产出方）

节点表统一由 `derive_rewind_log(history)` 纯函数（`loop/rewind.py`）推导，是**冷加载 / 热 turn 结束 / CompactNow 三处的唯一产出方**：

- **冷加载（engine `__init__`）**：
  ```
  self._history = reconstruct_logical_history(list(initial_history))  # 先重建逻辑 history
  self._rewind_checkpoints = derive_rewind_log(self._history)         # 再推导全 turn 节点表
  ```
- **热 turn 结束 / CompactNow 回写**：先 `self._history = list(runner.history_buffer)`，再 `self._rewind_checkpoints = derive_rewind_log(self._history)`（重算覆写，不 extend）。

热 turn 执行中的 live `RewindLog`（`turn.py`）仅用于 emit `rewind_checkpoint_recorded` 事件（R3），turn 结束后以 `derive_rewind_log` 重算覆写，二者由 `count_turns` 同一 helper 保证 node_id 一致（由奇偶校验测试锁死）。

### 热 turn 记录流程（`loop/rewind.py` + `turn.py`，仅 root turn）

每圈 `_sample_once` 采样前记一个 `iteration` 节点；每次工具 / `call_skill` 派发的 `function_call` 追加处记一个 `dispatch` 节点（两切点：`history_len` = 所属 iteration 采样前 = re_reason 切点；`inner_history_len` = fc 后 / fco 前 = retry_tool 切点）。`RewindCheckpoint` 只记 history 下标（append-only 不破，R5）；每记一个 emit `rewind_checkpoint_recorded`。`turn_root` 收敛进首个 iteration 节点 `t{k}:it1`（冗余，不单列）。

### 重推（`engine._handle_rewind`）

actor 模型下提交 `Rewind` 时上一 turn 已结束（engine 空闲），故「重推」= 截断 engine history（仅内存）+ 回退 `cache_anchor` + 落 rewind marker（store，payload 含 `cut_index`）+ emit `turn_rewound` + 建新 root TurnRunner 重跑：

```
Rewind(node_id, mode, new_args?)
  → 查 checkpoint（缺 → rewind_rejected(unknown_node)）
  → retry_tool 仅 dispatch 节点（否则 rewind_rejected(mode_kind_mismatch)）
  → 活跃挂起 → rewind_rejected(turn_suspended)   # 挂起态 rewind v1 不支持
  → 冷 engine 首次操作：_last_resolved 为空 + _history 非空
      → 惰性按构造时 entry skill resolve 指令层（resolve 失败 → log warning，不 silent suppress）
  → 选截点：retry_tool=inner_history_len（保 fc）/ re_reason=history_len
  → 截断 history + 回退 anchor（锁内；store append-only，旧 items 不删）
  → re_reason：新 runner 从截点重采样（LLM 重决下游）
    retry_tool：新 runner 先 _complete_seed_call 补跑悬空 call（复用 dispatch_batch +
      _build_tool_context，含 dispatcher → call_skill 子 skill 也能重跑）→ 续推
```

R2：rewind 蓄意回退 anchor → 首采样 cache 失效标 **expected**（`reason="rewind"`），不计 `unexpected_cache_breaks`。冷加载跨进程 cache 不可信，`__init__` 置 `_cache_anchor_index = -1`。

## 分离式 spawn + join-barrier（detached-spawn 契约）

在一次 turn 内发起**各自独立 HITL、各自独立完成**的并发专家 sub-skill，并在全部专家到达终态后自动触发一次聚合 skill turn，全程无 parked 父 turn。详见 [detached-spawn 契约](capabilities/detached-spawn.md) 与 ADR 0015。

### 数据流

```
业务 / LLM 工具 spawn_skill(skill_id, args, reason)
  → DispatchPolicy.check（白名单/深度/环/cannot_call_entry → ValueError）
  → K1 SpawnSlotRegistry.reserve（超限 → SpawnLimitError）
  → 建 child thread + detached asyncio task（cancel = root_cancel.child("spawn:xxx")）
  → 追加 spawn ResponseItem 到父 thread（R5）
  → emit spawn_started{handle_id, skill_id, child_thread_id}
  → 立即返回 {handle_id, child_thread_id}（非阻塞）
  → 父 turn 继续或结束

child task 运行（独立 TurnRunner @ child thread）：
  → 正常完成  → _finalize_spawn → emit spawn_completed
              → _check_barriers（每次终态均检查）
  → HITL 挂起 → 挂起 record 落 child thread → emit spawn_suspended{handle_id, thread_id, record_id, pending}
              → task 退栈，K1 slot 释放（suspended 不占并发额度）
  → 错误      → emit spawn_failed → _check_barriers
  → 取消      → emit spawn_cancelled → _check_barriers

Resume(thread_id=<child_thread_id>, resolutions=...)
  → engine 先查 SpawnHandleRegistry：命中挂起态 → SpawnDriver.resume_spawn（专用路径）
  → SuspensionResolver request 级核销（子集合法,全量达成才结算续跑）
  → _build_child_runner（call_stack 为空 → 独立根 turn）→ 续跑
  → 终态 → _finalize_spawn → _check_barriers
  → abort 裁决（TTL 到期 / 人工）→ _settle_failed → emit spawn_failed → _check_barriers
  → 再挂起 → 句柄重标 suspended，可再 Resume（多轮 HITL）

终态写入单点收敛：done/suspended/cancelled/error 经 _finalize_spawn；
suspended-kill 经 kill_spawn 内联；abort 裁决与各驱动宽 except 兜底经
_settle_failed —— 三个收敛点均「回写 + emit + _check_barriers」成套 + 终态
幂等（终态事件恰好一次），禁止任何路径手写三件套（详见 detached-spawn 契约
§终态写入单点收敛）。

join-barrier（全终态触发）：
  set_join_barrier(handle_ids=[A,B,C], then_skill_id="joint-review")
    → 校验每个 handle 已知 + then_skill_id 存在于 snapshot
    → 追加 join_barrier ResponseItem 到父 thread（R5）
    → emit join_barrier_registered

每次 _check_barriers：
  若全部 handle 终态 + 未 fired → 聚合 args = {hid: {status, result}} for ALL handles
  → _build_child_runner（call_stack 为空，then_skill_id 无 entry 门控）
  → 追加 join_barrier_fired 标记（幂等锚）
  → emit join_barrier_fired{barrier_id, then_thread_id}
```

### engine keepalive 生命周期（引用计数）

`has_live_spawns()` True（有 running / suspended 句柄）时：
- `pool.release(session_id)`（非 force）**空操作**，engine 保持缓存运行
- 父 turn 结束不触发释放

只有以下情形才释放：
- `pool.close()`：无条件拆除，级联取消全部 detached child
- `pool.release(force=True)`：强制释放

### 4 个 LLM 工具（`extra_tools=` opt-in）

通过 `ctx.extras["spawn_coordinator"]`（engine 在每次 TurnRunner 构建时注入自身）接入 engine，均 `parallel_safe=True`：

| 工具名 | 对应 API | 说明 |
| --- | --- | --- |
| `spawn_skill` | `engine.spawn_skill(skill_id, args, reason)` | 分离发起，立即返回 `{handle_id, child_thread_id}` |
| `await_skills` | `engine.set_join_barrier(handle_ids, then_skill_id, then_args_template)` | 登记 join-barrier，返回 `{barrier_id}` |
| `join_skill` | `engine.spawn_status(handle_ids)` | 非阻塞读各句柄当前 status+result |
| `kill_skill` | `engine.kill_spawn(handle_id)` | R4：杀单个，兄弟不受影响 |
| `send_message` | `engine.deliver_peer_message(target, text, mode)` | peer 点对点投递（thread_id / handle_id / "parent" 寻址，双模式） |
| `wait_peer` | `engine.wait_spawn_terminal(handle_id, timeout_seconds)` | turn 内阻塞等单个句柄终态（timeout **必填**防互等死锁） |

**注**：detached-spawn 能力**无新 Op**——发起 / 查询 / 取消全部通过 LLM 工具或业务直调 engine API 完成，不走 Submission 队列（与 turn 无关的 out-of-band 操作）。

### Resume child-thread 路由

`Resume(thread_id=<child_thread_id>)` 在 engine 分发时：

1. **先查 `SpawnHandleRegistry`**：命中 suspended 句柄 → `SpawnDriver.resume_spawn`（专用路径，不走父链）
2. **未命中** → 走原有 `_handle_resume` / `_handle_child_resume`（call_skill 嵌套挂起续跑链）

两条路径**严格不重叠**：detached spawn 用专用路径，call_skill 嵌套挂起用父链。

### K1 配额语义（nuance）

| 旋钮 | 维度 | suspended 是否占用 |
| --- | --- | --- |
| `max_concurrent_spawns` | 并发（in-flight runner） | **否**——runner 退栈即释放 slot |
| `max_total_spawns` | 生命周期累计（单调） | 是（每次 spawn 调用递增，不回收） |

结论：HITL 等待期不消耗并发额度，可支持大量错峰 HITL 并发场景。

### 资源护栏的另两条正交维（turn-resource-guards）

K1（广度）/ K2（token）之外，turn 级还有两条 opt-in 护栏（默认零变化）：

- **denial 断路器**：`denial_breaker_config`（`DenialBreakerConfig{max_consecutive_denials, max_recent_denials, window_size}`）注入后，TurnRunner 每 turn 新建 `DenialBreaker`，在工具配对回填处统一观察 `ToolResult.data["reason"] ∈ {hook_denied, permission_denied}` 计数（成功重置 consecutive）；越阈值 emit `denial_circuit_open` **恰好一次** + 迭代边界以同名 `end_reason` 提前终止（当轮 fc/output 已配对落史，无孤儿）。防「被拒后在 max_iterations 内空转重试」。
- **迭代预算分层**：裸计数器已抽成 `IterationBudget`（consume/refund/child）。`run_sub_skill` 派生子 turn 传 `budget.child()` —— 子独立预算、不回写父（父子总和可超父 cap，hermes 对标的有意语义；全局硬顶用 K2）。`ToolSpec.refunds_iteration=True` 的工具成功轮 `refund(1)` 不耗外层步数（spec 静态声明，LLM 不可触发；内核不为既有工具默认开启）。
- **doom-loop 检测**：`doom_loop_config`（`DoomLoopConfig{max_consecutive_repeats=N}`）注入后，TurnRunner 每 turn 新建 `DoomLoopDetector`，在同一配对回填处只观察**成功**结果的 `(tool, arguments_raw)`：连续 N 次同签名 → `doom_loop_warned` + 注 `system_injection(source="doom_loop")` 中性事实（让模型自改、turn 续跑）；警后到 2N → `doom_loop_circuit_open` + 迭代边界以同名 `end_reason` 终止。补「反复同参数调同一工具、每次成功、毫无进展」的盲区（DenialBreaker 只认 deny、IterationBudget 只数总量）。ADR 0021。

完整契约见 [`capabilities/turn-resource-guards.md`](capabilities/turn-resource-guards.md)。

## mid-turn steering：运行中 turn 注入用户输入（midturn-input-steering 契约）

`UserMessage` 经 `asyncio.create_task(self._run_turn_for(...))` 派发 —— turn 跑在独立 task，**不阻塞 Op 主循环**。据此支持「运行中 turn 不打断地插话」：

- **Op**：`InjectUserInput{submission_id, text}`（区别于 `InjectSystemMessage` 注 system 注记）。
- **共享队列**：`_PendingTurn.pending_input` 与对应 `TurnRunner.pending_input` 是同一 list 引用（`_run_turn` 构造时从 `self._pending[submission_id]` 取）。engine 主循环 append、runner drain，同 event loop 协作式调度无需锁。
- **drain seam**：`TurnRunner._drain_pending_input` 在迭代循环顶部（`_maybe_compress(pre_turn)` 前、成对 fc/output 已闭合的安全点）把 pending 转 user_message 并入 history + emit `UserInputInjected{delivered:true}`。
- **无活跃 turn 退化**：主循环找不到 `_pending[submission_id]` → 文本落历史不起新 turn（codex `inject_no_new_turn`），emit `delivered:false`。
- **R4**：drain 前 `cancel.is_cancelled` 守卫，已取消 turn 不并入（文本由 engine 收尾落历史，不丢，R5）。

完整契约见 [`capabilities/midturn-input-steering.md`](capabilities/midturn-input-steering.md)。

## peer-mailbox：活体 agent 间点对点消息（peer-mailbox-messaging 契约）

steering 解决「用户 → 运行中 turn」；peer-mailbox 把同一 seam 推广到「agent → agent」（同 engine 谱系内 sibling↔sibling / child→parent）：

- **Op + 工具同路径**：`SendToPeer{target_thread_id, text, mode}` 与 `send_message` 工具都收敛到 `engine.deliver_peer_message`（SpawnDriver 实现）。寻址 = child_thread_id / handle_id / `"parent"`（解析为谱系 root）；未知目标显式 error。
- **双模式**：`queue_only`（运行中投目标 runner 的 `pending_input`——B1 同一队列；空闲即时 `store.append` 落史，R5）；`trigger_turn`（空闲 spawn child 落史后以续跑范式唤醒新 detached turn，emit `peer_agent_woken`；运行中自动降级 `mode_downgraded=true`；root 拒绝；suspended 只落史——挂起只能由 Resume 解除）。
- **消息形态**：`user_message` + payload `source="peer", from_thread`（不新增 kind）；事件 `peer_message_sent` 不含正文。
- **wait_peer**：turn 内轮询句柄表等单个终态，与 `await_skills`（barrier，turn 结束后聚合）分工互补。

完整契约见 [`capabilities/peer-mailbox-messaging.md`](capabilities/peer-mailbox-messaging.md)。

## turn 的终止结局：含 suspended（suspend-resume）

`run_turn` 现有四种 `end_reason`（写进 `TurnCompleted.data["end_reason"]`，业务侧据此路由）：

| end_reason | 含义 | 后续 |
| --- | --- | --- |
| `completed` | 正常结束（含 max_iterations / resource_limit / denial_circuit_open / doom_loop_circuit_open 等护栏收尾） | — |
| `cancelled` | `CancelTurn` 中止 | — |
| `error` | 未捕获异常 / 确定性 LLMError 硬失败 | TurnFailed 配方 |
| **`suspended`** | turn 中途挂起（人类输入类 / 系统态），实例可释放 | 业务侧凭 `thread_id` 提交 `Resume` 续跑 |

**挂起即结局，不阻塞**：挂起点（`SuspendingPrompter.ask` / `request_user_input` 工具 / LLM 可恢复错误 retry 耗尽）不再阻塞 `await`，而是抛 `SuspendSignal`（内部控制流异常，**不继承 LLMError**）。`dispatch_batch` 把整批 `SuspendSignal` 收集成 `ToolCallOutcome.suspend`（不 fail-fast，支持多挂起点并存），`_dispatch_tools` 聚合后 `raise _BatchSuspend(pending...)`；`run_turn` 在通用 `except Exception` **之前**先 `except _BatchSuspend` / `except SuspendSignal`，落一条 `suspension` item（history + store）并把 `end_reason` 退栈为 `"suspended"`（避免被误分类成 TurnFailed）。终结 emit 据此发独立终结态 `TurnSuspended`（携带 `thread_id` / `record_id` / `pending` / `cache_invalidated`）而非 `TurnCompleted`，业务侧据此区分「完成」与「挂起待续」（R3）；`TurnOutcome` 返回值不变，仍带 `suspension` 与 `end_reason="suspended"`。协程随即彻底退栈，engine 实例可释放（tier-1 留 Pool / tier-2 驱逐 + 进程可退）。

**`Resume` Op 续跑**：`AgentEngine._handle_resume`（详见 [suspend-resume 契约](capabilities/suspend-resume.md)）：

```
Resume(thread_id, resolutions)
  → _find_active_suspension：扫 history 取最后一条未被 resolved-marker 消费的 suspension
       （找不到 → emit suspension_resolve_rejected(reason="no_active_suspension")）
  → SuspensionResolver.plan（非空子集；空集/未知 id → ResolveError → suspension_resolve_rejected）
  → 应用 plan：补齐 history-gap（form/data 直接回填 output；permission deny 回填 error；
       permission allow → _execute_resumed_tool 真正执行 tool，preapprove 一次性放行）
  → 落 resolved-marker（system_injection source='suspend_resolved'，幂等：重复 Resume 被拒）
  → emit suspension_resolved
  → 非 abort → _build_and_run_runner 续采样（system_retry → 重跑同次 sample）
```

挂起期间收到 `CancelTurn` → `_cancel_active_suspension` 同样追加 resolved-marker 丢弃挂起（R4）。**挂起真相** = 持久化的 `suspension` item + `function_call`-无-`function_call_output` 的 history-gap，使 mid-turn 挂起跨进程 resume 可行（R5）。

**子 skill 战绩沉淀**：`_spawn_sub_runner` 在子 skill 到达**终态**（`end_reason != "suspended"`）后，通过注入的 `OutcomeJudge`（默认 `StructuralOutcomeJudge`）裁决出 `success / failure / abandoned`，构造 `SkillExecutionRecord`，调 `store.append([skill_outcome_item(...)])` 旁路追加到子 thread JSONL，并 emit `SkillOutcomeRecorded` 事件。`suspended` 提前返回路径**不记录**（挂起不是终态）。`skill_outcome` item 不进 LLM 消息视图（`build_api_request` 跳过）。完整契约见 [skill-outcome-record.md](capabilities/skill-outcome-record.md)。

**子 thread 嵌套挂起 + 续跑回传父 call_skill**：`call_skill` 派发的子 skill 在独立子 thread 运行（`_spawn_sub_runner`，history 隔离）。子 turn 挂起时挂起记录落**子 thread**，子 emit `turn_suspended`（子 thread_id）。`_spawn_sub_runner` 在子 `end_reason=="suspended"` 时抛 `SuspendSignal(reason=CHILD_SKILL, detail={sub_thread_id, skill_id})` → 父 `call_skill` 随之挂起 → 逐层上抛至根 → 根也 emit `turn_suspended`。`Resume(thread_id=<子 thread>)` 经 `_handle_resume` 分流到 `_handle_child_resume` 续跑链：自根沿 `CHILD_SKILL` pending 串链至 leaf（不依赖 `get_metadata`，谱系由父挂起 record 的 pending detail 携带）→ 核销 leaf 用户挂起 + 续跑子 turn → 把子结果逐层回填父 `call_skill` 的 `function_call_output` + 续跑父 turn → 根以 `_build_and_run_runner` 收尾。子层续跑 turn `is_root=False`（engine 注入非空 `call_stack`），根续跑 `is_root=True`。**`Resume` 经 `asyncio.create_task` 异步派发**（与 `UserMessage` 一致），使续跑链多 turn 不阻塞 run 循环、且给 `subscribe(submission_id)` 留出注册窗口。详见 [suspend-resume 契约](capabilities/suspend-resume.md) §子 thread resume 续跑链。

- [x] `cancel.child()` 派生层级正确，父取消级联 —— `tests/loop/test_cancellation.py::test_child_cancel_propagates_from_parent`

## audit-required Session 的 Journal-first 执行（business-integration 契约）

注入 `AuditConfig` 后，Engine 对该 Session 切换到 **Journal-first 审计执行**：每一次业务效果都
「先 durable 落 Journal → definite ack → 才应用到 hot history / projection / 下游效果」，由
`SessionAuditCoordinator`（单 Session append/lifecycle 锁 + effect gate）统一门控。完整数据契约见
[SessionJournal Business Integration 能力契约](capabilities/session-journal-business-integration.md)；此处只记模块协作。

- **Submission admission（动态门）**：`AgentEngine.submit()` 在 audit 模式仅放行 UserMessage / CancelTurn /
  Shutdown。UserMessage 的 durable acceptance（`submission_accepted` + user 会话项 + `submission_applied`
  原子三记录）**先于**入队，actor 只应用已 ack 的 envelope 才更新 hot history/projection；非法输入落安全
  `submission_rejected` 不入队；能力面外的 Op（CompactNow/Rewind/Resume/… 共 10 类）在执行前 durable 拒绝
  （`reject_unsupported_audited_op`，failure_class=capability），不入队、不执行。
- **effect gate**：每个 durable 效果前 `coordinator.ensure_effect_allowed()`；首个 Journal IO / 完整性 /
  ack 不确定失败即 freeze（关闭 effect gate），此后该 Session 的 LLM/Tool/Skill 效果全部被拒。
- **LLM**：见 llm-client.md「attempt observer 与 checkpoint-before-delta」；checkpoint 之后、任何 Tool
  effect / turn 终态之前，`_sample_once` 原子提交 `llm_response_committed` + provider 顺序的
  reasoning/assistant/function_call 会话项（`audit_llm.commit_audited_llm_response`），ack 后才 extend hot
  history；function_call 会话项先于 Tool intent durable。
- **Tool convergence**（`audit_tool.audited_tool_batch`）：派发前把整批有序 `tool_intent_committed` 作为一个
  原子 batch durable；随后在 `anyio.fail_after(shield=True)`（取消无关有界 finalization）内复用既有
  `dispatch_batch`，让工具经 `ctx.cancel` 协作取消产出确定结果，而 outcome 落账不被外层取消打断；每个已提交
  意图恰好收敛到一个 `tool_outcome_committed`（success/error/rejected/cancelled/unknown）+ 唯一
  `function_call_output` 会话项，按 call-index 有序。整批派发前取消 → cancelled（不进 runtime）；dispatch
  中途取消/超时对 reconcilable/external_non_idempotent → UNKNOWN（无法证明外部效果）→ 记录后 freeze；声明
  non-suspending 却运行时挂起 → error 终态 + freeze（不进 HITL）。
- **同步 call_skill lineage**（`audit_skill.AuditedSkillDispatch`）：复用根 coordinator/lease；外层 Tool 意图
  之后先 durable `skill_selected`（完整 definition/body 快照）→ 配额拒绝走 `skill_dispatch_finished(rejected)`
  无 child；接受走原子 `skill_dispatch_started`+`thread_created`+`thread_bound`+child-seed，子 runner 携
  child `audit_state`（child thread、共享根 coordinator、同一 projector）→ 子 turn 的 LLM/Tool/Skill 效果
  递归走同一审计路径 → 原子 `skill_dispatch_finished`+`thread_terminal`+`skill_outcome`，先于外层 Tool
  outcome。嵌套 call_skill 天然形成三级谱系；子 turn 若挂起属 capability 违约 → freeze。
- **cancellation**：每个 active turn 有目标取消子树（CancelTurn 只取消其目标 turn/子树），Session root 取消
  保留给 freeze 与 Shutdown；LLM checkpoint 与 Tool outcome 的落账均为取消无关（shield）。
- **Session isolation**：coordinator/writer 健康态每 Session 独立；一个 Session freeze 不影响其他 Session 的
  effect gate 与提交。

> legacy 模式（未注入 `AuditConfig`）逐字保持原有 dispatch/persist/suspend/resume 行为，本节所有 audit 分支
> 均以 `audit_state is not None` 门控，对 legacy 零行为变化。
