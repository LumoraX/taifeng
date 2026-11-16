# plugin-isolation-and-release 实施计划

> 前置：`plugin-core`、`plugin-tool-hook-registration`、`skill-dir-registration`、`telemetry-fanout-sink`。分支 `feat/plugin-isolation-and-release`。

## 1. 隔离与边界
- [ ] 1.1 `tests/plugin/test_scope.py`：两个 pool 各加载同 id 插件，tool / hook / skill 目录 / sink 互不可见；关闭 pool A 后 pool B 四类注册仍在。Acceptance：`PYTHONPATH=src uv run pytest tests/plugin/test_scope.py -q` 全绿。
- [ ] 1.2 `tests/plugin/test_static_boundary.py`：扫描 `src/taifeng/plugin/`，拒绝 `tenant_id`、`user_id`、`os.getenv`、`os.environ`。Acceptance：测试绿；故意加一行 `os.getenv` 能被检出。

## 2. 示例
- [ ] 2.1 `examples/plugin_system/demo.py`：`ReadOnlyToolPlugin`、`JsonlAuditSinkPlugin`、`ExtraSkillsPlugin`，SimClient，带内成功标记。Acceptance：`PYTHONPATH=src uv run python examples/plugin_system/demo.py` 输出成功标记且无 `[TURN ✗]` / `isError=True` / `❌`。
- [ ] 2.2 `tests/examples/test_plugin_system.py` 与 `scripts/verify_examples.py`。Acceptance：`python scripts/verify_examples.py --list` 把它归入 sim 档；`python scripts/verify_examples.py` 全部通过。

## 3. 文档与登记
- [ ] 3.1 `docs/architecture/overview.md` 加 `plugin/` 模块；`agent-loop.md` 写加载点与关闭顺序；`skill-system.md` 确认 `add_dir` 已写入。
- [ ] 3.2 `docs/configurable-knobs.md` 登记 `plugins=`；`docs/architecture/public-api.md` 登记实验层新增名。Acceptance：`tests/loop/test_kernel_knobs_integration.py` 与 `tests/test_public_api.py` 全绿。
- [ ] 3.3 `docs/architecture/capabilities/README.md` 加索引；`docs/capability-matrix.md` 登记为实验能力（含「真实 LLM 验证」列）；`docs/README.md` 加 ADR 0144 与计划 / 契约链接。
- [ ] 3.4 `capabilities/plugin-system.md` 终稿：四个 capability、事件范围、冲突规则、缓存影响、恢复行为、第一版不做的事。

## 4. 真实回归与门禁
- [ ] 4.1 `examples/real_llm/capability_matrix.py` 加 `plugin_tool` 场景，断言分两路（pool 级 sink / engine 事件流）。先 `PYTHONPATH=src uv run python examples/real_llm/selfcheck.py`。Acceptance：selfcheck 通过；故意不加载插件时场景判 FAIL。
- [ ] 4.2 `PYTHONPATH=src uv run pytest tests/ -q`（3.12 与 3.13）、`uv run ruff check --select F,S108,I,TC src tests examples scripts`、`uv run --extra dev --extra litellm --extra telemetry-otel mypy src/`。Acceptance：全绿；所有新增 / 修改文件 ≤ 800 行。
- [ ] 4.3 `PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py` 全量，提交 `docs/real-llm-ledger.{json,md}`。Acceptance：`plugin_tool` PASS 且原有场景全 PASS；台账与源码同一 commit。
