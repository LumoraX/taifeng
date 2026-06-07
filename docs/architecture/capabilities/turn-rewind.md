# Capability: turn-rewind

## Purpose

把一次 root turn 的执行轨迹拆成一张**可寻址的回访节点表**，业务侧可对**任意节点直接 retry**：既能重跑某一次 LLM loop 采样（iteration 节点），也能重跑某一次工具 / `call_skill` 派发（dispatch 节点）。重试发生在**同一次 turn 内**；子 skill 全程 `entry: false`，**绕开 entry/call_skill 互斥**（不依赖把子 skill 变 entry，也不放松 [`skill-dispatch`](skill-dispatch.md) 的 `cannot_call_entry_skill` 约束）。

设计：`docs/superpowers/specs/2026-06-05-addressable-dispatch-rewind-design.md`
实现：`src/taifeng/loop/rewind.py`（节点结构）、`loop/turn.py`（记录 + retry_tool 补跑）、`loop/engine.py`（`_handle_rewind` + `rewind_nodes()`）。

## 数据契约

### `RewindCheckpoint`（`loop/rewind.py`）
一个 turn 内可回退锚点，**只记 history 下标**（append-only 不破，R5）：

| 字段 | 含义 |
| --- | --- |
| `node_id` | turn 内稳定唯一 id（`it{n}` / `disp{n}`） |
| `kind` | `iteration` \| `dispatch`（`turn_root` 收敛进首个 iteration 节点 `it1`） |
| `history_len` | re_reason 截点 = 该 history 长度 |
| `cache_anchor` | 回退时还原的 `cache_anchor_index` |
| `iteration_index` | 所属采样圈 |
| `call_id` / `target_id` | 仅 dispatch：被派发的 call_id / 工具名 |
| `inner_history_len` | 仅 dispatch：retry_tool 切点（`function_call` 后 / `function_call_output` 前） |
| `args_digest` | 仅 dispatch：原始 args 摘要（供 UI / 审计） |

### `Rewind` Op（`loop/submission.py`）
`{ node_id, mode ∈ {re_reason, retry_tool}, new_args? }`。`new_args` 仅 `retry_tool` + dispatch 节点有意义。

## Requirements

### Requirement: root turn 记录完整回访节点表

root TurnRunner SHALL 在每圈 `_sample_once` 采样前记一个 `iteration` 节点，并在每次工具 / `call_skill` 派发（`function_call` 追加处）记一个 `dispatch` 节点。每记一个节点 SHALL emit `rewind_checkpoint_recorded`（R3）。**仅 root turn**记录；子 turn 节点 v1 不入表。节点表随 turn 结束回写 engine，`engine.rewind_nodes()` 只读暴露。

#### Scenario: 自治链跑 N 圈 M 次派发
- **WHEN** 一次自治 turn 跑 3 圈、前 2 圈各 1 次 `read_skill` 派发
- **THEN** `rewind_nodes()` SHALL 含 3 个 iteration + 2 个 dispatch 节点
- **AND** 每个 dispatch 节点 `history_len` == 所属 iteration 节点的 `history_len`（re_reason 切点归一，因 assistant 消息原子、不可切在并行 tool_call 中间）
- **AND** `inner_history_len` > `history_len`（retry_tool 切点在 fc 之后）

### Requirement: Rewind re_reason 截到节点采样前并重采样

`mode=re_reason` 时，系统 SHALL 把 engine history 截到 `cp.history_len`、回退 `cache_anchor`，落 rewind marker，emit `turn_rewound`，再建新 root TurnRunner **从截点重采样**（LLM 自由重决下游）。

#### Scenario: rewind 到 iteration 节点
- **WHEN** 对 `it2` 提交 `Rewind(mode=re_reason)`
- **THEN** history 截到 `it2.history_len`，重采样走出新路径（下游可与首跑不同）
- **AND** `turn_rewound.data` 含 `node_id / mode / cut_index == it2.history_len`

### Requirement: Rewind retry_tool 保留派发决定、只重跑该工具

`mode=retry_tool`（仅 dispatch 节点）时，系统 SHALL 截到 `cp.inner_history_len`（**保留** assistant 的 `function_call`、丢弃旧 `function_call_output` 及其后），用 `new_args`（或原 args）**补跑该工具 / 子 skill**、追加新 output，再续推。补跑复用 `dispatch_batch` + `_build_tool_context`（含 `dispatcher`），故 `call_skill` 子 skill 也能正确重跑。

#### Scenario: retry_tool 重跑一次 call_skill
- **WHEN** 对某 dispatch 节点提交 `Rewind(mode=retry_tool)`
- **THEN** assistant 的 `function_call` 被保留；该工具被重跑、output 被替换；LLM 从新 output 续推
- **AND** `turn_rewound.data.cut_index == cp.inner_history_len`

### Requirement: 校验失败显式拒绝（禁 silent fallback）

下列情形 SHALL emit `rewind_rejected` 并**不改 history**，绝不静默 no-op：

| reason | 触发 |
| --- | --- |
| `unknown_node` | `node_id` 不在节点表 |
| `mode_kind_mismatch` | 对非 dispatch 节点用 `retry_tool` |
| `turn_suspended` | 存在活跃挂起（HITL）record —— 挂起态 rewind v1 不支持 |

#### Scenario: 未知节点
- **WHEN** `Rewind(node_id="does-not-exist")`
- **THEN** emit `rewind_rejected(reason="unknown_node")`，`history_snapshot()` 长度不变

#### Scenario: iteration 节点用 retry_tool
- **WHEN** `Rewind(node_id="it1", mode="retry_tool")`
- **THEN** emit `rewind_rejected(reason="mode_kind_mismatch")`

## R1–R5 影响

- **R1**：`RewindCheckpoint` / `Rewind` 全通用，无业务概念；业务经 Op + `rewind_nodes()` 使用。
- **R2**：rewind 蓄意回退 anchor → 首采样 cache 失效标 **expected**（`reason="rewind"`），不计入 `unexpected_cache_breaks`。
- **R3**：`rewind_checkpoint_recorded` / `turn_rewound` / `rewind_rejected` 三事件。
- **R4**：重推全程透传根 `CancellationToken`，子 skill 走 `cancel.child()`。
- **R5**：截断**仅内存**，store JSONL append-only（旧 items 不物理删），rewind marker 留痕。

## 演示 / 参考实现

`examples/web_ui/`（demo_id `turn_rewind`）提供浏览器可交互的演示：自治链跑完后拉回访节点表，支持 re_reason / retry_tool 两种重跑模式，重跑事件经 detached bridge 实时回流前端。无 key 自动化 smoke：`examples/web_ui/smoke_detached.py`。

## 边界与暂不支持（v1）

- 子 turn 内部节点不可寻址（只 root turn 入表）。
- 挂起态 turn 内 rewind：拒绝（`turn_suspended`）。
- `retry_tool` 假定**串行派发**（`max_parallel_tool_calls=1`，默认）：并行批次内部分重试会留下「已声明未补全」的 tool_call，v1 不支持。
- replay 模式（录后确定性重放整条 call 图）、压缩等内核动作作为节点：留待后续（见设计 §8）。
