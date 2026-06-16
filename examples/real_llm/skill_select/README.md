# skill_select —— 真实 LLM「技能选择准确率」基准（200+ 候选）

回答一个核心问题：**当一个 agent 拥有成百上千个 skill 时，给定一个任务，它能不能正确选到对的那一个？**

> 这个**只能用真实 LLM 测**——SimClient 的回答是写死的，不会"看一堆候选挑对的那个"。
> 之前的 `examples/skill_outcome_fleet` 用 SimClient 脚本化派发，测的是**战绩沉淀**，**没测选择**。
> 本基准补上：真实 LLM 在 **210 个候选**里给每个任务选一个，断言选得对不对。

## 怎么测

```
┌─ router (entry composite) ── child_skills = 全部 210 个叶子 ───────────┐
│  一次 turn 的 prompt 里列出全部 210 条 `id: 能力描述`                    │
│  模型读用户任务 → 从 210 个里选【唯一最匹配】的一个 → call_skill(它)      │
└───────────────────────────────────────────────────────────────────────┘
        ↑ 捕获 call_skill 的 skill_id = 模型选了谁，对比 expected
```

- **210 个 skill**，7 个领域各 ~30 个（data / doc / ops / content / analytics / dev / office），
  能力**互相可区分**。
- 每个 skill 配一条 **改写过的自然请求**（task）——故意不照搬技能描述的措辞，所以模型必须
  **理解意图做语义匹配**，而不是字面关键词匹配。这才是真正考"选对没"。
- 成本优化：权限 **deny `Skill(*)`** + **`max_iterations=1`** → 路由器采样一次发出 call_skill
  即停、**叶子不执行**，每个任务**恰好 1 次 LLM 调用**。

## 跑

```bash
# 1) 生成技能集（纯 stdlib，从 raw/*.json 生成 210 叶子 + router + tasks.json）
python examples/real_llm/skill_select/build_skills.py

# 2) 跑基准（需真实 LLM key，复用 examples/_provider_bootstrap 的 LLM_BOOTSTRAP_* env）
PYTHONPATH=src python examples/real_llm/skill_select/bench.py --sample 210   # 全量
PYTHONPATH=src python examples/real_llm/skill_select/bench.py --sample 40    # 抽样 40 条
```

`--sample N` 抽 N 条任务（默认 40，N≥210 即全量）；`--seed` 控制抽样；`--timeout` 单任务超时秒。

## 输出

逐条打印 `期望=… 实选=…`（✓/✗），最后给：
- **总准确率** `correct/total`（+ 未选出数 + 耗时）
- **按领域准确率**（每个领域选对多少）
- **错选明细**：期望 vs 实选 + 任务原文——直观看到模型把什么误选成了什么
  （常见错选来自**近邻技能**：如"段落改写"vs"本土化改写"、"精确去重"vs"模糊去重"）

## 这个基准说明什么

- 它测的是**当前机制**（210 个候选全量塞进 prompt，靠 LLM 在上下文里选）的选择准确率。
- 错选大多发生在**语义近邻**的技能之间——候选越多、越相似，越难选准。这正是"语义检索 /
  渐进式披露"（认知回路 phase 2 `search_skills`）要解决的问题：先按语义召回 top-K，再让模型
  在少数候选里选，避免在 200+ 个里硬选。
- 想看**规模衰减曲线**：`bench.py` 的候选规模目前固定全量；可扩展为只暴露 entry 的部分
  child（始终含正确目标）来对比 25/50/100/200 候选下的准确率。

## 文件

- `raw/*.json` — 7 个领域的技能定义（id / name / description / task），**源数据**。
- `build_skills.py` — 从 raw 生成 `skills/`（210 叶子 + router entry）+ `tasks.json`。
- `bench.py` — 真实 LLM 选择准确率基准。
- `skills/`、`tasks.json` — 生成产物（可由 build 重建）。
