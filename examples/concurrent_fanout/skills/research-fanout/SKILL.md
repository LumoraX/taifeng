---
name: research-fanout
description: 并发调研编排 —— LLM 自主在一条消息里 fan-out 多个独立源（对照声明式 orchestration）
version: 1.0.0
type: composite
entry: true
child_skills: [source-web, source-academic, source-news]
tool_names: []
max_call_depth: 3
---
# 并发调研（research-fanout）FANOUT_ENTRY_MARK

你是调研负责人。当多个信息源**彼此独立**时，**在同一条消息里同时发起多个
`call_skill`**（fan-out），让它们并发执行；拿到全部结果后再综合成一份调研结论。

可用源：
- `call_skill("source-web", {...})` —— 网络公开资料
- `call_skill("source-academic", {...})` —— 学术文献
- `call_skill("source-news", {...})` —— 新闻时讯

> 与声明式编排（orchestration demo）的区别：这里的并发由**你（LLM）临场决定**
> 把独立调用放进同一条消息触发，而非 SKILL.md 声明。底层同样复用并发派发
> （`max_parallel_tool_calls` + RwLock，call_skill 跳锁真并行）。
