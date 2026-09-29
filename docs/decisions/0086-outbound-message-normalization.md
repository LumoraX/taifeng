# ADR 0086：出站消息归一化——最终回答经 hook 归一后再交给业务

- 状态：Accepted
- 日期：2026-09-30
- 关联：[hooks 契约 § outbound_message](../architecture/capabilities/hooks.md)；
  [agent-loop 活文档](../architecture/agent-loop.md)；ADR 0017 / 0019 / 0061

## 背景

业务把模型的最终回答送到聊天窗口、消息渠道、工单系统之前，几乎都要做同一类处理：去掉漏进正文的推理标签、
规整换行与空白、脱敏、加签名。内核在这件事上有两个缺口：

1. **没有「这一轮的最终回答」这个事件**。`turn_completed` 不带文本，业务只能自己拼接 `assistant_text` 增量；
   多圈工具循环里各圈的文本如何拼接，要么照抄内核的规则，要么各写各的。
2. **没有改写它的入口**。`post_turn` hook 是审计型的，拿得到文本但改不了。ADR 0061 给工具输出开了
   `output_override`，出站这一侧没有对应物。

能力侧 review 把它列为「只定协议」的候选。命中 ADR 0017 规则③。

## 决策

1. **新增 hook 类型 `outbound_message`**，与 `post_tool_use` 的 `output_override` 同构：handler 返回
   `text_override` 即改写，多个 handler 链式生效，不可否决。
2. **新增事件 `outbound_message`**，携带归一后的文本，在 `turn_completed` 之前发出。`turn_completed` 是过滤订阅的
   终结信号，之后发出的事件订阅者收不到。
3. **只改出站文本，不改 history**。模型的原话留在 history 与 transcript 里：改写 history 会改变之后每一次请求的
   内容、破坏缓存前缀，也让审计对不上模型实际说了什么。
4. **只对 root turn 的真终态**。子 skill 与 detached spawn 的结果是给父模型或聚合 skill 看的，属于内核内部的
   信息流；挂起的 turn 没有最终回答。
5. **未注册 handler 时不发事件**：默认事件流不变，既有订阅者与 sim 金样不受影响。
6. **内核自带一个渠道无关的归一化**，opt-in 注册：去推理块、规整换行与空白；围栏代码块内部原样保留——
   代码里的缩进与空行是内容。
7. **不做渠道适配**（分片、富文本转换、长度上限）：那是产品层的事（ADR 0017 规则④），业务在自己的 handler 或
   下游处理。

## 替代方案

- **`EnginePool.create(outbound_normalizer=)`**：又一个要穿透构造链的参数；而 hooks 已经到达 turn 末尾，
  出站归一也正是一种 hook；否决。
- **让 `post_turn` hook 可改写**：`post_turn` 在 engine 回写状态之后触发，晚于 `turn_completed`；把它改成可改写
  还会改变它「仅审计」的既有契约；否决。
- **归一化后的文本写回 history**：见决策 3；否决。
- **对流式增量也做归一**：增量到达时看不到完整文本（推理块可能跨多个增量），只能缓冲，等于取消流式；
  增量本就是尽力而为的预览（ADR 0025），权威文本以本事件为准；否决。
- **默认就注册内核归一化**：会改变既有业务看到的事件流；否决。

## 后果

- `HookKind` 新增 `outbound_message`（桶位 9 → 10）；新增 `OutboundMessageHook` 与事件 `outbound_message`。
- 注册了 handler 的业务应以 `outbound_message.text` 为出站文本，不再依赖拼接增量。
- `TurnOutcome.final_text`、`post_turn` hook 拿到的文本、spawn 句柄的 `result` 仍是模型原话。
- R1–R5：R1 脱敏 / 签名等规则由业务 handler 提供；R2 history 不变；R3 新事件；R4 handler 运行在 turn 末尾，
  应当轻量，重活自行 detached；R5 不改持久化内容。

## 验证

`tests/hooks/test_outbound_message.py`：默认归一化各规则与组合、幂等、代码块内部与其中的标签保留；
未注册时无事件；默认归一化改写最终回答且先于 `turn_completed`、history 不变；干净文本报告未改写；
handler 链式生效并拿到 turn 事实；抛异常 / 非字符串改写 / deny 时文本不变且 turn 正常结束；
子 turn 不发出站事件。
