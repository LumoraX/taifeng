# Capability: execution-observability（执行树观测，实验层）

## Purpose

宿主订阅一个 engine 的事件流，即可识别并行 Skill 调用、观察阶段与资源用量，并在普通终端展示调用树。内核提供执行身份和事实事件；telemetry 提供纯状态投影与可选显示器。

## 数据契约

- `ExecutionIdentity`：不可变 Pydantic 模型。字段 `execution_id`、`attempt_id`、`thread_id`、`skill_id`、`model`，以及可空 `parent_execution_id` / `parent_thread_id`。
- v1 `execution_id = thread_id`，表示逻辑执行节点；每次 `TurnRunner.run()` 新建唯一 `attempt_id`。同 Skill 的并行调用创建不同 thread，因而不会合并；同 thread 的新 turn / Resume 更新同一节点而不伪造新 agent。
- `EventMsg.execution: ExecutionIdentity | None = None` 为可选增量字段。旧事件缺省 None；`submission_id`、seq 和订阅过滤保持原语义。身份不进入 LLM prompt 或 Journal 真相。
- `execution_updated` 是新增元数据事件，data 包含 `phase`（`model` / `running` / 异常退出时 `failed` / `cancelled`）、`iteration`、当前 attempt 自身累计 `usage`、`usage_complete`、`context_tokens`、`context_window`、`context_measured`。不包含正文、工具参数、推理内容或凭据。
- `ExecutionSnapshot` 为冻结 dataclass；至少含上述身份字段、`state`、`active_tools: tuple[str,...]`、`iterations`、`input_tokens` / `output_tokens` / `total_tokens`、`usage_complete`、上下文三字段、`started_at` / `updated_at` / `finished_at`（datetime）。状态为 `queued/running/model/tool/waiting_children/waiting_input/completed/failed/cancelled`。
- `ExecutionProjection(max_nodes=2048)`：单 engine 全量流的纯投影；`handle(EventMsg)` 同步应用、`snapshot() -> tuple[ExecutionSnapshot,...]` 返回不可变快照；`incomplete: bool` 标记跳号、迟到或容量截断。`max_nodes` 必须为正。
- `TerminalTreeSink(out=None, interactive=None, max_rows=40, projection=None)`：实现 TelemetrySink；`handle(ev)` 更新投影并显示，`attach(engine)` 订阅完整流；`render() -> str` 返回终端快照供宿主复用和测试。`attach_terminal_tree_sink(engine, **kwargs)` 返回后台 task。默认只在 TTY 重绘；非 TTY 保持追加可读输出。入口经 experimental 导出。

## 身份与传播

### Requirement: 每个执行的事件有统一身份
- WHEN root、同步 call_skill（含并行编排）、detached spawn 在 run 中发出事件，THEN SHALL 附带所属执行的身份；工具和文本不得仅按 submission_id 分组。
- WHEN 同一 session 的子 runner 在父异步任务上下文中启动，THEN SHALL 继承父 execution_id/thread_id；通过上下文作用域隔离兄弟节点，finally 恢复作用域。
- WHEN 跨 session 启动另一个 engine，THEN SHALL 不继承其他 session 的父级。
- WHEN engine 在活跃执行作用域中发出协调事件（如 spawn_started），THEN execution 标识发出方，事件 data 仍标识目标 child/handle；投影 SHALL 区分发出方与目标。

### Requirement: Resume 与观测边界
- WHEN 同 thread 开始新 attempt，THEN SHALL 重置阶段、活动工具与当前轮次，累计不同 attempt 自身用量，保留已知父级。
- WHEN 冷恢复或晚订阅未观察到父级，THEN SHALL 允许父级未知，不根据模型文本猜测调用关系；本轮不重建冷恢复前的完整执行树。
- WHEN 未收到历史、全量事件 seq 跳号或节点超限，THEN SHALL 标记 incomplete，不能展示为完整任务进度。

## 状态与计量

### Requirement: 状态来自事实事件
- WHEN spawn_started 到达，THEN SHALL 可先建立 queued 子节点；turn_started / execution_updated 表示实际开始和模型阶段。
- WHEN tool_call_started/completed 到达，THEN SHALL 按 call_id 维护并发活动工具；call_skill、await_skills、wait_peer、wait_any 表示 waiting_children，其余为 tool。单个工具错误不直接宣告整个执行失败。
- WHEN turn_suspended 到达，THEN SHALL 显示 waiting_input；恢复的新 attempt 进入运行态。取消请求本身不能显示已取消，只有真实退出事实才进入 cancelled。
- WHEN spawn_started 到达，THEN SHALL 记录 handle_id→child_execution_id 映射；spawn_completed/failed/cancelled/suspended SHALL 更新该目标子节点，而非 execution 字段指向的发出方。即使子节点尚无 attempt 或已挂起，也必须能据 handle 收敛。
- WHEN turn_completed/turn_failed 或作用域异常退出到达，THEN SHALL 收敛终态。终态后的同 attempt 遗留事件不得复活节点，新 attempt 可以重新运行。

### Requirement: 用量不能重复累计或冒充进度
- WHEN 单次采样完成或 turn 收尾，THEN SHALL 使用本 attempt 自身 usage 绝对值更新，重复快照不得重复记账；不得将父 subtree_usage 与子 usage 再相加。
- WHEN usage 不完整，THEN SHALL 保留已知用量并标明不完整，不用零值伪装未知消耗。
- WHEN provider completed 缺少有效 input_tokens，THEN SHALL 保持 context_measured=False，不能把估算值标成实测。
- WHEN 显示上下文，THEN SHALL 区分测量值和估算值；累计 token 不作为上下文占用或任务完成百分比。
- WHEN 显示子执行完成数，THEN SHALL 明确只统计已观察节点，不冒充未知任务总数。

## 终端与接口约束

- 渲染 SHALL 使用有限行数，过滤所有控制字符（包括 ESC / C1）并限制字段宽度；非 TTY 不发重绘控制序列。
- 活动树环或缺失父节点 SHALL 有界处理，不能递归挂死；仅显示已知事实。
- sink 是独立订阅任务，TextIO.write/flush SHALL 在线程中执行，不能因慢输出阻塞共享事件循环；取消 attach task SHALL 放弃等待正在进行的写入，不再安排新写入（已开始的底层写入可能稍后结束），不向 engine 发控制动作。
- 普通 ConsoleSink 保持可用，新增 execution_updated 具名渲染；不开启终端 sink 仍可用 JSONL / SSE 消费同一信号。

## R1–R5

- R1：零业务字段；不加入具体 UI 框架，ContextVar 仅作任务本地观测传播，跨 session 隔离。
- R2：身份和更新事件不改 prompt/cache，既有压缩的 cache_invalidated / anchor_preserved_until 不变。
- R3：采样边界更新、生命周期沿用原事件，seq 仍由唯一事件入口分配。
- R4：不增加 actor 等待；作用域取消传播，显示器由宿主取消，容量与递归有界。
- R5：不改变追加写与恢复真相；attempt 是本进程观察标识，冷恢复重新观察，不提供监控视图持久化保证。

## 验证

模拟验证：同 Skill 并行 call_skill / spawn、工具归属、父子嵌套、跨 session 隔离、取消/异常、挂起续跑、重复事件、seq 跳号、空输入、超长/控制字符、缺失父级、环和深度上限、TTY/非TTY。
真实验证：全量 capability_matrix，并在既有多路派发场景上校验身份与父子关系，核验模型发出的真实调用均能被终端状态投影观察。
