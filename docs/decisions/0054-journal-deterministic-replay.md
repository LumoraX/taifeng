# ADR 0054：用审计 Journal 做确定性回放（按请求摘要匹配）

- 状态：Accepted
- 日期：2026-09-28
- 关联：[llm-client § Journal 确定性回放](../architecture/llm-client.md)；ADR 0025 / 0027（审计请求摘要）

## 背景

strict audit Journal 已经 durable 记下每次 LLM 调用的请求摘要与最终响应，但没有任何消费者能把它
回放回来。回归测试只能手写 SimTurn 剧本，而真实会话里出现的路径无法被固定下来复跑。

## 决策

1. **数据源就是 Journal**，不另建录制格式。
2. **按请求摘要匹配**：复用审计侧 `project_attempt_request` 的 canonical 摘要（空模型按录制侧适配器
   同规则补齐）。顺序匹配在并发子 turn 下不稳定；摘要匹配同时就是「请求是否与录制一致」的断言。
3. **分叉显式失败**，不回退到「按顺序给下一条」——静默回退会让回放测试失去意义。
4. **只支持 Chat 协议录制**：Responses 输入项带 thread 派生 sample id，跨运行摘要必然不同；与其给出
   永远失败的回放，不如构造期拒绝。provider 专有回传状态（thinking 签名、Gemini extra_content）不在
   normalized items 里，回放不还原（录制侧如需完整还原，属后续 Journal schema 扩展）。
