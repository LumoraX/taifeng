# Dual Track —— 自治链 + 业务编排 retry 在同一批核心上共存（wrapper 双轨）

解决 `examples/step_pipeline/README.md`「三条路」之②：让**同一批核心步骤 skill**
**既能**被 `lung-nodule` 式自治链一键跑完，**又能**被业务编排单独拉起 / 步级 retry。

## 为什么需要 wrapper（不是纯加法的根因）

taifeng 运行时有一条**硬不变量**——`entry:true` 与「可被 `call_skill` 派发」在
**同一个 skill 上互斥**：

| 角色 | 要求 | 来源 |
| --- | --- | --- |
| 被业务编排 / retry 单独拉起（作 session root） | **必须 `entry: true`** | `loop/pool.py:389`、`loop/engine.py:127` |
| 被自治链 `call_skill` 派发（作子 routine） | **必须 `entry: false`** | `skill/dispatch.py:175`（拒 `cannot_call_entry_skill`） |

所以一个 skill 不能同时担两职。**wrapper 双轨**用「核心 + 入口」拆分绕过：

```
intake_core / risk_core / plan_core   ← entry:false，真正的分析逻辑（单一真相）
  ├─ main（entry:true）自治链 call_skill 依次派发  → 轨道 A：一键跑完
  └─ intake / risk / plan（entry:true wrapper）call_skill 转调 → 轨道 B：业务编排 + 步级 retry
```

- **核心 `*_core`**：`entry:false` 的 atomic，写真正的分析 prompt。两条轨道复用同一份，逻辑不重复。
- **`main`**：`entry:true` composite，`child_skills:[三核心]`，body 指示「依次 call_skill 三核心、合并结论」。
- **wrapper `intake/risk/plan`**：`entry:true` composite，`child_skills:[对应核心]`，body 指示
  「把输入透传给核心、把核心结论**原样回流**」。供 `Pipeline` 当独立步骤拉起、可 retry。

## 跑

```bash
cd taifeng
# 需 .env 配 LLM（同 examples/web_ui / step_pipeline）；真实计费，不进 CI
PYTHONPATH=src uv run python examples/dual_track/demo.py
```

demo 用「随机病例号 + 每核心唯一 ⟦标记⟧」证明两条轨道都真实跑通同一批核心：

- **轨道 A**：用 `skill_dispatched` 事件证明 3 核心都被 `main` `call_skill` 派发
  （`stack=['main','intake_core']` …），且 `main` 终报合并了三步结论（含各 ⟦...⟧ 标记）。
- **轨道 B**：`Pipeline` 拉起 3 wrapper 顺跑 → 各 wrapper 原样回流核心标记；再 retry 中间步
  → 断言「上游不动、中间换新 thread、下游级联」。

## 代价（选这条路要知道）

- **多 6 个 skill 文件**（3 核心 + 3 wrapper；外加 1 个 main）。
- **wrapper 多一跳 LLM**：转调核心 + 回流，多一次模型调用与 token；且「原样回流」靠 prompt
  约束，LLM 偶有改写风险（demo 已验证标记保真，生产建议对关键字段做断言或后处理）。
- 若**不需要**自治链，直接用 `examples/step_pipeline`（核心即 entry，省掉 wrapper 与多跳）。
