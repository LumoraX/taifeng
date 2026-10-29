## Why

文件工具已能经 WorkspaceFS 访问容器或远端工作区，但 OffloadStrategy 仍直接写本机目录。两者装配到不同存储后，模型收到的 stub 无法被 file_read 回读，破坏无损回溯保证。

## What Changes

- OffloadStrategy 接受与文件工具相同的 `workspace=`，与兼容入口 `file_root=` 恰好二选一。
- 写入与 thread 清理统一经 WorkspaceFS；确认完整写入成功后才生成 stub。
- 保留相对路径、UTF-8 内容、分页、cache anchor、失败回退和 JSONL resume 语义。
- 增加非本机工作区、路径边界、取消、失败隔离、冷恢复测试及真实模型回读场景。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `compaction-offload-strategy`：写入、清理和回读共享工作区；保留本机参数兼容。

## Non-goals

不新增 ArtifactStore、不扩展 WorkspaceFS 七成员协议、不引入具体远端后端；不增加 TTL、流式 IO、共享状态或其他内核候选能力。

## R1–R5 影响

- R1：复用可注入协议，后端由宿主提供，无业务字段。
- R2：仅改 anchor 后 tail，返回 cache_invalidated=False 与原 anchor。
- R3：继续通过 compaction_completed.detail 提供 offloaded / bytes_saved；IO 失败记录 warning。
- R4：每条写入及清理前有协作取消检查点，后端等待可取消，取消不得降为 IO 失败。
- R5：stub 与确定性路径不变；恢复时宿主重新注入同一持久工作区。

## Impact

实现：`src/taifeng/context/strategies/offload.py`。测试：`tests/context/test_offload_workspace.py`、既有 offload 与 WorkspaceFS 套件。真实验证：`examples/real_llm/` 的能力矩阵、selfcheck、生成台账。

活文档：`docs/architecture/capabilities/compaction-offload-strategy.md`、`workspace-fs.md`、`docs/architecture/context-compression.md`；同步 `docs/capability-matrix.md`、`docs/configurable-knobs.md` 和文档索引，新增 ADR。
