# ADR 0109：工具与脚本执行带归属标识（会话、提交）

- 状态：Accepted
- 日期：2026-09-30
- 关联：[suspend-resume](../architecture/capabilities/suspend-resume.md)、[script-execution](../architecture/capabilities/script-execution.md)

## 背景

宿主在内核之上做按会话隔离的工作区、按轮归属的审计时，需要知道一次执行「属于哪个会话、哪一轮」。
内核给出的信息有两处缺口：

1. **审批通过后的重跑没有 `submission_id`**。turn 内正常派发时 `ToolContext.extras` 带
   `submission_id`；`Resume` 批准的调用在 engine 层重跑（根 thread 的 `execute_resumed_tool`、子 thread 的
   `execute_resumed_tool_on_thread`），两处各自手写了一份 extras，都没有这个键。同一个工具的两次执行，
   一次能归到某一轮、一次不能。
2. **执行路径上拿不到会话标识**。`session_id` 只在 Engine 上；`ToolContext.extras` 与
   `ScriptInvocation` 都不带。`ScriptExecutor` 不知道脚本属于哪个会话，只能让同一运行时下的所有会话
   共用一个工作区。

命中 ADR 0017 规则①。

## 决策

1. **`ToolContext.extras` 在三条执行路径上都带 `submission_id` 与 `session_id`**：turn 内派发、根 thread
   重跑、子 thread 重跑。重跑时的 `submission_id` 是触发它的那次 `Resume` 的 submission id——执行
   发生在这一轮，它的事件也记在这一轮名下，与审计模式下续跑 turn 里重跑的取值一致。
2. **两条重跑路径共用一个上下文构造函数**（`loop/resume_tool_context.py`），不再各写一份 extras。
3. **`session_id` 随 `TurnRunner` 传递**，`call_skill` 的子 turn 与分离式派发的子 turn 继承同一个值：
   一个会话的 root 与各子 thread 共用一个会话标识。
4. **`ScriptInvocation` 增加四个可选字段**：`thread_id`、`session_id`、`submission_id`、`call_id`，由
   `run_script` 工具填入。都有默认值 None——直接构造 `ScriptInvocation` 的调用方、只读前三个字段的
   执行器都不受影响。

## 不做

- **重跑时补齐依赖 runner 的键**（`dispatcher`、`call_stack`、`spawn_coordinator`、`iteration` 等）：
  engine 层重跑没有 runner，造不出这些对象；需要它们的派发类工具本来就在续跑的 turn 里重跑。
- **重跑时给「发起调用的那一轮」的 submission id**：执行与事件都在 `Resume` 这一轮；要找原来那一轮，
  挂起记录里有（`SuspensionRecord.submission_id`）。
- **把 `session_id` 提成 `ToolContext` 的一等字段**：`ToolContext` 是稳定层类型，业务直接构造它的
  测试代码很多；extras 里的键是增量，不破坏任何调用方。

## 影响

- R1：无业务概念；`session_id` 是 `EnginePool.get_or_create` 已有的参数。
- R2–R5：无影响。

### 行为变化

- `ToolContext.extras` 多一个 `session_id` 键；审批通过后重跑时多一个 `submission_id` 键。
- `ScriptInvocation` 多四个默认为 None 的字段。

## 验证

`tests/loop/test_execution_identity.py`（4 项）：正常派发与审批后重跑各带所在那一轮的 submission id 与
会话 id；子 thread 上的重跑同样带；`run_script` 把线程、会话、提交、调用 id 交给执行器；四个字段可省。
