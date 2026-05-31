# ADR 0004: Cache-aware 上下文压缩

- 状态：Accepted
- 日期：2026-05-22

## 背景

主流 agent 框架的压缩策略：

| 框架 | 策略 | Cache 影响 |
| --- | --- | --- |
| LangGraph cookbook | summarize_conversation 节点 | ❌ 无 cache 区分 |
| AutoGen v0.4 | `ChatCompletionContext`（滑窗 / head-tail 截断） | ❌ 无 cache 区分 |
| Letta / MemGPT | archival memory + recall | ❌ 无 cache 区分（设计哲学不同） |
| 另一宿主业务 `compact.py` | 每次重写整个 history | ❌ system prompt 后全部 cache miss |
| **codex / claw-code** | mid-turn 改 tail + pre-turn 动 head + 显式 cache anchor | ✅ |

codex 这套是目前开源世界几乎独一份的设计。

## 决策

Taifeng 采用 codex 范式的 **cache-aware 压缩**：

1. 引入 `InitialContextInjection` 枚举区分压缩注入位置
2. mid-turn 压缩**只允许动 tail**，保 `cache_anchor_index` 之前的 history byte-identical
3. pre-turn / manual 压缩可以动 head（cache 失效是预期内的）
4. 任何 `CompressionStrategy.compress()` 返回 `CompressionResult { cache_invalidated: bool, anchor_preserved_until: int }` —— 显式声明
5. `PromptCacheStats` 追踪 `expected_invalidations` vs `unexpected_cache_breaks`，后者是 bug 信号

## 理由

### Cache 经济性

Anthropic prompt cache 定价：
- Cache write: 1.25x base rate
- Cache read: 0.1x base rate
- 不 cache: 1x base rate

一个 50k token 的 system prompt + skill 列表，cache miss 一次约 $0.15。宿主业务 当前每天 ~5000 turn，**naive 压缩每天浪费 ~$150 在重建 cache**。

cache-aware 压缩把 mid-turn 压缩的 cache 命中率从 ~5% 提升到 ~70%（参照 codex 实测）。

### 设计哲学差异

| 范式 | 隐喻 |
| --- | --- |
| 滑窗 / 截断 | "记忆有限，丢弃旧的" |
| Letta archival | "agent 长生不老，把记忆归档到外存" |
| **codex handoff** | "**当前 agent 即将下班，写交接给下一班**" |

handoff 范式假设每次 turn 都可能"换班"，这与 Taifeng 的 turn-based 主循环天然契合。Letta archival 范式假设单 agent 持久学习，与 turn-based 哲学不符。

### LLM-to-LLM 接力提示词

参照 codex `templates/compact/prompt.md` 的四段结构：

```
## 进度 (Progress)
## 决策 (Decisions)
## 待办 (TODO)
## 引用 (References)
```

**关键：必须明确保留所有"不透明标识符"** —— UUID / hash / file path / 分支名。压缩可以丢失语言流畅度，但**绝不能丢失标识符**，否则后续 LLM 无法引用回压缩前的对象。

### tool_use / tool_result 边界保护

参照 claw-code `compact.rs` 的关键发现：

> Walk the boundary back until we start at a safe point. The first preserved
> message is a user message whose first block is a ToolResult, the assistant
> message with the matching ToolUse was slated for removal — that produces an
> orphaned tool role message on the OpenAI-compat path (400: tool message must
> follow assistant with tool_calls).

另一宿主业务 当前 `compact.py:217` **没有这个保护**。Taifeng 必须内置 `_walk_back_to_safe_boundary` 算法。

### Cache break 检测

不仅要"保 cache"，还要**检测预期外的 cache miss**：

```python
class CacheBreakEvent:
    unexpected: bool
    reason: Literal[
        "compaction_pre_turn",
        "compaction_manual",
        "skill_snapshot_changed",
        "tool_spec_changed",
        "unknown_drop",                # ⚠️ bug signal
    ]
    token_drop: int
```

`unexpected_cache_breaks++` 触发告警 —— 这是引擎层最重要的可观测信号。

## 后果

### 正面

- 成本节省 ~$150/天（基于 宿主业务 当前量）
- 压缩可靠性大幅提升（tool 配对不再切断）
- cache 行为可观测、可归因

### 负面

- 实现复杂度提升 ~3x（vs 另一宿主业务 单一压缩函数）
- 业务侧不能再"简单调一个 compress()"，必须传 `InitialContextInjection` 枚举
- 调试压缩失败需要理解 cache anchor 概念

### 缓解措施

- 提供 `CompressionOrchestrator` 协调器，业务侧只调 `maybe_compress()`，内部自动选 injection 模式
- 提供 `taifeng debug compaction` CLI 工具，可视化 cache anchor 位置与压缩前后 diff

## 参照实现

| 文件 | 提供什么 |
| --- | --- |
| codex `core/src/compact.rs` | mid-turn vs pre-turn 区分逻辑 |
| codex `templates/compact/prompt.md` | 四段接力提示词模板（英文） |
| claw-code `crates/runtime/src/compact.rs` (834 行) | tool 边界保护算法 |
| claw-code `crates/api/src/prompt_cache.rs` (735 行) | CacheBreakEvent 统计逻辑 |
| openclaw `src/agents/pi-hooks/context-pruning/pruner.ts` | 多策略协调思路 |
| openclaw `src/agents/pi-embedded-runner/compact.ts` | SessionCompactionCheckpoint |

Taifeng 不直接移植代码（语言不同），而是按这些参照重写 Python 版。提示词模板中文化。

## 相关

- [架构：上下文压缩](../architecture/context-compression.md)
