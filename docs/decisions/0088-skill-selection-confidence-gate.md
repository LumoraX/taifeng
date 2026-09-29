# ADR 0088：按选择置信度分流——低置信候选不可直接派发

- 状态：Accepted
- 日期：2026-09-30
- 关联：[skill-selection-gate 契约](../architecture/capabilities/skill-selection-gate.md)；
  [skill-recall 契约](../architecture/capabilities/skill-recall.md)；
  设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §6 相位 3；ADR 0017 / 0023 / 0024 / 0077

## 背景

相位 2 让 `search_skills` 把候选连同置信度交给模型，内核不据置信度分流。结果是置信 0.2 的候选与 0.9 的候选
对模型而言都是「搜到了」：模型倾向于拿排第一的候选直接派发，低置信误派发要到子 skill 跑完才暴露，
代价是一整个子 turn。设计稿约定的相位 3 是「高置信直接用、中置信先试用、低置信升级」，此前未实现。

命中 ADR 0017 规则②（模型认知回路原语）。

## 决策

1. **三档分流**：`proceed` / `trial` / `escalate`。默认策略 `ThresholdSelectionPolicy` 用两个阈值
   （`tau_high=0.75`、`tau_low=0.4`）加一个并列间距（`ambiguity_margin=0.05`）：置信都够高但彼此难分的候选
   一律降为 `trial`。分流口径是协议 `SelectionConfidencePolicy`，业务可替换。
2. **在派发处强制，不只是提示**。`search_skills` 的结果标注 `route` 是给模型看的；约束力来自
   `call_skill` / `spawn_skill` 里的分流门，位于 `DispatchPolicy.check` 之后、hook 与权限审批之前——
   不合格的派发不应打扰审批人。
3. **`trial` 的默认试用方式是模型自己读说明书**（`read_skill`）。它零额外 LLM 调用，且与既有的懒加载路径
   一致。可选注入 `TrialJudge`，由内核在派发前做一次适配判断；`VerifierTrialJudge` 复用既有的
   `SkillVerifier`。模型已读过说明书时不再问试用门。
4. **`escalate` 不可经试用解锁**。低置信意味着召回本身没有找到对口的 skill，正确动作是换关键词重搜、
   如实报告、或问用户；允许「读一下就能派发」会让这一档形同 `trial`。
5. **全部候选为 `escalate` 时返回 `no_match`**，候选列在 `low_confidence` 里。沿用验证门已有的
   显式 `no_match` 信号，模型不需要学新的约定。
6. **判定由 history 推导，不在 runner 上持有状态**。模型看到了哪份召回结果、之后有没有读过说明书，
   history 里都有；冷恢复、rewind 之后结论自动与 history 一致。既有的 turn 内溯源映射
   （`_register_selection_trace`）不复用：它是内存态，且只记最后一次置信度。
7. **只约束本轮经召回看到的 skill**。白名单里直接选中的 skill 是作者预授权的工作集；上一轮的召回
   不约束本轮——新一轮有新的用户输入，模型的选择依据已经不同。
8. **分流结论不进战绩**。置信度决定的是「要不要先试」，不是「值不值得信」（ADR 0077 不变量 1）。
9. **`spawn_skill` 走同一道门**，否则模型改用分离派发即可绕过。

## 替代方案

- **只在结果里标注、不在派发处拦**：依赖模型自觉，正是相位 2 已经证明不够的做法；否决。
- **`trial` 档由内核自动跑一次沙箱试执行**：设计稿里的「试用」原意。但内核没有通用的无副作用执行环境，
  skill 的副作用取决于它调用的工具；自动试执行等于替业务决定哪些副作用可以接受。改为「读说明书 / 适配判断」，
  真正的试执行留给业务实现 `TrialJudge`。
- **把分流门做成 `pre_skill_dispatch` hook**：hook 拿不到 history，且 hook 链可被业务整体替换；
  分流门需要与召回结果的标注成对出现，做成构造参数一次注入两处；否决。
- **读不到分流结论时一律拦下**：召回结果被截断或改写后模型也没看到完整候选，此时拦下会把
  白名单内的正常派发一并挡住；选择放行并在契约里写明失效方向。

## 后果

- 新增实验层符号：`SkillSelectionGate`、`SelectionConfidencePolicy`、`ThresholdSelectionPolicy`、
  `SelectionCandidate`、`RoutedCandidate`、`TrialJudge`、`TrialVerdict`、`VerifierTrialJudge`。
- 新增事件 `skill_selection_routed`、`skill_selection_gated`。
- `EnginePool.create`、`make_search_skills_tool`、`make_call_skill_tool`、`make_spawn_skill_tool` 各多一个
  可选参数，缺省行为不变。
- 启用后，模型在 `trial` 档会多一次 `read_skill` 往返；配置试用门则多一次 LLM 调用。
- 默认阈值是经验值，未经真实负载标定；标定依据应来自 `skill_selection_gated` 与战绩的对照。
- 未覆盖：真实模型是否遵从「先读说明书」的提示（需真实 LLM 验证）；非白名单 skill 的授权（相位 4）。
- R1–R5 见契约。

## 验证

`tests/skill/test_selection.py`：阈值边界、并列判定与关闭、参数校验、顺序保持、试用门适配、
由 history 推导最近一次分流、跨 turn 不继承、读说明书的先后。
`tests/loop/test_selection_gate.py`：高置信直接派发、`trial` 先拦后放、读在召回之前不算、试用门放行 / 拒绝、
已读不再问试用门、前两名难分、全低置信返回 `no_match` 并拦下、读说明书不解锁 `escalate`、重搜后以最近一次为准、
`spawn_skill` 同样受约束、白名单直选不过门、召回结果未提到的 skill 不过门、上一轮召回不约束、
未启用时输出不变。
