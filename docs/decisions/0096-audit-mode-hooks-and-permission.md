# ADR 0096：审计模式放开 hook 与不挂起的权限裁决

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §16](../architecture/capabilities/session-journal-business-integration.md)；
  [hooks](../architecture/capabilities/hooks.md)、[permission-gate](../architecture/capabilities/permission-gate.md)；
  ADR 0025（Phase 4）、0010、0022

## 背景

审计模式在构造期拒绝任何 hook 运行器与权限策略。需要审计的部署恰恰是最需要这两样的部署：
生产环境的写操作要过审批，敏感输出要经 hook 脱敏。ADR 0025 约定「审批的请求和决定都是独立事实，
决定必须在执行被批准动作前提交」，此前未实现。

实现过程中发现一处既有缺陷：审计模式下子 skill 失败且带错误文本时，战绩条目的 `error_detail`
进不了 Journal（该字段只允许为空），`call_skill` 以内部异常告终。

命中 ADR 0017 规则①。

## 决策

1. **业务的 handler 与策略原样运行，内核在外面包一层**：handler 返回、策略裁决之后，
   先落一条记录，ack 后才把裁决交给调用方。业务代码不需要知道自己运行在审计模式下。
2. **按 turn 绑定，在 runner 构造时完成**。裁决记录要有 turn 归属，而 hook 注册表与权限策略是 pool 级
   的共享对象；绑定层带着「哪个 thread 的哪个 turn」。子 skill 的 runner 重新绑定到自己的 thread。
3. **每个 handler 的每次裁决一条记录**，不是每次 hook 触发一条。多个 handler 串行运行时，
   是哪一个拒绝的、哪一个改写的，要能分清。
4. **改写内容落账，其余 metadata 只记键名**。`args_override` / `output_override` / `text_override`
   是内核会据以改变执行的内容，必须可查证；其余 metadata 是业务的私有数据，内核不替它决定能不能落账。
5. **handler 出错只记异常类名**。异常原文可能带出任意内容（ADR 0025 对错误文本的既有约束）。
6. **权限请求的上下文进不了 Journal 时拒绝这次请求**，不调用业务的策略。放行一个无法记录的请求
   等于放弃审计；冻结整个 Session 又过重——请求方换一组参数就能继续。
7. **裁决记录用独立的 operation（`{turn_id}:hook:{n}` / `{turn_id}:permission:{n}`）**，
   不挂在工具调用的 operation 下。turn 层的 hook（`pre_turn`、`pre_compact`、`outbound_message`、
   `post_turn`）不属于任何工具调用；统一成一种 identity 后，针对的对象写在 `subject` 里。
8. **序号由 Session 级的计数分配**。同一 turn 的 engine 层 hook 与 runner 层 hook 经由不同的绑定对象
   触发，各自计数会撞号。
9. **只放开内核自己的 `HookRunner` 与 `PermissionPolicy`**。其他类型的对象内核不知道怎么包，
   照旧拒绝。
10. **以挂起方式进行的审批不在本次范围**。它需要挂起记录、`Resume` 的准入与恢复时对「等人」状态的
    识别，单独立项。当场作答的 prompter（回调）可以用。

## 替代方案

- **让业务在 handler 里自己写 Journal**：业务拿不到 coordinator，也不该拿到；且落账顺序
  （先于动作）只有内核能保证。
- **在每个 hook 调用点各加一段落账代码**：调用点有八处，散在六个模块里；新增调用点时容易漏。
- **用 contextvar 传递 turn 归属**：不必绑定，但归属变成隐式状态，子 skill 与父 turn 在同一个任务里
  先后运行时要小心还原。
- **每次 hook 触发只落一条汇总记录**：见决策 3。

## 后果

- 新增 record：`hook_evaluated`、`permission_decided`；operation 文法新增 `{turn_id}:hook:{n}`、
  `{turn_id}:permission:{n}`。
- 审计静态门对 `hooks` 与 `permission_policy` 改为按对象类型放行。
- 启用后每次裁决多一次 Journal 追加。
- 行为变化（缺陷修复）：审计模式下子 skill 失败时，战绩条目不再携带自由文本的错误详情，
  `call_skill` 正常返回失败结果；稳定错误仍在 `skill_dispatch_finished` 里。
- 未覆盖：挂起式审批与 HITL；`post_turn` 在 Session 紧接着关闭时可能不落账；
  handler 自由 metadata 的值；进程重启后对同一 turn 续编序号（当前恢复不重跑已有的 turn，不会发生）。
- R1：记录里只有通用字段，业务语义在 `request_metadata` 与 `reason` 里原样保存。R2：不涉及。
  R3：既有 hook / 权限事件不变。R4：落账使用 coordinator 既有的有界追加。R5：裁决成为 Journal 里的事实。

## 验证

`tests/loop/test_audit_gates.py`，条目见契约 §16.5。
