# ADR 0020: 预算自知提示 —— 穿越 soft_limit 注中性预算事实

- 状态：Accepted
- 日期：2026-06-14
- 关系：立项依据 ADR 0017 规则②（模型认知回路原语）；复用 `context/budget` 阈值与 `system_injection` 注入形态

## 背景

2026-06-14 对照 5 个参照实现做差距分析时，**codex 在多处按「剩余预算」裁剪上下文**，但
内核真正缺的认知回路原语是：**让模型自己知道还剩多少上下文预算**。当前 taifeng 在用量达
`soft_limit` 时触发压缩（cache-aware），但模型对「自己快撞上限」**全程无感知**——它拿不到自身
token 用量，只能一路啰嗦到被压缩/截断，无法主动收敛。

ADR 0017 规则②把「模型认知回路原语（自我 review / 任务清单工作记忆 / 状态穿越压缩）」明列为
该做的内核能力。**「预算自知」与「自我 review」同类**：内核给模型回路补一个它自己拿不到的自我
状态信号。区别于规则④（产品功能）：内核只**陈述事实**（用了多少、剩多少），不替业务决定「拿到
这个事实该怎么做」。

设计验证（读真实代码）确认：① `ContextBudget` 的 soft/hard 阈值与 `is_soft_exceeded` 已就位；
② `system_injection` + `_reinject_pinned_state` 提供了成熟的尾部注入范式；③ 全仓无任何「剩余
预算提示 / 上下文压力」实现（`llm/types.py` 的 `tokens_remaining` 是 provider 限流字段，无关）。

## 决策

### 决策一：穿越 `soft_limit` 时 pre-turn 注一条中性预算事实

`context/budget_hint.py` 提供纯决策（`render_budget_hint` 渲染事实串 + `evaluate_budget_hint`
穿越判定）。`TurnRunner._maybe_inject_budget_hint` 在每次迭代顶部、`_maybe_compress(pre_turn)`
**之前**调用：按当前 history 估算用量，穿越 soft 则往 history 尾追一条 `system_injection`
（`source="budget_hint"`）+ emit `BudgetHintInjected`。放在压缩前，使提示反映承压瞬间的高水位。

### 决策二：复用 `soft_limit`，穿越一次注一次（回落复位）

不新增阈值配置，直接复用 `ContextBudget.soft_limit`。一个「超 soft episode」内至多注一条
（`_budget_notified` 标志），用量回落到 soft 以下复位、再穿越重新注。**这是 R2 的关键**：把每个
episode 的额外 system 消息限到 1 条，避免每轮刷新反复打断 prompt cache。

> 取舍记录：曾考虑新增 50%/75% 双档（更早预警），但会引入新配置面；也考虑「每迭代都注最新用量」，
> 但 cache 代价与噪声最高。最终选「复用 soft + 一次性」——零新增配置、与压缩阈值对齐、cache 代价最小。
> 注：soft 同时是压缩触发点，故提示与压缩同时发生——提示因此兼具「解释刚发生的承压」的作用。

### 决策三：中性事实，不含产品意见（R1 / 规则④边界）

注入文本只陈述客观事实（百分比 + 距 hard 的剩余 token），**不含**「请总结 / 该收尾」等祈使。
「该怎么收敛」是业务/产品策略（规则④，触 R1），交给模型与业务侧——内核只补自我状态信号。

## 影响（R1–R5）

- **R1**：✅ 纯 token 预算（内核概念）；文本只陈述事实，无业务/产品策略。
- **R2**：✅ 仅 history 尾追加（不动已缓存前缀）；一次性语义限制额外 system 消息频率。
- **R3**：✅ `budget_hint_injected` 事件（穿越才 emit）。
- **R4**：✅ 同步纯内存判定 + 一次 `store.append`，不阻塞主 actor。
- **R5**：✅ 注入项落盘，resume 作为普通历史回放。

默认即生效（无开关）；短对话不穿越 soft → 零注入零事件，与旧版行为完全一致（迁移即回滚）。
契约见 `docs/architecture/capabilities/budget-awareness.md`。
