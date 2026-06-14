# ADR 0021: doom-loop 检测 —— 重复同调用空转的先警后断守卫

- 状态：Accepted
- 日期：2026-06-14
- 关系：立项依据 ADR 0017 规则②（模型认知回路原语）；并入 turn-resource-guards（与 `DenialBreaker` / `IterationBudget` 同族，镜像其注入与生命周期）

## 背景

2026-06-14 对照 5 个参照实现做差距分析时，opencode（`processor.ts`）单独做了「重复工具调用检测」。核实 taifeng 现状：turn 级资源护栏有两条——`DenialBreaker`（连续 **deny** 断路）和 `IterationBudget`（总迭代数上限），但二者都盖不到一个正交盲区：

**模型反复用相同参数调同一工具、每次都成功、却毫无进展的空转**（doom-loop）。例：一遍遍读同一文件、反复跑同一命令。`DenialBreaker` 只在 deny 时跳闸，doom-loop 全是成功调用 → 永不响；`IterationBudget` 只数总量 → 会一路烧到顶才停，中间几十轮空转全浪费，且报 `max_iterations` 掩盖了真因。

这正是 ADR 0017 规则②要做的认知回路原语：给模型回路补一个它自己拿不到的自我状态——「我在原地打转」。区别于规则④（产品功能）：内核只**陈述事实**（同工具同参数已连续 N 次、结果一致），不替业务决定「该怎么跳出」。

## 决策

### 决策一：先警后断（escalate），并入 turn-resource-guards

`loop/doom_loop.py` 新增 `DoomLoopConfig` + `DoomLoopDetector`（纯逻辑、turn 级、opt-in）。`TurnRunner._note_tool_outcome`（既有单点记账处）在**成功**结果上记 `(tool, arguments_raw)`：

- 连续 N 次同签名 → `"warn"`：注一条 `system_injection(source="doom_loop")` 中性事实 + emit `DoomLoopWarned`，turn 续跑给模型自改机会；
- 警告后仍连续到 2N → `"open"`：emit `DoomLoopCircuitOpen` + 置闩锁，turn 在迭代边界以 `end_reason="doom_loop_circuit_open"` 终止（对齐 `DenialBreaker` 的迭代边界终止 + 失败处置 policy 判定）。

> 取舍：曾考虑「只自知提示不终止」（最纯规则②，但真卡死仍烧到 max_iterations）与「直接断路」（最简，但不给模型自改机会、偏苛）。最终选 escalate——既给规则②的自改 nudge，又有断路兜底。grace 窗口 = N（警后再 N 次同签名才断），单旋钮 `max_consecutive_repeats` 派生 2N 断路阈值。

### 决策二：精确签名 (tool, arguments_raw)，连续相同计数

签名 = 工具名 + LLM 原始参数串，完全相等才算重复；不做 key 排序/去空白归一化（首版从简、确定性强、零误报；归一化口径本身需另定，留后续）。连续相同累加、出现不同签名即重置（含 warned 闩锁复位）。

### 决策三：中性事实，不含产品意见（R1 / 规则④边界）

警告文本只陈述「tool X 已连续 N 次相同参数调用、结果一致」，不含「请换个方法 / 该总结」等祈使——与 ADR 0020（预算自知）的中性事实决策一致。怎么跳出交模型与业务。

## 影响（R1–R5）

- **R1**：✅ 计数/记账无业务语义；阈值业务注入；文本只陈述事实。
- **R2**：✅ warn 注入仅 history 尾追加（不破 anchor）；不触压缩。
- **R3**：✅ `doom_loop_warned` / `doom_loop_circuit_open` 事件 + console 专用渲染（`loop ↻` / `loop ⊘`）。
- **R4**：✅ 断路终止走既有迭代边界终结路径（配对安全）；同步纯内存记账。
- **R5**：⚪ 检测器 turn 内瞬态；warn 注入项经 `store.append` 落盘随历史 resume 回放。

默认 `doom_loop_config=None` → 检测器不建，行为零变化（迁移即回滚）。契约见 `docs/architecture/capabilities/turn-resource-guards.md`。

> 真实 LLM 验证缺口（如实记录）：doom-loop 需模型主动「卡在重复调用」才触发，真实 LLM 难稳定复现，故 capability-matrix 真实验证列标 `—`（同 nested HITL resume）；以 sim e2e（`test_doom_loop_integration.py`）覆盖触发链路，全量真实回归矩阵跑通证明未破基础层。
