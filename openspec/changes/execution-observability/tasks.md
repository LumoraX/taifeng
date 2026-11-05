# 执行树观测实施计划

> 按已确认设计执行；先契约、红测试、实现、独立审查，再全量回归。实现子任务允许按目录隔离到独立worktree。

## 1. 契约
- [x] 1.1 提交能力契约、ADR0132与设计，独立审查。Acceptance：openspec validate execution-observability --strict。

## 2. 内核身份与事实事件
- [ ] 2.1 tests/loop/test_execution_observation_identity.py、test_execution_observer.py 先证明字段/作用域缺失，覆盖同Skill并行、spawn、跨session、取消与异常恢复。Acceptance：观察红→绿。
- [ ] 2.2 新增 loop/execution_identity.py、execution_observer.py；接入event、engine_events、TurnRunner与采样两处。Acceptance：相关loop测试全绿；turn.py和turn_sample.py均≤800行；R3新kind有Console渲染。

## 3. 投影与终端
- [ ] 3.1 新增 telemetry/execution_projection.py、terminal_tree.py 及对应tests/telemetry/test_*.py，TDD覆盖并发/绝对用量/重复事件/终态/续跑/跳号/容量/TTY/控制字符/环/取消。Acceptance：定向测试与ruff全绿。
- [ ] 3.2 experimental导出、可运行sim演示与独立审查；同步活文档/能力矩阵/usage。Acceptance：稳定API快照不变，演示实际显示父子节点和工具。

## 4. 验证
- [ ] 4.1 新增真实多路调用取证，证明真实模型产生的子调用有正确身份、父子关系与工具归属，selfcheck先过。Acceptance：故意移除身份或混淆父级的测试能检出。
- [ ] 4.2 Python3.12/3.13全量pytest、examples冒烟、ruff F,S108,I,TC、带litellm/telemetry-otel的mypy strict。Acceptance：全绿且工作树不被示例污染。
- [ ] 4.3 提交所有基础层实现后全量capability_matrix，提交生成台账，完成独立审查。Acceptance：全PASS、无未处理阻断项；不自动合入main。
