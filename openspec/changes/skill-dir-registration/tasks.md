# skill-dir-registration 实施计划

> 任务 1–2 无前置，可与 `plugin-core` 并行；任务 3 前置 `plugin-core`。分支 `feat/skill-dir-registration`。

## 1. 注册表 API
- [x] 1.1 `tests/skill/test_registry_add_dir.py` 红测试：`add_dir` 后 skill 可见且 `source` 正确、快照版本 +1；handle `aclose()` 后不可见、版本再 +1。Acceptance：观察红。
- [x] 1.2 红测试：id 重名抛 `SkillIdCollisionError` 且快照版本不变；`source` 缺省或非法被拒；目录不存在被拒；重复 `aclose` 幂等；并发两次 `add_dir` 结果确定。Acceptance：观察红。
- [x] 1.3 `skill/registry.py` 实现 `add_dir`、handle、`SkillIdCollisionError` 与内部锁。Acceptance：`PYTHONPATH=src uv run pytest tests/skill/test_registry_add_dir.py -q` 全绿；`registry.py` ≤ 800 行；初始化多目录覆盖的既有测试不变。
- [x] 1.4 红测试再实现：撤销后被其他 skill 引用的子 skill 变为不可达（`reachable_from` 更新）。Acceptance：测试绿。

## 2. watcher
- [x] 2.1 `skill/watcher.py`：增删后重置 mtime 基线。测试：开着 watcher 调 `add_dir`，只观察到一次快照变更。Acceptance：`tests/skill` 全绿。

## 3. 插件接线
- [x] 3.1 `tests/plugin/test_context.py` 红测试：`ctx.skills.add_dir(path, source=...)` 注册、pool close 后撤销；注册表非 `FilesystemSkillRegistry` 时抛 `PluginCapabilityError`。Acceptance：观察红→绿。
- [x] 3.2 `loop/pool_plugins.py` 包装层：成功后向活跃 engine 提交 `RefreshSnapshot`；入口 skill 在待移除目录时拒绝移除。Acceptance：`tests/loop/test_pool_plugins.py` 相关用例绿。
- [x] 3.3 先写测试固定现状：入口 skill 所在目录缺席时 `get_or_create` 恢复会话的行为。把观察到的行为写进契约；若是未分类异常，改为既有的分类错误。Acceptance：行为有测试且已入文档。

## 4. 验证
- [x] 4.1 更新 `docs/architecture/skill-system.md`（`add_dir`、重名规则与初始化覆盖语义的并列说明、R2 缓存影响）和 `capabilities/plugin-system.md`。
- [x] 4.2 `PYTHONPATH=src uv run pytest tests/ -q`、ruff 门禁、mypy strict。Acceptance：全绿。
- [ ] 4.3 任务 3 改了 `loop/`，合入前跑真实回归并提交台账。Acceptance：原有场景全 PASS。
