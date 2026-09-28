# ADR 0065：pinned 状态周期重注

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/postcompact-state-reinjection.md § 周期重注](../architecture/capabilities/postcompact-state-reinjection.md)；ADR 0017（规则② 认知回路原语）

## 背景

pinned 状态（todo 清单是官方范例）只在压缩成功后钉回历史尾部。没触发压缩的长会话里，模型最后一次看到清单可能在几十轮之前，
清单作为「工作记忆」逐渐失焦；Claude Code 等会周期性提醒模型当前任务清单。按 ADR 0017 规则②，这是认知回路原语的缺口。

## 决策

1. 新增可选能力协议 `PeriodicPinnedStateSource`：在 `PinnedStateSource` 之上多一个 `reinject_every_turns`。节奏按 source 声明
   （todo 需要、其他状态未必需要），而不是 engine 级全局旋钮。
2. 计数从 history 推导：上次 `pinned:<name>` 注入项之后的 user_message 数。不另存计数器，resume 后自然延续；压缩后钉回同样重置计数，
   两种注入不会连着重复。
3. 注入点：每个 turn 首轮迭代、pre-turn 压缩判断之后（先压缩再判断，避免压缩钉回后紧接着又重注）。尾部追加，R2 安全。
4. 复用 `system_injection` 形态与 `pinned_state_reinjected` 事件（`phase="periodic"`），不新增事件类型。
5. `TodoStore(reinject_every_turns=None)` 默认关闭，零行为变化。

## 否决的方案

- **engine / pool 级 `pinned_reinject_every_turns` 旋钮**：所有 source 被迫同一节奏，且要穿过 pool → engine → runner 多层构造。
- **内容未变就不重注**：周期重注的目的恰是「提醒」，清单没变正是模型可能遗忘的情形。
- **按采样迭代计数**：一轮工具循环可能迭代几十次，按迭代计会在同一用户请求内反复注入。

## 验证

`tests/context/test_pinned_periodic.py`（6 例）：计数推导、TodoStore 满足协议、到期筛选、无节奏不注入、
真实 pool 中节奏 2 在第 2、4 轮后注入且事件 phase=periodic、未配置节奏时不注入。
