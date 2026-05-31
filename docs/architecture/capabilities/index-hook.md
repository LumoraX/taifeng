# index-hook Specification

## Purpose
TBD - created by archiving change store-protocol-decoupling. Update Purpose after archive.
## Requirements
### Requirement: IndexHook 协议形状

系统 SHALL 提供 `IndexHook` 作为 `typing.Protocol` 且 `runtime_checkable`，方法签名固定为：

- `async def on_thread_created(meta: ThreadMetadata) -> None`
- `async def on_message_appended(thread_id: str, items: list[ResponseItem]) -> None`
- `async def on_metadata_updated(thread_id: str, patch: dict) -> None`

#### Scenario: 业务实现可通过 isinstance 校验
- **WHEN** 业务实现以上 3 个 async 方法
- **THEN** `isinstance(instance, IndexHook)` SHALL 返回 True

### Requirement: NoopIndexHook 默认实现零成本

系统 SHALL 提供 `NoopIndexHook` 作为默认实现，三方法都 `pass`，零开销。

#### Scenario: 默认未传 hook 时使用 NoopIndexHook
- **WHEN** 构造 `EnginePool.create(...)` 未传 `index_hook` 参数
- **THEN** Engine 内部 SHALL 使用 NoopIndexHook 实例

#### Scenario: NoopIndexHook 三方法可直接 await
- **WHEN** `await NoopIndexHook().on_thread_created(meta)`
- **THEN** 调用 SHALL 立即返回 None，无异常

### Requirement: fire-and-forget 调用

当 `MessageWriter.create_thread` / `MessageWriter.append` / `ThreadDirectory.update_metadata` 任一方法完成，系统 SHALL spawn background task 调用对应 IndexHook 方法，SHALL NOT 阻塞调用方法的返回。

#### Scenario: append 完成立即返回不等 hook
- **WHEN** hook.on_message_appended 内部 `await anyio.sleep(2.0)`，业务调用 `await writer.append(tid, [item])`
- **THEN** append 调用耗时 SHALL < 0.1s（不等 hook 完成）
- **AND** 2s 后 hook 内部记录的调用计数 SHALL 增 1

#### Scenario: 主路径错误不连带 hook
- **WHEN** writer.append 自身抛 IOError
- **THEN** hook 的 on_message_appended SHALL NOT 被调用

### Requirement: hook 失败不影响主路径

当 IndexHook 方法抛任何异常，系统 SHALL 捕获异常并发 EventMsg `index_hook_failed`（含 `method / thread_id / cause_repr`），主路径（MessageWriter 写 / ThreadDirectory 写 / engine 主循环）SHALL 不受影响。

#### Scenario: hook 抛 RuntimeError
- **WHEN** 某 IndexHook 的 on_thread_created 内部 `raise RuntimeError("boom")`
- **THEN** writer.create_thread 已正常返回 thread_id
- **AND** 事件流 SHALL 含一条 `index_hook_failed`（method="on_thread_created", cause_repr 含 "RuntimeError: boom"）
- **AND** 当前 turn SHALL 继续正常运行直至 TurnComplete

#### Scenario: hook 抛 CancelledError 也不传播
- **WHEN** hook 内部 raise `anyio.get_cancelled_exc_class()`
- **THEN** 主路径 SHALL 不被 cancel；事件流 SHALL 含 `index_hook_failed`

### Requirement: shutdown 等待 pending hooks 有 grace period

当 `engine.shutdown()` 被调用，系统 SHALL await 所有 in-flight hook task，最多 5 秒；5s 后未完成的 hook task SHALL 被 cancel + 为每个被 cancel 的 task 发 EventMsg `index_hook_abandoned`（含 `method / thread_id`）。

#### Scenario: 1s 内完成的 hook 在 shutdown 前等到
- **WHEN** hook 内部 `await anyio.sleep(1.0)` 后写文件，spawn 后立即调 `engine.shutdown()`
- **THEN** shutdown 调用 SHALL 等约 1s 返回
- **AND** hook 写的文件 SHALL 存在

#### Scenario: 10s 卡住的 hook 在 5s 后被 cancel
- **WHEN** hook 内部 `await anyio.sleep(10.0)`，spawn 后立即调 `engine.shutdown()`
- **THEN** shutdown 调用 SHALL 在约 5s 返回（不等 10s）
- **AND** 事件流 SHALL 含 `index_hook_abandoned`

### Requirement: hook spawn 顺序与发起顺序一致

同一 thread 上 create_thread 后立即 append，两个 hook 任务 SHALL 按发起顺序 spawn（`on_thread_created` 先 spawn），但 SHALL NOT 保证 hook 实际完成顺序（业务侧自决要不要内部加锁）。

#### Scenario: create 先于 append 的 spawn
- **WHEN** spy hook 在两个方法内分别 append spawn 时间戳到 list，业务依次调 `await writer.create_thread(...)`、`await writer.append(tid, [item])`
- **THEN** spawn 时间戳列表中 on_thread_created 的时间 SHALL ≤ on_message_appended 的时间

### Requirement: hook 协议违反在构造期失败

当传入 EnginePool 的 IndexHook 对象不满足 Protocol（缺方法 / 方法非 async），Engine SHALL 在构造时 raise `TypeError`（不延迟到运行时）。

#### Scenario: 缺 on_metadata_updated 方法
- **WHEN** `EnginePool.create(..., index_hook=只有两个方法的对象)`
- **THEN** 构造 SHALL raise `TypeError`，错误信息提及缺少的方法名

