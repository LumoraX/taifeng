# Taifeng 文档索引

> 调研、设计、决策记录的入口。

## 阅读顺序

### 第一遍：理解 Taifeng 是什么 / 为什么

1. [架构总览 (architecture/overview.md)](architecture/overview.md) —— 5 维度抽象 + 6 核心包 + 5 基础设施包
2. [决策 0001：为什么叫 Taifeng](decisions/0001-naming-taifeng.md)
3. [决策 0002：为什么选 Python](decisions/0002-python-language.md)

### 第二遍：理解 Taifeng 与主流框架的差异

4. [对标差距分析](architecture/hermes-gap-roadmap.md) —— codex / claw-code / openclaw / hermes 四方横向对比
5. [决策 0003：Skill = 上下文，不是 Tool](decisions/0003-skill-as-context.md)
6. [决策 0004：Cache-aware 压缩](decisions/0004-cache-aware-compression.md)
7. [决策 0005：Submission / Event 双总线](decisions/0005-submission-event-bus.md)
8. [决策 0006：统一 Skill 模型，删除 Agent](decisions/0006-unified-skill-model.md)

### 第三遍：进入实现细节（architecture/ 模块设计）

9. [Skill 系统设计](architecture/skill-system.md)（§1.1，对应 ADR 0003 / 0006 / 0009）
10. [主循环设计](architecture/agent-loop.md)（§1.2 + §1.6 指令注入，对应 ADR 0005 / 0007 / 0010）
11. [对话持久化设计](architecture/conversation.md)（§1.3，对应 ADR 0008）
12. [压缩策略设计](architecture/context-compression.md)（§1.4，对应 ADR 0004）
13. [LLM 客户端设计](architecture/llm-client.md)（§1.5）

后续决策（随实现推进补充的 ADR）：

14. [决策 0007：Instructions 走业务侧注入协议](decisions/0007-instructions-as-injection.md)
15. [决策 0008：持久化层三协议拆分 + stdlib SQLite 索引](decisions/0008-store-protocol-decoupling.md)
16. [决策 0009：SKILL.md scripts 运行时](decisions/0009-scripts-runtime.md)
17. [决策 0010：闭合 permission gate 体系](decisions/0010-permission-gate-completeness.md)

### 第四遍：对标差距与进度（architecture/ 差距分析）

18. [引擎能力对比差距路线图](architecture/hermes-gap-roadmap.md) —— 逐 feature 进度（P0/P1/P2，带 commit 状态）
19. [微内核差距分析](architecture/kernel-gap-analysis.md) —— 内核子系统视角（K1–K7 原语）

> 两份差距文档互补、互相对账：roadmap 看"特性建了没"，kernel-gap 看"内核机制长齐没"。截至 2026-05-30，P0/P1/P2 与 K1–K7 双双清零。

## 文档分类约定

| 目录 | 用途 | 寿命 |
| --- | --- | --- |
| `architecture/` | 当前生效的架构设计 + 对标差距分析（gap/roadmap） | 长期；随实现更新 |
| `decisions/` | ADR 决策记录 | 永久；不可改写，只能补充新 ADR 推翻 |

> 与变更流程的边界：`architecture/` 写"系统现在的样子"（改，含 `capabilities/` 契约层）、
> `decisions/` 写"为什么这么定"（增）。详见根目录 `CLAUDE.md` / `AGENTS.md` 的「文档体系与义务」。

## 维护红线

- **`decisions/` 不可改写**：决策记录是历史。如要推翻，写新 ADR，标注 `Supersedes #NNNN`
- **`architecture/` 跟随代码**：实现改了，架构文档必须同步——否则 PR 不合并
- **新增设计必经 ADR**：任何引入新依赖、新协议、新红线的变更，必须先有 ADR
