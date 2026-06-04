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

**只加一处**：让每个想独立重试的步骤 skill 能作为入口单独跑 —— 即给它加 **`entry: true`**。

- 你的 lung-nodule 6 个子 skill 现在是 `entry: false` 的 tool-only composite（`tool_names:
  [request_user_input]`）。加 `entry: true` 后，它们**既能**被 `lung-nodule` 自治链 `call_skill`
  派发（一键跑完），**又能**被本范式的业务编排单独拉起 / 重试。一个 skill 同时是某 entry 的
  child + 自身 entry，taifeng 允许。
- body 基本不用动：你的每个步骤本来就写「你将收到患者数据 + 上一步结论」，天然是「给定输入
  即可独立跑」的契约。只要保证它**只认传入的 `{患者数据 + 上游结论}`**、不依赖自治链的隐式
  上下文即可。

即：**纯加法**——自治模式继续可用，额外获得「业务编排 + 步级重试」的调试模式。把
`server.py` 里的 `STEPS` 与 `SKILLS_DIR` 换成你的 6 步即可（或用 env 指向你的 skills 目录）。
