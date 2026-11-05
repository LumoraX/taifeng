## ADDED Requirements

### Requirement: 执行身份和父子传播
系统 SHALL 在执行事件上附加 execution_id、attempt_id、thread_id、skill_id 和可空父身份，并保持原submission订阅语义。

#### Scenario: 并行同 Skill
- **WHEN** 父turn并行调用同一Skill两次
- **THEN** 子事件 SHALL 有不同execution_id和相同父身份，输出和工具归属不串线

#### Scenario: 跨会话与取消
- **WHEN** 另一个session启动或执行退出/取消
- **THEN** SHALL 隔离父身份并恢复上下文，取消向上传播

### Requirement: 状态、用量与不可变快照
投影 SHALL 消费完整事件流并输出冻结快照，识别工具/子调用/人工等待及真实终态，按attempt自身用量绝对值计量。

#### Scenario: 续跑与重复事件
- **WHEN** 同thread续跑或收到重复事件
- **THEN** SHALL 复用节点、重置活动状态且不重复累计tokens，旧attempt事件不得复活终态

#### Scenario: 不完整观察
- **WHEN** 晚订阅、seq跳号或节点超限
- **THEN** SHALL 标记incomplete，保持有界内存，不推断不存在的节点

### Requirement: 可直接接线的终端显示
TerminalTreeSink SHALL 显示调用树、阶段、当前工具、耗时与用量，TTY重绘、非TTY追加可读输出。

#### Scenario: 边界输入
- **WHEN** 空树、深树、环、未知父级或超长含控制字符的字段被渲染
- **THEN** SHALL 有界结束、不向终端注入控制字符，明确标示截断

#### Scenario: 宿主关闭
- **WHEN** 宿主取消sink订阅任务
- **THEN** SHALL 正常退出，engine的任务和取消语义保持不变
