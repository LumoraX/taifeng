## Context

本轮在独立 worktree 和 feat/offload-workspace-consistency 分支实现。用户已确认先修复 offload 存储一致性、契约先行并完成测试与真实回归。

## Goals / Non-Goals

目标是让 offload 写入与 file_read 回读落在同一工作区，兼容原本机装配。范围限于存储一致性；不扩展压缩算法、调度器和存储后端。

## Decisions

1. 复用 WorkspaceFS，而不是新建 ArtifactStore 或复制本机文件到远端。前者避免协议重复，后者避免多份真相。`OffloadStrategy(file_root=None, workspace=None, ...)` 复用 workspace_for 归一化，恰好给一个。
2. 路径始终是 `_offload/{thread_id}/{call_id}`；对外部 ID 继续做安全路径段校验。用工作区 resolve 校验目标父目录与 thread 目录一致，再 write_bytes UTF-8 内容。不对远端标识做 pathlib 或本机 IO。
3. 写入失败保留原始 output，继续其他条目；取消向上传播。本机走 LocalWorkspaceFS 的原子写，外部后端沿用 WorkspaceFS 声明的原子性边界，不额外承诺事务或 CAS。
4. cleanup_thread 经同一后端 remove(recursive=True)，FileNotFoundError 为幂等；其他错误传播。无效 thread_id 在访问后端前拒绝并 warning。保留最终符号链接删除自身的既有工作区语义。
5. 不自动推断文件工具使用的后端。宿主显式把同一持久工作区传给 offload 与 reader，恢复时重新装配。协议不提供全局身份比较。
6. 新增真实 offload_workspace 场景：模型获取含隐藏中段随机口令的大结果，经 offload 后只能通过 stub 指向的工作区分页回读；driver 核验实际压缩事件、回读调用与最终答案。全量矩阵同时回归既有场景。

## Risks / Trade-offs

- 后端非原子写失败可能留下不完整文件 → 不生成新 stub；原文仍在，原子性由后端承诺。本机默认原子写。
- 取消恰好发生在写成功后 → 允许未被 stub 引用的完整文件，不发布悬空指针。
- WorkspaceFS 是整文件 IO → 本轮保持既有内存规模，不新增流式协议。
- 同名 call_id 并发写无 CAS → 沿用现有确定性路径与后端覆盖语义；不承诺不同内容的并发版本保存。

## Migration Plan

原 `file_root=` 无需迁移。远端接入改为同时对策略和 reader 传 `workspace=fs`，已持久化 stub 无需重写。代码回滚仅影响新增 workspace 参数装配，存储路径不变。

## Open Questions

无；具体外部后端与部署由宿主选择。
