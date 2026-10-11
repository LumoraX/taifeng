# ADR 0118：挂起取消的交接所有权

状态：Accepted

## 决策

pending 保持到 runner 的 history 回写结束。挂起事件发出后的取消由同一所有者处理，避免 marker 被回写覆盖。成功发出 suspension_cancelled 并撤销 TTL。审计路径不扩展挂起取消。

## 契约

见 [提交生命周期](../architecture/capabilities/submission-lifecycle.md)。
