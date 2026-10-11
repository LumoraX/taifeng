# ADR 0120：当前挂起记录的核销幂等

状态：Accepted

## 决策

先校验所有请求 ID，再跳过当前 record 内已核销的请求。全部已核销拒绝 already_resolved。不扫描已结束历史来改变拒绝原因。

## 契约

见 [提交生命周期](../architecture/capabilities/submission-lifecycle.md)。
