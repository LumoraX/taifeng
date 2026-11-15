## Context

来源：计划 r2 的 Task 5。分支 `feat/skill-dir-registration`。r1 把 `SkillSource` 当成可插拔对象，而它是 `skill/definition.py` 里的信任层级 `Literal["system", "user", "marketplace"]`；真正缺的是「可增删的目录」。

## Goals / Non-Goals

目标是一个安全的运行期目录增删 API，重名不产生静默替换，撤销后不残留。不引入新的 skill 来源抽象。

## Decisions

1. **只加在 `FilesystemSkillRegistry` 上**。`async add_dir(path, *, source) -> RegistrationHandle`。pool 的注册表不是该类型时，`ctx.skills.add_dir` 抛 `PluginCapabilityError`，不静默跳过。
2. **`source` 必填，无默认值**。它决定 `SkillDefinition.source` 并喂给 `SkillTrustPolicy`，默认成 `user` 会让插件 skill 悄悄拿到用户级信任。
3. **重名拒绝**。先单独加载新目录，与当前快照比对 id；有交集抛 `SkillIdCollisionError`，且不修改 `_skills_dirs`、不递增版本。理由：覆盖意味着插件能替换宿主 skill，撤销后旧 skill 又会「复活」。初始化时的多目录覆盖语义不动。
4. **撤销**。handle `aclose()` 幂等：从 `_skills_dirs` / `_sources` 移除后 `discover()`。构造时传入的目录没有 handle，不可移除。
5. **并发**。`add_dir` 与撤销用注册表内的一把 `asyncio.Lock` 串行化，避免两次 discover 交错。
6. **watcher**。`SkillFileWatcher._scan_mtimes` 每轮读 `_skills_dirs`，新目录自动纳入；增删后重置它的 mtime 基线，避免紧接着多触发一次重复 discover。
7. **活跃会话**。`pool_plugins` 包一层：`add_dir` / 撤销成功后向当时所有活跃 engine 提交 `RefreshSnapshot`，与 `pool_session.start_skill_watcher` 的 `_on_change` 同一做法，不依赖 `auto_watch_skills`。
8. **移除语义**。运行中的 turn 持有不可变快照，移除在下一 turn 生效。活跃 engine 的入口 skill 位于待移除目录时拒绝移除。插件路径下撤销只发生在 pool close，届时无活跃 engine，这条检查是给直接使用注册表 API 的宿主的。

## Risks

- 决策 3 与初始化时的覆盖语义不一致，文档必须把两者并列说清。
- 决策 8 需要注册表知道「谁在用哪个入口 skill」，这个信息在 pool 而不在注册表；检查应放在 `pool_plugins` 的包装层，注册表本身只做目录操作。
