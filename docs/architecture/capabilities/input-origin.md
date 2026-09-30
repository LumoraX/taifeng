# Capability: input-origin

> 状态：Experimental。关联 ADR 0017（规则①/③）/ 0085。
> 实现：`src/taifeng/conversation/origin.py`（数据契约与汇总）；接线见下文「打标点」。
> 经 `taifeng.experimental` 导出 `InputOrigin` / `InputTaint` / `origin_of` / `summarize_taint` / `taint_from_extras`。

## Purpose

模型看到的内容来源各异：用户亲手输入、宿主程序注入、工具从外部取回、其他 agent 发来。外部内容可能夹带指令。
内核**不判断内容是否恶意，也不决定怎么防**，只做两件事：记下每段输入的来源，把上下文里不可信内容的汇总交给业务
的 hook 与工具。要不要在上下文被污染时拦截某个动作，由业务决定（R1）。

## 数据契约

### `InputOrigin`

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `kind` | `user` / `host` / `tool` / `peer` / `derived` | 谁把内容送进来：用户 / 宿主程序 / 工具 / 其他 agent / 内核由已有内容派生 |
| `trust` | `trusted` / `untrusted` | 声明的可信度 |
| `label` | 1–128 字符，可空 | 业务自定义的不透明标签（渠道名、工具名）；内核不解释 |

不接受其他字段。落在条目 `metadata["origin"]`，形状 `{kind, trust, label?}`（无 label 时不带该键）。

- `tag_origin(item, origin)`：返回带标记的新条目（id、payload、其余 metadata 不变）；`origin` 为 None 时原样返回；
- `origin_of(item)`：读出标记；没有返回 None；形状不合法抛 `ValueError`。

没有标记的条目是「未声明」：既不算可信也不算不可信，不计入汇总。

### `InputTaint`

`summarize_taint(history)` 的结果：`untrusted`（是否存在不可信内容）、`kinds`、`labels`（去重、排序）、`item_count`。
`to_dict()` 为 `{untrusted, kinds, labels, item_count}`。

`derived_origin()`：由这些内容派生出的新内容应带的标记——`InputOrigin(kind="derived", trust="untrusted",
label=<标签以逗号拼接，超过 128 字符截断>)`；没有不可信内容时为 None。汇总遇到 `derived` 标记时把标签按逗号拆回。

## Requirements

### Requirement: 打标点

| 输入 | 标记来自 |
| --- | --- |
| `UserMessage` / `InjectUserInput` / `InjectSystemMessage` / `SendToPeer` | Op 的 `origin` 字段（业务声明；缺省 None = 不打标，Op 序列化形状不变） |
| 工具结果 | `ToolSpec.output_trust`（缺省 None = 不打标）；声明后标记为 `{kind: "tool", trust, label: <工具名>}` |
| `call_skill` 子 thread 的种子消息 | 父上下文汇总的 `derived_origin()` |
| `send_message` 工具发出的 peer 消息 | 发送方上下文汇总的 `derived_origin()` |
| 压缩产生的 `compacted` 条目 | 被折叠条目汇总的 `derived_origin()`；随条目落 transcript |

内置工具 `http_request` 与经 MCP 绑定的工具默认 `output_trust="untrusted"`。其余内置工具不预设。

派生继承保证不可信内容不能经压缩、子 skill 或转发「变干净」。

### Requirement: 向工具与 hook 透出汇总

每次工具派发，`ToolContext.extras["input_taint"]` 与 `pre_tool_use` / `post_tool_use` hook 的
`HookContext.extras["input_taint"]` SHALL 为当前 thread history 的 `summarize_taint(...).to_dict()`。
`taint_from_extras(extras)` 把它还原为 `InputTaint`（键缺失视为干净；形状不合法抛 `ValueError`）。

内核 SHALL NOT 依据汇总改变任何派发、权限或可见性裁决。

#### Scenario: hook 在上下文被污染时拦截动作
- **GIVEN** 业务注册了 `pre_tool_use` hook：`input_taint.untrusted` 为真时拒绝 `act` 工具
- **WHEN** 用户提交 `UserMessage(text=<邮件正文>, origin={kind: user, trust: untrusted, label: email})`，模型调用 `act`
- **THEN** 工具未执行，模型收到 `hook_denied: ...`；hook 看到的 `labels == ["email"]`

#### Scenario: 不可信的工具结果影响之后的调用
- **GIVEN** `fetch` 声明 `output_trust="untrusted"`
- **WHEN** 同一 turn 里先调 `act`、再调 `fetch`、再调 `act`
- **THEN** 第一次 `act` 看到干净的上下文；第二次看到 `{untrusted: true, kinds: ["tool"], labels: ["fetch"]}`

### Requirement: 标记不进 prompt

来源标记只存在于条目 metadata。带不带标记，发给模型的请求（system 文本、消息序列、工具集）SHALL 逐项相同（R2）。

### Requirement: strict audit 会话

strict Journal 的 `submission_accepted` 尚无来源标记字段。audit 会话收到带 `origin` 的 `UserMessage` 时 SHALL 以
`InvalidAuditedSubmissionError` 拒绝并落 `submission_rejected`，SHALL NOT 丢掉标记后照常接受。不带标记的输入不受影响。
其余带 `origin` 的 Op 本就在 audit 能力面之外。audit 路径的工具结果不打标。

## R1–R5 影响

- R1：标签不透明；是否拦截由业务 hook 决定。
- R2：标记不进 prompt；不影响缓存前缀。
- R3：汇总随每次工具派发透出；标记随条目落 transcript。
- R4：汇总是对 history 的一次线性扫描。
- R5：标记在条目 metadata 里，冷加载后汇总结果不变。

## 显式边界

| 不做 | 说明 |
| --- | --- |
| 内核内置的拦截 / 降权策略 | 业务经 hook 或权限策略实现 |
| 判断内容是否包含注入 | 内核不解析内容语义 |
| 就地改写类压缩策略的标记传播 | 它们不删条目，条目上的标记原样保留 |
| detached spawn 的种子消息 | 由业务经 `engine.spawn_skill` 发起时 `args` 来自业务；经 `spawn_skill` 工具发起的继承留待需要时补 |
