# ADR 0119：Resume 领域事件作为提交终态

状态：Accepted

## 决策

部分核销、拒绝与根中止均必须收尾订阅。复用领域事件；中止的 suspension_resolved 增加 resumed=false。子链核销继续是中间态，不创建虚构 turn。

## 契约

见 [提交生命周期](../architecture/capabilities/submission-lifecycle.md)。
