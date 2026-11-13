# ADR 0141：短命存储资源治理与观测状态分类

状态：Accepted

日期：2026-11-14

## 决策

`EphemeralMessageStore` 默认使用有界资源配置：256 个 thread、单 thread 10,000 条消息、
实例总 100,000 条消息和 256 MiB canonical JSON 字节。超限写入在改变内存前抛出结构化
`EphemeralStoreLimitError`；同一 atomic batch 的幂等确认和冲突判断先于容量检查。

真实 LLM 台账保留旧 `verdict`，新增 `status` 与 `failure_class`。provider 限流、传输、内部
错误和不可靠终止归为 `TRANSIENT_FAILURE`；场景断言、协议和内核错误归为 `CODE_FAILURE`；
缺关键事件但有终态为 `PARTIAL`；凭据缺失继续写入 `NOT_EXECUTED`。

执行观测严格区分有效 usage 和估算上下文。只有有效 input/output usage 才计入 metered；
执行更新和所有 turn 终态报告 started/metered/missing 计数。没有 TurnRunner 的 gate 终态
使用零采样计数与 `usage_complete=true`，明确表示没有开始模型采样。

## 后果

短命执行达到内存边界时会显式失败，宿主可以通过 `usage()` 做监控。真实回归报告可以区分
服务临时故障与代码故障，避免过载场景阻塞无关修复。执行树终端可以显示缺失 usage 尝试数，
但不会把估算值当作真实 provider 用量。
