# Step Pipeline —— 业务编排多步流水线 + 步级级联重试

演示**另一种编排范式**：与「entry skill 自治 `call_skill` 一口气跑完整链」相对，这里把
**编排（何时跑下一步）下沉到业务层**，每个步骤 skill 作为**独立 entry** 单独跑一个
turn / thread。换来的是**每一步都可调试式重试，且输入语义严格可控**。

## 为什么要这样（解决什么）

自治链（如 `examples/web_ui` 的 lung-nodule）一句话跑完 6 步很爽，但**没法外科手术式
重跑中间某一步**——6 步活在一个根 turn 里，步不是独立可寻址 / 可回滚的单元。

本范式把步拆成独立单元：

- **输入语义可控**：每步输入 = `seed（患者数据）+ 前序步骤输出`，由业务**显式构造并持久化**
  （`Step.input_text`）。重试时**原样重放**该输入 —— 不是把 skill 需要的参数盲目重填一遍。
- **步级级联重试**：`retry(k)` 作废 `k..N` 旧结果，用各自**重新构造的输入**从 `k` 往后重跑
  （下游依赖上游，上游变了下游必须重算）。
- **HITL 融入**：某步内 `request_user_input` 挂起**该步自己的 root thread**（步骤是 entry，
  无嵌套），用户填表后 `Resume` 续跑该步、再自动往后顺跑。

## 结构

```
skills/{intake,risk,plan}/SKILL.md   # 3 个步骤 skill（均 entry:true 的 tool-only composite）
pipeline.py                          # 编排器：run_from / retry / resume_step（不绑定 web）
demo.py                              # 离线自测（MockClient）：跑流水线 + 表单 + 级联重试断言
server.py + static/index.html        # 最小 web：步卡 UI + 🔄 重试按钮 + 就地表单
```

## 跑

```bash
cd taifeng

# 1) 离线自测（无需 API key，验证编排 + 级联重试 + 输入语义）
PYTHONPATH=src uv run python examples/step_pipeline/demo.py

# 2) Web（需 .env 配 LLM，同 examples/web_ui）
PYTHONPATH=src uv run python examples/step_pipeline/server.py
# 浏览器 http://localhost:8766
#   输入患者数据 → 开始 → 步1 弹表单 → 填写 → 步2/3 自动跑
#   任一已完成步点「🔄 重试此步」→ 用其持久化输入重放 + 级联重跑下游
```

## 这套范式对你的真实 skill 要改什么

> ⚠️ **重要更正（替代旧版「纯加法」说法）**：本范式与「`lung-nodule` 自治链一键跑完」
> **不是叠加，是二选一**。原因是 taifeng 的运行时硬约束——
> **`entry: true` 与「可被 `call_skill` 派发」在同一个 skill 上互斥**：
>
> | 角色 | 要求 | 来源 |
> | --- | --- | --- |
> | 被业务编排 / retry 单独拉起（作 session root） | **必须 `entry: true`** | `loop/pool.py:389` `loop/engine.py:127`（非 entry 报 `not entry-eligible`） |
> | 被自治链 `call_skill` 派发（作子 routine） | **必须 `entry: false`** | `skill/dispatch.py:175`（entry 一律拒 `cannot_call_entry_skill`） |
>
> 所以**给步骤 skill 加 `entry: true` 会让 `lung-nodule` 的 `call_skill(step)` 在运行时被拒**，
> 自治链就断了。采用本范式 = **用业务编排替换自治编排**（不是额外叠加）。

### 你有三条路（按推荐度）

**① 只用业务编排（推荐，零核心改动）**——把 6 步都标 `entry: true`，由业务层（`pipeline.py`）
顺序驱动；不再依赖 `lung-nodule` 的 `call_skill` 自治链。「一键跑完」用 `run_from(0)`
一次跑到底即可模拟。**换来步级 retry，代价是放弃 `call_skill` 自治链**。
→ 改动：6 个步骤 skill 各加 `entry: true`；`server.py` 的 `STEPS` / `SKILLS_DIR` 换成你的 6 步。

**② wrapper 双轨（想两种模式都保留时）**——核心步骤 skill 保持 `entry: false`（供
`lung-nodule` 自治链 `call_skill`）；**另给每步加一个薄 entry 包装** `step_xxx`
（`entry: true, child_skills: [核心步骤]`），业务编排拉起包装、包装内 `call_skill` 核心步骤。
两种模式共存。代价：6 个包装 skill + 包装需把输入透传给核心、把核心结论原样回流（多一跳
LLM 成本）。已实测 `wrapper(entry)→call_skill(core 非 entry) = ALLOW`。

**③ 改核心设计（最重）**——放松 `dispatch.py:175`，允许在白名单内 `call_skill` 一个
`entry` skill（或加 `callable_as_child` 标志）。让「双重身份」真正成立。**触及 entry 不变量，
必须走 ADR**（评估对 R1–R5 影响）。非必要不走这条。

> 无论选哪条，**body 基本不用动**：每步本就写「你将收到患者数据 + 上一步结论」，天然是
> 「给定输入即可独立跑」的契约——只要它**只认传入的 `{患者数据 + 上游结论}`**、不依赖
> 自治链隐式上下文即可。
