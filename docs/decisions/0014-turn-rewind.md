# ADR 0014: turn-rewind —— turn 内任意节点可寻址 rewind

- 状态：Accepted
- 日期：2026-06-05
- 关系：不推翻任何 ADR；与 ADR 0006（统一 skill 模型 / entry 不变量）、ADR 0012（suspend-resume 原语）协作

## 背景

自治链「一键跑完」= 用户一条 `UserMessage` → 根 entry skill 的 LLM 在**同一个 root turn 内**多圈采样、连续 `call_skill(...)` → 一个 `turn_completed`。多个「步」是这一个 turn 里的采样圈 + 嵌套工具调用，中间**没有 user-message 边界**。

由此暴露两个诉求无法同时满足：

1. **重跑自治 run 里的中间某一步**。现有 `ThreadRollback` 粒度是「user_message 轮」，比 turn 内的采样圈 / 工具派发粗一到两级；自治链整条只有一轮，要么不退、要么整条全废。
2. 业务侧曾试图用「给步骤 skill 加 `entry: true` 让它能被业务编排单独 retry」绕过——但 `entry: true` 与「可被 `call_skill` 派发」在同一 skill 上**互斥**（`dispatch.py` 的 `cannot_call_entry_skill`），等于**替换**自治链而非叠加。

目标：让「自治一键跑完」与「重跑任意节点」在**同一套 `entry: false` 子 skill** 上共存。

## 决策

在 `src/taifeng/loop/` 加一个**通用内核原语**：把一次 root turn 拆成**可寻址回访节点表**，业务侧用 `Rewind(node_id, mode)` 回退到任意节点主动重推。

### 1. 节点三类，但 `turn_root` 收敛进 `it1`

- `iteration`：每圈 `_sample_once` 采样前。
- `dispatch`：每次工具 / `call_skill` 派发；暴露两个切点。
- `turn_root`（整条 turn 重来）：**不单独记**——首个 iteration 节点 `it1` 的截点（user_message 之后、首次采样前）已等价于「整条 turn 重来」。单列会与 `it1` 冗余。

### 2. dispatch 节点给两个切点（「两种都要」）

- `retry_tool`：截到 `function_call` 之后 / `function_call_output` 之前 —— **保留** assistant「决定调它」的动作，只重跑该工具、换 output。
- `re_reason`：截到所属 iteration 采样前 —— LLM 重新决定是否调、调什么。

> `dispatch.re_reason` 的截点**归一到所属 iteration 采样前**，而非「该 function_call 前」。因为 assistant 消息是原子的：一条 assistant 消息可能声明多个并行 tool_call，不能切在其中间再续推（会留下「已声明未补全」的 tool_call，破坏 API 合法性）。

### 3. 默认 `re_reason`

默认让 LLM 重决下游，使「自动跑完」全程保持自治、重试后下游自适应。`retry_tool` 是「保决定、只换工具结果」的精准变体。

### 4. 不放松 entry 约束

本能力让子 skill 全程 `entry: false`，根本不碰「entry 能否被 `call_skill`」。因此**不需要**放松 `dispatch.py` 的 `cannot_call_entry_skill`（README step_pipeline 的「路③改核心设计」在「自治 + 重试」诉求下不再必要）。entry 不变量（ADR 0006）保持。

### 排除备选

- **放松 entry 约束让「双重身份」成立**：触及 entry 不变量、波及订阅/打包语义（ADR 0006 §3），代价远大于收益，且本能力已让该需求消失。
- **录后确定性重放整条 call 图**（业务层 step_pipeline 范式）：放弃「重试时仍自治」，且活在业务层而非内核；保留为另一种范式，不作内核默认（留作未来 `replay` 模式，见设计 §8）。
- **复用 `ThreadRollback`**：粒度太粗（user_message 轮），无法寻址 turn 内的采样圈 / 单次派发。

## 影响

- **R1–R5**：见 [`capabilities/turn-rewind.md`](../architecture/capabilities/turn-rewind.md) 「R1–R5 影响」。关键：R2 rewind 的 cache 失效标 expected（不计 unexpected_breaks）；R5 截断仅内存、store append-only。
- **新增**：`Rewind` Op、`RewindCheckpoint` / `RewindLog`、`rewind_checkpoint_recorded` / `turn_rewound` / `rewind_rejected` 三事件、`engine.rewind_nodes()`、`TurnRunner` 的 retry_tool 补跑（`_complete_seed_call`）。
- **v1 暂不支持**：子 turn 内部节点、挂起态 rewind、并行批次内部分 retry_tool、replay 模式、压缩等内核动作作为节点（见契约「边界与暂不支持」）。

## 参照

设计 spec：`docs/superpowers/specs/2026-06-05-addressable-dispatch-rewind-design.md`
