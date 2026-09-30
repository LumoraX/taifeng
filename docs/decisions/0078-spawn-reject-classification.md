# ADR 0078：spawn 拒绝带稳定分类，对模型与事件流可见

- 状态：Accepted
- 日期：2026-09-30
- 关联：[detached-spawn 契约 § 拒绝路径](../architecture/capabilities/detached-spawn.md)；
  [agent-loop 活文档 § 分离式 spawn](../architecture/agent-loop.md)；ADR 0015 / 0017（规则①可观测）

## 背景

detached spawn 的准入有三道门：目标存在、`DispatchPolicy`、K1 配额。`spawn_driver.spawn_skill` 的 docstring 自己
写着「reject 分类细化留待后续 task」，ADR 0017 把它列为保留项。在 main（c3aa46b）核对现状：

1. **拒绝以普通异常冒出**：未知 skill / 策略拒绝抛裸 `ValueError`，分类只存在于消息文本里。
2. **经 `spawn_skill` 工具时被当作工具故障**：异常上抛到 tool runtime，模型看到
   `tool_error: dispatch_rejected: not_in_whitelist`，`data.reason == "exception"`；日志里是一段 traceback。
   一次预期内的准入拒绝与工具自身崩溃无法区分。
3. **事件流没有拒绝事件**：`skill_spawn_rejected` 只在 `call_skill` 的配额拒绝时发出，且 data 只有
   `limit_kind` / `limit`，没有统一的分类字段。

## 决策

1. **稳定分类 `SpawnRejectReason`**：`unknown_skill` / `max_depth_exceeded` / `cycle_detected` /
   `not_in_whitelist` / `cannot_call_entry_skill`（结构性门控，取值与 `DispatchVerdict.reason` 一致）+
   `spawn_limit_concurrent` / `spawn_limit_total`（配额）。
2. **异常类型携带分类，不改既有捕获方式**：
   - 结构性拒绝抛 `SpawnRejectedError(ValueError)`，消息前缀不变，另带 `reject_reason` / `skill_id` / `path`；
   - 配额拒绝仍抛 `SpawnLimitError`，新增 `reject_reason` 属性；`kind` 只接受 `concurrent` / `total`。
3. **`spawn_skill` 工具把准入拒绝当作结果**：捕获这两类异常，返回
   `ToolResult.error("spawn_rejected: <reason> (...)", reason=<reason>, ...)` 并 emit `skill_spawn_rejected`。
   其余异常照常上抛——只有准入拒绝是「预期内」的。
4. **`skill_spawn_rejected` 统一 data 形状**：`skill_id` / `call_id` / `reason` / `origin` / `path`，配额拒绝
   另带 `limit_kind` / `limit`。`call_skill` 的配额拒绝补上 `reason` / `origin` / `path`，既有键保留。
5. **事件由工具层发出**，不在 `spawn_driver` 里发：业务直接调 `engine.spawn_skill` 时异常本身就是信号，
   调用方就在栈上；需要事件的是模型经工具发起的场景，那里才有 `call_id` 与所属 submission。

## 替代方案

- **统一改抛一种新异常**（配额也并入 `SpawnRejectedError`）：`SpawnLimitError` 已被测试与业务按类型捕获并读
  `kind` / `limit`；改类型是无收益的破坏；否决。
- **在 tool runtime 的通用异常处理里识别这两类异常**：runtime 不应认识某个具体工具的领域异常；否决。
- **`call_skill` 的结构性拒绝也发 `skill_spawn_rejected`**：它已经以 `dispatch_rejected` 工具结果回给模型并带
  `data.reason`，且会伴随 `tool_call_completed`；再发一条事件是重复信号。事件保留给「占用 / 申请 spawn 资源
  被拒」的语义；否决。

## 后果

- 模型经 `spawn_skill` 看到的拒绝文本从 `tool_error: dispatch_rejected: <reason>` 变为
  `spawn_rejected: <reason> (skill '<id>')`；依赖旧文本做匹配的业务需更新。
- `skill_spawn_rejected` 的消费者会开始收到来自 `spawn_skill` 的事件，并在所有事件上看到 `reason` /
  `origin` / `path`。只读 `limit_kind` / `limit` 的消费者需先判断键存在（结构性拒绝不带这两个键）。
- `SpawnLimitError("weird", n)` 现在在构造期抛 `ValueError`。
- R1–R5：R1 无业务概念；R2 不涉及；R3 本决策即补可观测缺口；R4 / R5 不涉及。

## 验证

`tests/loop/test_spawn_reject_classification.py`：engine API 两类结构性拒绝的异常类型、消息、分类与路径；
`SpawnLimitError` 的分类与非法 kind；经工具的未知 skill / 白名单外 / 并发配额三种拒绝的模型可见文本、
事件 data、无异常日志、turn 正常完成；handler 级结构化结果；非准入异常照常上抛；非法分类构造期拒绝。
既有 `tests/loop/test_detached_spawn.py` / `test_spawn_registry.py` 未改动即通过。
