# ADR 0131：offload 与回读共享 WorkspaceFS

- 状态：Accepted
- 日期：2026-10-29
- 关系：补充 ADR 0113 / 0129，保留既有 offload 路径与压缩语义。

## 背景

OffloadStrategy 直接写本机 file_root，文件工具已可用 WorkspaceFS 访问远端。策略与 reader 的存储不一致会发布无法回读的 stub。

## 决策

策略接受 workspace 或 file_root 恰好一个。复用 WorkspaceFS 的路径、写入与删除，不引入新后端协议。原 file_root 经 LocalWorkspaceFS 保持兼容，并获得同目录独占临时文件后的原子替换。宿主负责给策略和文件工具注入同一持久工作区。

ID 必须是安全路径段，后端负责边界校验。完整写入成功后才发布相对路径 stub；IO 失败保留原文并 warning，取消传播。清理只经工作区删除目标 thread，缺失幂等，非法 ID 不触碰后端。已有 cache、事件计数和 JSONL 恢复契约不变。

## 替代方案与边界

不新增 ArtifactStore：当前七成员协议已足够。不复制本机文件到远端：避免两份真相。本轮不增加 CAS、流式写或后端身份自动检查。外部后端的原子性沿用 WorkspaceFS 自身承诺；失败可能留下未被新 stub 引用的文件。参照 deepagents 随 backend offload 的范式，差异是复用 Taifeng 既有协议与 cache-aware 策略。
