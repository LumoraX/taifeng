## Why

skill 是 markdown 目录，但 `FilesystemSkillRegistry` 的目录在构造时就定死了，`EnginePool.create` 也只接受一个 `skills_dir`。插件（或宿主）无法在运行期挂上、摘掉一组 skill。

## What Changes

- `FilesystemSkillRegistry` 新增 `add_dir(path, *, source)`，返回可撤销的 handle。
- `add_dir` 遇到 skill id 重名直接报错，不沿用初始化时的「后加载覆盖先加载」。
- 增删之后刷新 watcher 基线，并让活跃 engine 收到 `RefreshSnapshot`。
- `PluginContext` 增加 `skills` capability。

## Capabilities

### New Capabilities

- `skill-dir-registration`：运行期增删 skill 目录。

### Modified Capabilities

无。初始化时多目录的覆盖语义保持不变。

## Non-goals

不把 `add_dir` 加进 `SkillRegistry` Protocol；不支持非文件系统的 skill 来源；不改 `SkillSource` 的三个取值；不改 `SkillTrustPolicy`。

## R1–R5 影响

- R1：无业务字段。
- R2：skill 列表进 prompt 前缀，每次增删会让活跃会话的前缀缓存失效一次。插件路径下增删只发生在首个 engine 之前与 pool close 时，无活跃会话。契约文档必须写明这一点。
- R3：沿用既有的 skill 快照刷新事件，不新增事件。
- R4：`add_dir` 与撤销都是一次 discover，无新增长时操作。
- R5：入口 skill 所在目录缺席时恢复会话的现有行为尚未核实，本变更先用测试固定现状再写进契约。

## Impact

实现：`src/taifeng/skill/registry.py`、`src/taifeng/skill/watcher.py`、`src/taifeng/plugin/context.py`、`src/taifeng/loop/pool_plugins.py`。

测试：`tests/skill/test_registry_add_dir.py`、`tests/plugin/test_context.py`、`tests/loop/test_pool_plugins.py`。

文档：`docs/architecture/skill-system.md`、`docs/architecture/capabilities/plugin-system.md`。

注册表部分（任务 1–2）不依赖 `plugin-core`，可并行开工；context 接线（任务 3）依赖它。
