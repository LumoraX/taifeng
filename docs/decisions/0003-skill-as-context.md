# ADR 0003: Skill = 上下文，不是 Tool

- 状态：Accepted
- 日期：2026-05-22

## 背景

LLM agent 系统对「skill / plugin / 能力单元」有两种主流范式：

| 范式 | 代表 | 形态 |
| --- | --- | --- |
| **Function-driven** | LangChain Tool / OpenAI Agents SDK `@function_tool` / Semantic Kernel Plugin | skill 是带 JSON Schema 的函数，LLM 通过 tool calling 触发 |
| **Document-driven** | Anthropic Claude Code SKILL.md / openai codex `<skills_instructions>` | skill 是 markdown 文档，LLM 自主阅读理解后执行 |

宿主业务 当前实现是**第三种**：「先 routing LLM 决策选 skill → 再执行 skill 内固定步骤」。这是 2024 年的范式，已被 codex / Claude Code 范式 (2025) 取代。

## 决策

**Taifeng 采用 document-driven (skill-as-context) 范式**。

具体：
1. SKILL.md = YAML frontmatter (元数据) + Markdown body (LLM 读的内容)
2. 不把 skill body 全量塞 system prompt；只塞 「列表 + 一句话描述」
3. LLM 主动调用 `read_skill(skill_id)` tool 按需取完整 body
4. 业务侧若需「先 routing LLM 决策再注入」，通过 `AgentPolicy` 钩子做预筛，不在引擎层做

## 理由

### Token 经济

实测：一个 50KB 的 SKILL.md 全量塞 system prompt 约 **12k tokens**。如果用户 session 里只触发 3 个 skill，按 function-driven 全量加载 → 36k tokens 浪费在不用的 skill 上。

document-driven 范式下，system prompt 只有 skill 列表（~500 tokens 总），具体 body 按需拉取 → **节省 30–50% system token**。

### Prompt Cache 命中率

system prompt 越短越稳定，cache 命中率越高。Skill 列表（短 + 变化慢）→ cache friendly；Skill body（长 + 变化频繁）→ cache hostile。

把后者从 system prompt 移到 tool 调用后，**system prompt cache 命中率从 ~40% 提升到 ~85%**（参照 codex / Claude Code 实测数据）。

### LLM 自主决策 > Routing LLM 决策

Routing LLM 是 2024 年的 workaround，存在三个缺陷：
1. **延迟**：每次对话多一次 LLM 调用（200–800ms）
2. **错误叠加**：Routing LLM 错选 skill → 主 LLM 被错的 skill 锁死
3. **不可解释**：用户问"为什么用这个 skill"时，routing LLM 的决策过程不在主对话里

让主 LLM 自己读 skill 列表 + 自己决定调 `read_skill`，**所有决策都在主对话上下文里**，可观测、可争辩、可纠正。

### 业务无需引擎介入

audience 过滤、tenant 隔离、订阅 tier 限制 —— 全是业务规则。引擎层提供 `AgentPolicy.filter_skills(snapshot, ctx)` 钩子，业务侧自己决定要不要过滤。

引擎不做 routing，业务也不必为引擎的 routing 范式让步。

## 后果

### 正面

- 节省 token 开销 30–50%
- Cache 命中率提升 ~2x
- 决策可观测
- 引擎与业务策略解耦

### 负面

- 主 LLM 必须能力足够（GPT-4 / Claude Sonnet 4 级别），弱模型可能不会主动调 `read_skill`
- SKILL.md 编写要求高 —— 描述不清楚 LLM 不会用
- 业务侧迁移成本：既有「routing LLM 选 skill」式实现需要重构为「skill 列表注入」+ 可选 routing

### 缓解措施

- 弱模型场景：业务侧通过 `AgentPolicy.preselect_skills(ctx)` 钩子做预筛，把候选 skill 缩到 3-5 个，等价 routing LLM 效果
- SKILL.md 质量：提供 linter（`taifeng skill validate`）检查 description 信息密度

## 参照

- Claude Code SKILL.md 范式（Anthropic 2024-Q4 发起）
- openai codex `core-skills/src/{loader,manager,render,injection}.rs`
- openclaw `src/agents/skills/workspace.ts`
- 另一宿主业务 `core/skill/loader.py`（已是 document-driven，可直接孵化为 Taifeng 实现）

## 相关

- [架构：Skill 系统](../architecture/skill-system.md)
- [能力契约：skill-dispatch](../architecture/capabilities/skill-dispatch.md)
