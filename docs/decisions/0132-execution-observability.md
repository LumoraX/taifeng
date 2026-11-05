# ADR 0132：以结构化事件支持执行树观测

- 状态：Accepted
- 日期：2026-11-06

## 背景

同步子调用共享submission，现有Console逐行输出难以区分并行Skill。宿主需要只接终端就能观察调用关系与运行状态。

## 决策

复用事件总线，EventMsg可选附带ExecutionIdentity，采样边界发execution_updated。thread标识逻辑节点，attempt区分新turn与Resume。任务本地ContextVar只传播同session观测身份，不改变调度、prompt或Journal。状态投影与终端显示置于telemetry、实验导出；普通Console继续工作。

不建立另一条总线，不解析模型文本猜关系，不引入UI框架或监控数据库。冷恢复前完整历史不在本轮保证中；晚订阅、跳号和截断显式标不完整。上下文估算与实测、累计消耗与完成进度分开表示。

参照codex的父子调用与结构化事件范式；差异是复用Taifeng单engine总线、thread谱系和TelemetrySink接口，不移植具体产品界面。
