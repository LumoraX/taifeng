# Offload 存储一致性实施计划

> **For agentic workers:** 使用 executing-plans 逐项执行，契约和审查先于实现；用户已授权完成测试与真实回归。

**Goal:** offload 与文件工具共享 WorkspaceFS，保留本机兼容与无损回溯。

**Architecture:** OffloadStrategy 使用 workspace_for 归一化后端，write_bytes 写入，remove 清理。算法、stub 与 cache 契约保持不变。

**Tech Stack:** Python 3.12/3.13、anyio、pytest、SimClient、WorkspaceFS。

## 1. 契约与设计

- [x] 1.1 更新 compaction-offload-strategy / workspace-fs 契约，提交 ADR 0131、proposal、design 与本计划，独立审查设计。Acceptance：`openspec validate offload-workspace-consistency --strict` 通过，契约先于 src 提交。

## 2. 工作区接入（TDD）

- [x] 2.1 `tests/context/test_offload_workspace.py` 增加 MemoryWorkspace 正常写读、参数互斥、无效协议测试，先跑出因缺失 workspace 参数的红测试。Acceptance：`PYTHONPATH=src uv run pytest tests/context/test_offload_workspace.py -q` 确认预期失败。
- [x] 2.2 修改 `OffloadStrategy.__init__/_offload_one/cleanup_thread`：`self._workspace = workspace_for(file_root, workspace)`、`await self._workspace.write_bytes(rel_path, output.encode('utf-8'))`、`await self._workspace.remove(rel_path, recursive=True)`；维持安全 ID 和父目录校验。Acceptance：新增与既有 offload / workspace 测试全绿后提交。
- [x] 2.3 测试失败保留原文且继续、等待时取消、路径与 symlink 越界、空输入与超长 Unicode、并发、清理隔离和 JSONL 冷恢复。Acceptance：`PYTHONPATH=src uv run pytest tests/context/test_offload*.py tests/tool/test_workspace_fs.py -q` 全绿；已有深度/环测试由全量套件验证。

## 3. 真实验证场景与文档

- [ ] 3.1 新增 `examples/real_llm/_offload_workspace.py`、对应 skill，接入 capability_matrix / selfcheck，核验中段口令只能由分页回读取到。Acceptance：selfcheck 全绿，故障后端无法产出压缩成功证据；场景文件小于 800 行。
- [ ] 3.2 更新 context-compression、能力矩阵、旋钮和文档索引。Acceptance：示例使用同一 workspace；稳定导出快照无新增符号；全部实现提交先于真实台账提交。

## 4. 门禁、真实回归与审查

- [ ] 4.1 Python 3.12 与 3.13 全量 `pytest tests/`，`scripts/verify_examples.py`，`ruff check --select F,S108,I,TC`，`mypy src/`。Acceptance：全绿，依赖带 dev/litellm/telemetry-otel。
- [ ] 4.2 先 `examples/real_llm/selfcheck.py`，再全量 `examples/real_llm/capability_matrix.py`，检查生成的 `docs/real-llm-ledger.{json,md}`。Acceptance：全量 PASS，offload_workspace 含实际回读与答案取证；台账 commit 新于基础层实现。
- [ ] 4.3 独立代码审查、处理发现并完成分支收尾。Acceptance：无未处理阻断项、任务状态与验证证据一致；保留独立 worktree 供用户审阅。
