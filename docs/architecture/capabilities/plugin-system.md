# Capability: plugin-system

> 状态：🧪 实验层（`taifeng.experimental`）。决策：[ADR 0144](../../decisions/0144-plugin-lifecycle-and-scope.md)、[ADR 0145](../../decisions/0145-plugin-backend-providers.md)（两段加载）。
> 交付分期见 `openspec/changes/plugin-*`；本契约随各期合入逐步补全，**只写已实现的现状**。

## Purpose

业务系统把扩展写成插件包，由宿主显式交给 `EnginePool.create(plugins=...)`，而不是修改内核。
内核负责插件的身份、依赖顺序、生命周期、回滚、取消与可观测；插件只能经 `PluginContext`
上显式列出的 capability 接入内核。

已交付：插件核心（生命周期 + pool 接线）、`ctx.tools`、`ctx.hooks`、`ctx.skills`、`ctx.telemetry`、存储后端（`provide_backends`）。

## 数据契约

### `PluginSpec`（frozen dataclass）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | `str` | 插件身份，pool 内唯一；非空 |
| `version` | `str` | 插件版本，只用于事件与诊断；非空 |
| `requires` | `tuple[str, ...]` | 依赖的插件 id，默认 `()`；不得包含自身 |
| `scope` | `Literal["pool"]` | 作用域，第一版只有 `pool` |

### `Plugin`（Protocol）

```python
class Plugin(Protocol):
    spec: PluginSpec
    async def setup(self, ctx: PluginContext) -> None: ...
    # 可选：async def close(self) -> None
    # 可选：def provide_backends(self, backends: PluginBackends) -> None   （ADR 0145，见「存储后端」）
```

- `setup` 不返回值。插件经 context 做的每个注册由 context 自动登记，manager 负责撤销。
- `close()` 可选（manager 用 `getattr` 探测），在该插件的注册被撤销**之前**调用。

### `RegistrationHandle`（Protocol）

只有幂等的 `async aclose()`。由 context 内部创建并登记到 manager，插件无需保存。

### `PluginContext`

| 成员 | 含义 |
| --- | --- |
| `plugin_id` | 当前插件 id |
| `cancel` | 由 pool 根令牌派生的 `CancellationToken`（`pool/plugin:<id>`） |
| `tools.register(spec) -> RegistrationHandle` | 向 pool 共享 `ToolRegistry` 注册工具（见下） |
| `hooks.register(kind, handler) -> RegistrationHandle` | 向 pool 的 `taifeng.hooks.HookRunner` 注册钩子（见下） |
| `await skills.add_dir(path, *, source) -> RegistrationHandle` | 挂上一个 skill 目录（见下） |
| `telemetry.add_sink(sink) -> RegistrationHandle` | 订阅 pool 级事件（见下） |

不暴露 engine、runner、prompt、history 或任何业务对象。`setup` 返回后 context **封口**，
之后的注册抛 `PluginCapabilityError`。

### `PluginSnapshot`（frozen dataclass）

`state: PluginManagerState`、`loaded_ids: tuple[str, ...]`（按加载顺序）。

`PluginManagerState`：`new → loading → loaded → closing → closed | closed_with_errors`；
加载失败回滚后为 `closed` 或 `closed_with_errors`。

### 错误（均继承 `PluginError`，带稳定 `code`）

| 类 | `code` | 何时 |
| --- | --- | --- |
| `DuplicatePluginError` | `duplicate_plugin` | 两个插件 id 相同 |
| `MissingPluginDependencyError` | `missing_dependency` | `requires` 指向未传入的插件 |
| `PluginDependencyCycleError` | `dependency_cycle` | 依赖成环（含自依赖） |
| `PluginSetupError` | `setup_failed` | 某插件 setup 抛错或被取消；`__cause__` 为原始异常 |
| `PluginCloseError` | `close_failed` | 关闭时有插件出错或超时（manager 不向 pool 抛，只记录） |
| `PluginCapabilityError` | `capability_denied` | context 封口后注册，或 capability 不可用 / 冲突 |
| `PluginStateError` | `invalid_state` | 在非 `new` 状态下再次 `load` |

错误消息只含插件 id、capability 名与 code，不含配置正文。

### 生命周期事件

| kind | 何时 | `data` |
| --- | --- | --- |
| `plugin_loaded` | 某插件 setup 成功 | `plugin_id`, `version`, `scope`, `duration_ms` |
| `plugin_failed` | 某插件 setup 失败（含取消） | 上述四项 + `failure_class`（异常类名）+ `rollback_errors`（int） |
| `plugin_unloaded` | 某插件被撤销（正常关闭或回滚） | 上述四项 + `failure_class`（成功为 `None`） |

事件以 `EventMsg(submission_id="*")` 交给 pool 的 `sink=`（与持久化层事件同一通道），`seq=0`——
此时没有 engine，不经 engine `_emit`。未配置 `sink` 时只记日志。事件**不携带**插件配置、
工具参数或外部响应。

### capability：`tools`

- 直接调用 pool 共享的 `ToolRegistry.register`，所有 engine 经既有 `tool_set_changed` 机制看见它；
  `ToolRegistry` 自身 API 与 `version` 语义不变。插件工具与其他工具一样受 skill 的 `tool_names`
  白名单约束。
- **重名一律报错**：与内置工具、`extra_tools`、其他插件或 MCP 已注册的同名工具冲突时抛
  `PluginCapabilityError`（`__cause__` 为 `DuplicateToolError`），不覆盖。插件先占名后，MCP 再注册
  同名工具时由 MCP 侧拿到既有 `DuplicateToolError`。插件不能 `replace`。
- 撤销只移除**仍是本插件注册的那个对象**；若已被他人 `replace`，撤销报错（计入关闭错误），
  不误删别人的工具。

### capability：`hooks`

- 目标是 `EnginePool.create(hooks=...)` 的 `taifeng.hooks.HookRunner`（不是索引钩子 runner）。
  宿主没传 `hooks` 时，首个插件注册钩子那一刻新建一个 `HookRunner` 交给 pool（尚无 engine，
  engine 创建时才读取它）；没有插件注册钩子时 `hooks` 保持 `None`。
- 宿主传了别的钩子对象（非 `HookRunner`）时，`ctx.hooks.register` 抛 `PluginCapabilityError`。
- 多个插件的同类钩子按加载顺序串行执行；撤销按对象身份（`HookRegistry.unregister`），
  执行中的一轮不受影响。

### capability：`skills`

- 委托 `FilesystemSkillRegistry.add_dir(path, *, source)`（[skill-system](../skill-system.md)）：`source` 必填
  （`system` / `user` / `marketplace`，决定信任层级），skill id 与已加载 skill **重名即拒绝**
  （`PluginCapabilityError`，`__cause__` 为 `SkillIdCollisionError`），不产生新快照。
- 成功后让所有活跃 engine 提交 `RefreshSnapshot`（不依赖 `auto_watch_skills`）；插件加载期没有 engine，
  是空操作。
- 撤销时若某活跃 engine 的入口 skill 来自该目录则拒绝；插件路径下撤销只在 pool 关闭时发生，
  届时已无活跃 engine。运行中的 turn 持有不可变快照，增删在下一 turn 生效。
- 目录自成一体：`child_skills` 只能引用**同目录**内的 skill（loader 按目录校验），插件 skill 不能把宿主
  skill 当子 skill。
- pool 的 skill 注册表不是 `FilesystemSkillRegistry` 时不可用。

### capability：`telemetry`

- 传了 `plugins` 时，`create` 一开头就把 `sink=` 包成 `FanoutSink`（`taifeng.telemetry.fanout`），宿主 sink
  排第一；索引钩子 runner 与 SQLite 目录拿到的就是这个 fan-out。未传 `plugins` 时 `sink` 原样透传，对象身份不变。
- 插件 sink 收到的是 **pool 级事件**：插件生命周期事件、索引钩子失败、持久化层事件——与宿主 `sink=` 今天收到的
  范围一致。engine 的 turn / tool 事件流**不**转发（宿主经 `engine.subscribe_all()` 获取）。
- 只收到加入**之后**的事件，不补发。关闭时插件的 sink 随其注册一起撤销，因此收不到自己的 `plugin_unloaded`。
- 成员之间故障隔离：单个 sink 抛错只记日志，不影响其他成员与上游；取消照常传播。同一 sink 重复加入抛
  `PluginCapabilityError`。审计 pool 同样提供本 capability（不影响静态门）。

### 存储后端：`provide_backends`（两段加载第一段，ADR 0145）

插件可选实现**同步**方法 `provide_backends(backends: PluginBackends)`，只声明工厂、不做 IO。`create` 在构造任何
后端之前按加载顺序收集声明并调用工厂；`setup` 仍在原加载点。

| 登记方法 | 等价的宿主参数 | 所有权 |
| --- | --- | --- |
| `backends.message_store(factory)` | `message_store=` | pool 拥有：`create` 中途失败由内核关闭；`pool.close()` 时关闭，晚于插件 close |
| `backends.thread_directory(factory)` | `thread_directory=` | 与宿主注入相同，pool 不关闭 |
| `backends.audit(factory)` | `audit=`（整份 `AuditConfig`，含 journal core） | 与宿主注入相同 |
| `backends.attachment_store(factory)` | `attachment_store=`（ADR 0148） | 与宿主注入相同，pool 不关闭；产物须实现 `AttachmentStore` 协议，否则 `TypeError` |

- 每项每个 pool 至多一个提供者；两个插件都提供，或宿主已显式传了对应参数（`message_store` 还包括
  `storage_dir` / `threads_dir`），抛 `PluginCapabilityError`——在调用任何工厂之前。
- 工厂抛错 → `PluginSetupError`（`plugin_id` 为提供者，`__cause__` 为原始异常），已构造的 store 被关闭。
- 插件 store 走与宿主注入相同的 `validate_injected_store`；插件 `AuditConfig` 走同一个审计静态门，审计 pool 照旧
  拒绝插件注册 tools / hooks / skills。
- `provide_backends` 返回后登记器封口，运行中不可替换持久化真相源。
- 恢复会话时必须加载同一个提供者插件，否则读不到历史（与更换 `message_store=` 同理）。

### capability 不可用的情形

- **审计 pool（`audit=`）**：工具、钩子与 skill 快照在 pool 构造时经静态门校验，插件之后再注册会绕过它，
  因此 `tools` / `hooks` / `skills` 均抛 `PluginCapabilityError`，原因写在错误信息里。
- 不经 pool、直接 `PluginManager()` 构造时不提供任何 capability。

## 行为契约

1. **显式注入**：只接受 `EnginePool.create(plugins=...)`，默认 `()`。未传时不创建 manager、
   不发插件事件，pool 行为与引入插件前一致。不扫描 entry points。
2. **加载点**：pool 构造完成之后、`create` 返回之前；此时工具注册表、skill 注册表、根取消令牌
   已就绪，尚无 engine。
3. **加载前校验**：重复 id、缺依赖、依赖环在任何 `setup` 运行前抛错。
4. **顺序**：Kahn 拓扑序；同一层按传入顺序，结果确定。
5. **回滚**：某插件 `setup` 失败时，先撤销它自己已做的注册，再按逆加载序关闭此前成功的插件。
   `create` 抛 `PluginSetupError`（原始异常为 `__cause__`），并走既有失败路径关闭 pool；
   回滚过程中的错误只计入 `plugin_failed.rollback_errors` 并记日志。
6. **取消（R4）**：`setup` 在 `interrupt_on_cancel(ctx.cancel)` 内运行；令牌被取消时 setup 被打断，
   按第 5 条回滚，`create` 抛 `PluginSetupError`。
7. **关闭**：`pool.close()` 在全部 Session 强制释放之后、停 skill watcher 与关 store 之前，
   按逆加载序关闭插件：先 `Plugin.close()`，再逆序 `aclose()` 它的注册。`close()` 与每次 `aclose()`
   各受 `close_timeout_seconds`（默认 5 秒）约束；超时或出错不阻断其余插件，manager 进入
   `closed_with_errors` 并在全部关闭后抛 `PluginCloseError`——pool 按既有「保留首错」语义并入，
   照常清理其余资源后由 `pool.close()` 外抛。重复关闭幂等。
   取消令牌：关闭插件时 pool 根令牌**尚未**取消（插件收尾仍可正常 await），pool 关闭完成后才取消。
8. **不支持运行中卸载**：第一版注册只在 pool 关闭（或加载回滚）时撤销；那时已无活跃 Session，
   不存在在途调用。

## R1–R5 影响

- **R1**：`taifeng.plugin` 只含通用机制，无业务字段、不读环境变量。
- **R2**：工具集与 skill 列表都进 prompt 前缀。插件在首个 engine 之前注册、pool 关闭时撤销，两个时点都
  没有活跃会话，不打破缓存。直接调用 `FilesystemSkillRegistry.add_dir` / 撤销的宿主会让活跃会话的前缀
  缓存失效一次。
- **R3**：三个生命周期事件；Console 有专用标签（`plug`），JSONL 原样落盘。OTel 走通用事件路径——pool 级事件没有
  turn span 可挂，与其他 pool 级事件（索引钩子失败、持久化层事件）一样不产生 span 事件。
- **R4**：见行为契约第 6、7 条。
- **R5**：插件不持久化任何状态，不改 `MessageStore`。恢复一段含插件工具调用的会话而插件缺席时，
  历史照常重放，该工具**不再提供**给模型；模型若仍幻觉调用，走既有 `not_offered` 拒绝路径
  （is_error 结果、turn 不中断）。入口 skill 来自缺席插件目录时，`get_or_create(resume_thread_id=...)`
  抛 `ValueError("unknown skill: ...")`，不静默换入口。

## 不做的事（第一版）

自动发现、热重载、运行中卸载单个插件、`engine` / `session` scope、不经 pool 直接构造的
`AgentEngine`、把 engine 事件流转发给插件 sink。

## 验证

`tests/plugin/test_context.py`、`tests/plugin/test_manager.py`、`tests/plugin/test_capabilities.py`、
`tests/loop/test_pool_plugins.py`、`tests/telemetry/test_plugin_events.py`、`tests/hooks/test_registry_unregister.py`、
`tests/skill/test_registry_add_dir.py`、`tests/telemetry/test_fanout.py`、`tests/plugin/test_backend_capabilities.py`。
