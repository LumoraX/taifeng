# 设计：放松 composite 校验 —— 允许 tool-only composite

- 日期：2026-06-04
- 状态：已获用户批准（待 spec 评审）
- 关联 ADR：将新增 `docs/decisions/0013-composite-tool-only.md`；澄清 ADR 0006 本意

## 1. 背景与问题

Taifeng 的 skill 分两态（ADR 0006）：

- `atomic` —— 纯内容 / 知识叶子，**禁止**声明 `tool_names` / `child_skills` / `orchestration`，不可作 entry。
- `composite` —— 有 agency 的 skill，可声明 `tool_names`（自己的工具白名单）与 `child_skills`（可 `call_skill` 派发的子 skill）。

当前 [`definition.py`](../../../src/taifeng/skill/definition.py) 的 `validate()` 对 composite 强制 **`child_skills` 非空**。由此产生一个建模摩擦：

> 一个语义上是"叶子"的 skill（只做分析、并需要调用 `request_user_input` 之类的工具向用户采集数据），因为要用工具就必须升为 `composite`；而 composite 又被强制要求非空 `child_skills`，于是开发者被迫**凭空捏一个 dummy 子 skill** 才能通过校验。

典型场景：`lung-nodule`（composite，LLM 驱动编排）下的某个子 skill 需要在分析中途调 `request_user_input` 暂停问用户补数据，补齐后续跑、再进编排下一个子 skill。该子 skill 本质是工具型叶子，却被迫带一个无意义的 `child_skills`。

### 关键事实

ADR 0006 的数据结构（§数据结构）只把 `child_skills` / `tool_names` 标为"composite 特有字段（atomic 全部留空）"，**从未要求 composite 必须有非空 child_skills**。"composite 必须声明 child_skills"是 `definition.py` 后加的实现约束，并非决策本身。因此本次放松是**回归 ADR 0006 本意**，而非推翻它。

## 2. 决策

采用 **变体 A**：把 composite 的含义从"必须有子 skill"修正为"**有 agency —— `child_skills` 与 `tool_names` 至少其一非空**"。两者皆空 = 戴帽子的 atomic（无意义空壳），仍 fail-fast 拒绝。

排除的备选：

- **变体 B**（完全去掉非空要求，两者皆可空）：会放进"无子无工具的 composite"空壳，违背项目"无空壳 / fail-fast"调性。
- **变体 C**（不动 composite，改为允许 atomic 声明 tool_names）：与"atomic = 纯内容、无 agency"的模型定位相悖；用户已明确选择放松 composite。

`request_user_input` 维持为**普通工具**，由 skill 通过 `tool_names` 显式授予后 LLM 才会选择调用——**不引入任何内置原语**（保持"无配置即纯 LLM 调工具"的范式）。

## 3. 改动设计

### 3.1 唯一代码改动点

[`src/taifeng/skill/definition.py`](../../../src/taifeng/skill/definition.py) 的 `validate()` composite 分支：

```python
elif self.type == "composite":
    # composite = 有 agency 的 skill：可调子 skill、可调工具，二者至少其一。
    # 两者皆空 = 戴帽子的 atomic（无意义空壳）→ fail-fast 拒绝。
    if not self.child_skills and not self.tool_names:
        raise SkillValidationError(
            f"composite skill {self.id!r} 必须至少声明 child_skills 或 tool_names 之一"
        )
    if self.max_call_depth < 1:
        raise SkillValidationError(
            f"composite skill {self.id!r} max_call_depth 必须 >= 1"
        )
```

`max_call_depth >= 1` 检查保留（对 tool-only 叶子无害，默认值 6）。

### 3.2 零回归依据（其余文件不改）

全仓库唯一强制 child_skills 非空的点就是上述 `validate()`。其余用到 `child_skills` 的地方均为**迭代**，空集合即零次循环：

- `loader.py::compute_reachable_graph`：叶子无子 → 内层循环零次，可达图正常（被访问、不贡献新可达节点）。
- `dispatch.py`：`for child_id in defn.child_skills` 同理。
- `loader.py` child_skills 引用完整性：空集合 → 无未知引用，通过。
- `orchestration`：tool-only 叶子根本不声明 orchestration；orchestration 引用必须 ⊆ child_skills，由既有校验保证，无冲突。

## 4. 对 lung-nodule 场景的落地形态

- **要调 `request_user_input` 的子 skill**：写成 `type: composite` + `tool_names: [request_user_input]`，`child_skills` 留空，不再捏 dummy 子 skill。
- **纯分析、不调任何工具的子 skill**：保持 `type: atomic`。
- 父 `lung-nodule`（LLM 驱动）的挂起 / 续跑链不受本次改动影响——改的只是子 skill 的合法性校验，不是运行时派发。

> 注意（超出本次范围）：声明式 `orchestration` 块路径（`orchestration_exec.py`）当前**不传递子 skill 挂起**，是独立的已知缺口；lung-nodule 走 LLM 驱动（无 `orchestration:` 块），不受其影响。本设计不处理该缺口。

## 5. 测试计划

| 测试 | 类型 | 断言 |
| --- | --- | --- |
| `test_tool_only_composite_accepted`（新增） | loader | composite + `tool_names` 非空 + `child_skills` 空 → 加载 / validate 通过 |
| `test_composite_missing_child_skills_rejected`（**保持不变**） | loader | 既无 child 又无 tool 的 composite → 仍 `SkillValidationError`（变体 A 语义，该旧测试天然覆盖新规则的拒绝侧） |
| `test_tool_only_composite_suspend_resume`（新增） | e2e | tool-only composite 作子 skill，在 LLM 驱动父下调 `request_user_input` 挂起 → `Resume` 续跑回传父（复用 `tests/test_child_suspend_resume.py` 模式），给真实场景端到端兜底 |

验证命令：`PYTHONPATH=src uv run pytest tests/skill/test_skill.py tests/test_child_suspend_resume.py -v`。

> 切片提示：前两个测试是 loader 级 validate 断言，可与代码改动同 commit；第三个 e2e（`test_tool_only_composite_suspend_resume`）体量最大，建议**独立 commit 切片**（遵守 DoD ≤3h / 单 commit 单功能）。

## 6. 文档落档（实现完成但文档未同步 → 不合并）

1. **新增 ADR** `docs/decisions/0013-composite-tool-only.md`（只增不改）：撤销 definition.py 的隐式"composite 必须有 child"约束，确立"composite = child_skills 或 tool_names 至少一个"；标注与 ADR 0006 的关系（澄清本意，非推翻）。
2. **活文档** `docs/architecture/capabilities/skill-dispatch.md:233`：「composite: 必须声明 child_skills（>=1）」→「必须声明 child_skills 或 tool_names 至少一个」。
3. **活文档** `docs/architecture/skill-system.md`：该文件 §162-169 的 `validate()` 代码片段当前只渲染了 atomic 分支（没有"composite 必须 child_skills"的字面行可改），实现时应**补上 composite 分支**（体现新规则），而非搜索替换旧文本；同步校验小节叙述。
4. `definition.py` 的 docstring / 注释同步。

## 7. R1–R5 影响声明

- **R1 业务零侵入**：仅放松一条结构校验，无业务概念引入。✅
- **R2 Cache 友好 / R3 可观测 / R4 可取消**：不碰压缩 / cache / 事件 / 取消路径。无影响。
- **R5 可 resume**：不碰 store / history；tool-only composite 子 skill 的挂起续跑复用既有 LLM 驱动链路。✅

## 8. 范围边界（YAGNI）

- **不**处理声明式 orchestration 路径的挂起传递缺口（独立问题，另立提案）。
- **不**引入任何内置工具 / 原语。
- **不**改动 atomic 的约束（atomic 仍禁工具）。
