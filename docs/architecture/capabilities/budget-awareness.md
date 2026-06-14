# Capability: budget-awareness

## Purpose

让模型**自知剩余上下文预算**，从而主动收敛——解决「模型对自己快撞上下文上限毫无感知、
一路啰嗦到被迫压缩/截断」。当 history 用量在 pre-turn 穿越 `soft_limit` 时，往 history
尾部注一条**中性预算事实**（用了百分之几 / 距 hard limit 还剩多少 token），把「我还剩多少
预算」这一自我状态喂回模型。

属 ADR 0017 **规则②（模型认知回路原语）**：与「自我 review」「任务清单工作记忆」同类——
内核给模型回路补一个它自己拿不到的自我状态信号，而非外部成熟服务能承担的功能。

实现：`src/taifeng/context/budget_hint.py`（纯决策：`render_budget_hint` + `evaluate_budget_hint`）、
`loop/turn.py`（`_maybe_inject_budget_hint`，迭代顶部、pre-turn 压缩**之前**）、
`loop/event.py`（`BudgetHintInjected`）。详见 ADR 0020。

## 数据契约

### 纯决策函数（`context/budget_hint.py`，无状态、可单测）

| 函数 | 含义 |
| --- | --- |
| `render_budget_hint(used, budget) -> str` | 渲染中性事实串：`"Context budget: ~X% of the N-token context window is used (~M tokens until the hard limit)."`。**只陈述事实，不含「该不该收敛」的产品意见**（R1）。 |
| `evaluate_budget_hint(used, budget, *, was_notified) -> (inject, notified)` | 穿越判定 + notified 状态流转：超 soft 且未通知 → `(True, True)`；超 soft 已通知 → `(False, True)`；未超 soft → `(False, False)`（回落复位）。 |

### 注入形态

复用既有 `system_injection` ResponseItem kind，`source="budget_hint"`，**history 尾部追加**
（cache anchor 之后，R2 无额外 break）；`store.append` 持久化（R5）。渲染为 `role="system"`
消息进 LLM 视图（与 pinned / memory 类注入同路，provider 各自特判中段 system）。

### `BudgetHintInjected` 事件（`kind="budget_hint_injected"`）

`data = {"used": int, "context_window": int, "ratio": float, "remaining_to_hard": int}`——
不重复正文；`ratio = used / context_window`（两位）。未穿越则**不 emit**（零噪声）。

## 行为契约

### Requirement: 注入点 = 迭代顶部、pre-turn 压缩之前

- 每次迭代边界（`_drain_pending_input` 之后、`_maybe_compress(phase="pre_turn")` **之前**）按
  当前 history 估算用量判定。放在压缩前，使提示反映**承压瞬间的高水位**（`soft_limit` 同时是
  压缩触发点；压缩会随后把用量降下来，提示则解释「刚发生了承压」）。
- 估算口径复用 `estimate_history_tokens`（与压缩判定同源），不引入第二套口径。

### Requirement: 复用 `soft_limit` 阈值，穿越一次注一次（回落复位）

- 不新增阈值配置；直接复用 `ContextBudget.soft_limit`（默认 `0.85 * context_window`）。
- 一个「超 soft episode」内**至多注一条**（`_budget_notified` 标志），避免每轮刷新打断 cache。
- 用量回落到 soft 以下 → 标志复位；再穿越 → 重新注一条。

### Requirement: 中性事实，不含产品意见（R1 / 规则④边界）

- 注入文本只陈述客观事实（百分比 + 剩余 token）；**不含**「请总结 / 该收尾 / 节约」等祈使。
  「拿到这个事实该怎么做」交给模型与业务侧——内核只补自我状态信号，不替业务定收敛策略。

### Requirement: 默认即生效，迁移即回滚

- 无新增开关：任何 turn 只要 history 穿越 soft 即注入。未穿越（短对话）→ 零注入零事件，
  行为与旧版完全一致。

## R1–R5 影响

- **R1**：✅ 纯 token 预算（内核概念，非业务概念）；文本只陈述事实，不含业务/产品策略。
- **R2**：✅ 仅 history **尾追加**（不动已缓存前缀、不破 anchor）；一次性语义把每个 episode 的
  额外 system 消息限到 1 条，避免反复刷新。注入在 pre-turn 边界（R2 允许动 head 的相位）。
- **R3**：✅ `budget_hint_injected` 事件（穿越才 emit，零噪声）。
- **R4**：✅ 同步纯内存判定 + 一次 `store.append`，不阻塞、随 turn 的 cancel 自然中断。
- **R5**：✅ 注入项经 `store.append` 落盘，resume 续跑时作为普通历史回放。
