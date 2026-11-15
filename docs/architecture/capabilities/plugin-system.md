# Capability: plugin-system

> 状态：🧪 实验层（`taifeng.experimental`）。决策：[ADR 0144](../../decisions/0144-plugin-lifecycle-and-scope.md)。
> 交付分期见 `openspec/changes/plugin-*`；本契约随各期合入逐步补全，**只写已实现的现状**。

## Purpose

业务系统把扩展写成插件包，由宿主显式交给 `EnginePool.create(plugins=...)`，而不是修改内核。
内核负责插件的身份、依赖顺序、生命周期、回滚、取消与可观测；插件只能经 `PluginContext`
上显式列出的 capability 接入内核。

当前已交付：**插件核心**（生命周期 + pool 接线）。`PluginContext` 暂只有 `plugin_id` 与
`cancel`，尚无可注册的 capability——tool / hook / skill 目录 / sink 由后续变更逐个加入。

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
- **R2**：核心不向 prompt 注册任何内容，不触碰压缩与缓存。
- **R3**：三个生命周期事件；Console 有专用标签（`plug`），JSONL 原样落盘。OTel 走通用事件路径——pool 级事件没有
  turn span 可挂，与其他 pool 级事件（索引钩子失败、持久化层事件）一样不产生 span 事件。
- **R4**：见行为契约第 6、7 条。
- **R5**：插件不持久化任何状态，不改 `MessageStore`。

## 不做的事（第一版）

自动发现、热重载、运行中卸载单个插件、`engine` / `session` scope、不经 pool 直接构造的
`AgentEngine`、存储后端 capability（见 `openspec/changes/plugin-backend-providers`）。

## 验证

`tests/plugin/test_context.py`、`tests/plugin/test_manager.py`、`tests/loop/test_pool_plugins.py`、
`tests/telemetry/test_plugin_events.py`。
