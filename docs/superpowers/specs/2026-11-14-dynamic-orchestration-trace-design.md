# 动态编排与执行关系基础展示设计

## 目标

在微内核的实验观测层提供一个最小的关系展示能力：同一份事件流同时保留实际执行节点和动态编排计划事件，输出稳定、有限的文本关系。业务层可以在此快照之上实现卡片、网络图、时间线或其他布局。

## 边界

- 内核只提供执行事实、计划事件、父子关系、状态和有界文本输出。
- 计划节点与实际 child thread 没有可靠绑定时不猜测绑定关系。
- 计划事件按 `seq` 追加保存；重规划、新 attempt 和 Resume 不覆盖旧事件。
- UI 卡片、颜色、坐标、进度算法、任务文案和跨 engine 调度留在宿主层。
- 计划历史达到容量上限时保留已知前缀并将 `incomplete` 置为真。

## 数据模型

`ExecutionSnapshot` 继续表示每个逻辑执行节点的当前状态和累计用量。

新增两个冻结快照：

- `OrchestrationGroupSnapshot`：计划组序号、`parallel / serial / when` 类型、技能 ID 或条件。
- `OrchestrationPlanSnapshot`：执行节点、attempt、技能、计划组、事件 `seq` 和更新时间。

`ExecutionProjection.plan_snapshot()` 返回容量内按事件顺序排列的全部计划快照。它消费已有的 `orchestration_plan_resolved` 事件；无效结构只标记 `incomplete`，不生成推测节点。

## 文本输出

`TerminalTreeSink.render()` 的基础输出同时包含：

- 已观察执行树：由 `execution_id` / `parent_execution_id` 组成；
- 计划事件行：使用 `[计划#<seq>:<type>]` 前缀，明确标记为待执行计划，不伪装成执行节点。

输出保持当前有界行数、控制字符过滤、宽字符裁剪、环和缺失父级保护。业务层可直接读取两个不可变快照，不必依赖该文本格式。

## 验证

- 计划重规划追加而不覆盖；新 attempt 也保留既有计划事件。
- 损坏计划和计划历史容量截断标记 `incomplete`。
- 文本输出同时包含计划历史和实际执行节点。
- 运行全量 pytest、examples 冒烟、ruff F/S108/I/TC 和 mypy strict。
