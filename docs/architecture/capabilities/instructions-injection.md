# instructions-injection Specification

## Purpose
TBD - created by archiving change instructions-injection. Update Purpose after archive.
## Requirements
### Requirement: 数据契约 InstructionSource / Layer / Context / ResolvedInstruction

系统 SHALL 提供以下类型作为协议的输入输出：

- `InstructionSource(Protocol, runtime_checkable)`：`async def fetch(ctx: InstructionContext) -> str | None`；返回 None 表示本次不注入；实现 SHALL 协程并发安全
- `InstructionContext`（frozen）：`session_id / thread_id / entry_skill_id / turn_index / metadata: dict[str, Any] / cancel: CancellationToken`（`metadata` 为业务侧不透明上下文，无业务命名字段，R1；taifeng 不解析其 keys）
- `InstructionLayer`（frozen）：`name / source: InstructionSource | str / scope: Literal['engine','session','turn'] / cache_ttl_seconds: float = 0 / priority: int = 0 / cache_volatile: bool = True`
- `ResolvedInstruction`（frozen）：`name / scope / text / fetched_at: float / source_kind: Literal['static','dynamic'] / cache_hit: bool`
- `InstructionFetchError(LLMError)`：`layer_name: str / cause: Exception`

#### Scenario: 顶层导入
- **WHEN** 业务侧 `from taifeng import InstructionSource, InstructionLayer, InstructionContext, ResolvedInstruction, InstructionFetchError`
- **THEN** 五个符号 SHALL 全部可用

#### Scenario: dataclass 不可变
- **WHEN** 代码尝试 `layer.name = "x"` 或 `resolved.text = "..."`
- **THEN** SHALL raise `dataclasses.FrozenInstanceError`

### Requirement: 装配顺序

`render_system_prompt` 被调用、传入 `instructions: list[ResolvedInstruction]` 时，输出 system prompt SHALL 按以下顺序拼接：

1. `<system_instructions>` 块（按 priority 升序），每层一个独立块，含 `name / scope / priority` 属性
2. `<entry_skill>` 块（含 entry skill body）
3. `<available_child_skills>` 块（仅 id + description）
4. `<dispatch_policy>` 块

若 `instructions` 为空或 None，输出 SHALL 不含 `<system_instructions>` 块（向后兼容）。

#### Scenario: 两层不同 priority 按升序拼接
- **WHEN** layers = `[(name='b', priority=50), (name='a', priority=10)]`
- **THEN** prompt 中 `<system_instructions name="a">` SHALL 出现在 `<system_instructions name="b">` 之前

#### Scenario: 空 instructions 不出现块
- **WHEN** 调用 render_system_prompt 时传入 None 或空 list
- **THEN** 输出 SHALL 不含 `<system_instructions>` 子串

### Requirement: 三档 scope 生命周期

InstructionResolver SHALL 在 `EnginePool` / `AgentEngine` / `TurnRunner` 三个生命周期边界分别解析对应 scope 的层并缓存到对应层；上层缓存命中 SHALL 跳过重解析：

- `EnginePool.create(instruction_layers=...)` 被调用时，SHALL 立即解析 `scope='engine'` 的层，结果在进程生命周期内不变
- `AgentEngine.__init__` 创建会话时，SHALL 解析 `scope='session'` 的层，结果缓存到 engine 实例
- 每次 `TurnRunner.run_turn` 启动时，SHALL 解析 `scope='turn'` 的层；engine + session 层 SHALL 复用缓存

#### Scenario: engine scope 静态文本永不重 fetch
- **WHEN** engine layer source 是 str
- **THEN** EnginePool.create 之后该层 SHALL 永不重新 fetch

#### Scenario: turn scope ttl=0 每 turn 拉
- **WHEN** turn layer source 是 Protocol 且 cache_ttl_seconds=0
- **THEN** 每个 turn SHALL 触发 fetch（ResolvedInstruction.cache_hit=False）

### Requirement: 缓存

同一 layer 的 source 是 Protocol 且 `cache_ttl_seconds > 0` 时，系统 SHALL 用 `(layer.name, ctx.session_id, ctx.entry_skill_id)` 作键缓存 fetch 结果。首次 fetch 之后 ttl 内的 resolve SHALL 走缓存（telemetry: `instruction_cache_hit`）；ttl 过期后下次 resolve SHALL 重新 fetch（telemetry: `instruction_fetched`）。

#### Scenario: ttl 内命中走缓存
- **WHEN** ttl=10s，第一次 fetch 后 1s 内再次 resolve
- **THEN** SHALL 不调 InstructionSource.fetch；ResolvedInstruction.cache_hit SHALL = True

#### Scenario: ttl 过期重新 fetch
- **WHEN** ttl=1s，第一次 fetch 后 2s 后再次 resolve
- **THEN** SHALL 调 fetch 一次；ResolvedInstruction.cache_hit SHALL = False

### Requirement: 热更 UpdateInstructions

`engine.submit(UpdateInstructions(layer_name='x', new_source=...))` 被消费时，系统 SHALL 替换内部 layers 中 `name=='x'` 的条目。该 layer 的缓存 SHALL 失效（下次 resolve 强制 fetch）。SHALL 发 EventMsg `instruction_updated`（含 `layer_name / new_source_kind`）。

#### Scenario: 热更后下个 turn 立即生效
- **WHEN** turn N 完成后 submit UpdateInstructions(name='x', new_source='new text')
- **THEN** turn N+1 的 system prompt 中 name='x' 的块 SHALL 含 'new text'

#### Scenario: 热更未知 name 被拒绝
- **WHEN** UpdateInstructions 的 layer_name 不在现有 layers
- **THEN** 系统 SHALL 拒绝（EventMsg `instruction_update_rejected` 含 reason='unknown_layer'）；layers 不修改

### Requirement: 外部读取 instructions_snapshot

`engine.instructions_snapshot()` 被业务侧调用时，系统 SHALL 返回 `list[ResolvedInstruction]`（最近一次 resolve 的副本，按 priority 升序）。返回值 SHALL 是 frozen / immutable。若 engine 尚未跑过任何 turn，snapshot 仅含 engine scope 的层（session / turn 层为空）。

#### Scenario: snapshot 不可变
- **WHEN** 业务侧拿到 snapshot 后尝试 `snapshot[0].text = "x"`
- **THEN** SHALL raise `FrozenInstanceError`；业务侧操作不影响 engine 内部状态

#### Scenario: 未跑 turn 仅返回 engine scope
- **WHEN** engine 刚构造未 submit 任何消息
- **THEN** snapshot SHALL 仅含 scope='engine' 的层（session / turn 层为空）

### Requirement: 失败处理 fail-fast

InstructionSource.fetch 抛任何异常时，系统 SHALL 包成 `InstructionFetchError` 上抛到 TurnRunner。该 turn SHALL 失败（不允许 silent fallback 到空字符串）。SHALL 发 EventMsg `instruction_fetch_failed`（含 `layer_name / cause_repr`）。

#### Scenario: fetch raise 转 InstructionFetchError
- **WHEN** InstructionSource.fetch 抛 `RuntimeError("db_down")`
- **THEN** TurnRunner SHALL 抛 `InstructionFetchError`，其 cause 是该 RuntimeError
- **AND** EventMsg `instruction_fetch_failed` SHALL 被发出，含 layer_name + cause_repr

### Requirement: fetch 内部不发起 HITL

InstructionSource.fetch 检测到业务级权限不足 / 租户禁用 / 配额超限时，实现方 SHALL 直接 raise（被包成 InstructionFetchError）或返回 None（表示本次不注入）。SHALL NOT 自行发起 HITL 询问机制（弹窗 / SSE / Slack bot 等）。

> 设计原因：HITL 决策点在 taifeng 中统一由 `PermissionPolicy + PermissionPrompter` 承担（动作级权限）。InstructionSource 是**数据级**权限决策点 —— 业务侧在 fetch 内部完成访问控制，不与动作级 HITL 通道混用。

#### Scenario: 业务侧权限不足直接 raise
- **WHEN** 业务侧 InstructionSource 发现当前 tenant 无权读该指令
- **THEN** SHALL raise 或返回 None
- **AND** SHALL NOT 调用 PermissionPrompter / 发起弹窗 / 推 SSE 等待用户决策

### Requirement: 取消传播

TurnRunner 的 CancellationToken 被取消时，任何 in-flight 的 InstructionSource.fetch SHALL 在下次 await 点立即终止。Resolver SHALL NOT 抛 InstructionFetchError，而是抛 `anyio.get_cancelled_exc_class()`（让上层级联）。

#### Scenario: cancel 中断长时 fetch
- **WHEN** fetch 内部 `await anyio.sleep(10)`，0.5s 后父 turn cancel
- **THEN** fetch SHALL 在 1s 内退出（抛 CancelledError）
- **AND** SHALL NOT 发 `instruction_fetch_failed` 事件（属于正常取消）

### Requirement: 可观测 EventMsg

系统 SHALL 通过 TelemetrySink 发以下 EventMsg：

| EventMsg kind | 触发时机 | 必含字段 |
| --- | --- | --- |
| `instruction_fetched` | 动态 source 完成一次 fetch | `layer_name / scope / duration_ms / text_length` |
| `instruction_cache_hit` | 缓存命中跳过 fetch | `layer_name / cache_age_seconds` |
| `instruction_updated` | UpdateInstructions Op 成功 | `layer_name / new_source_kind` |
| `instruction_fetch_failed` | InstructionFetchError 抛出前 | `layer_name / cause_repr` |
| `instruction_update_rejected` | 未知 name 等 | `layer_name / reason` |

#### Scenario: 成功 fetch 发 instruction_fetched
- **WHEN** 动态 source 第一次被解析成功
- **THEN** EventMsg `instruction_fetched` SHALL 被发出，含 `layer_name / scope / duration_ms / text_length`

