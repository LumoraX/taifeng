# ADR 0130：输入来源完整性与稳定入口

状态：Accepted

Amends #0085

## 决策

上下文扫描不能回答「此前是否进入过不可信内容」：回滚和压缩可能移除条目。
内核按 thread 维护只增不减的来源事实，从 append-only 原始记录冷重建；不把移除的正文重新塞回 prompt。
记录只包含不可信条目 id、来源类别与标签，InputTaint 快照不可变。
单次 ToolResult.origin 可声明读取结果的来源，静态不可信声明不能被可信结果降级。
派生种子与子会话返回继承来源，introspect 公开累计汇总；是否确认留给宿主。

audit 工具结果与派生种子的来源使用既有 conversation-item 自由 metadata；barrier 登记需要在成员完成前持久化创建者快照：BarrierRegisteredV1 增加可选 origin，旧记录缺省为空，旧消费者会明确拒绝含新字段的记录；其余 DTO 不变。未声明时 serializer 省略 origin，保持旧 wire；按 ADR0025 向后兼容字段扩展沿用 V1。
来源 thread 必须区分公共 root API 与工具实际调用者；barrier 只冻结登记时的来源，不继承无关 root 后续输入。
strict audit 带来源的 UserMessage 仍按 ADR0085 显式拒绝，admission schema 扩展不纳入本次。

补足运行示例、协议接线、稳定出口与回归之后晋升已有来源类型和查询 helper，experimental 保留兼容。

## 契约

见 [input-origin](../architecture/capabilities/input-origin.md)。
