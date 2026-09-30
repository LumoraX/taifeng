# ADR 0085：输入来源标记——内核记来源、汇总透出，不做裁决

- 状态：Accepted
- 日期：2026-09-30
- 关联：[input-origin 契约](../architecture/capabilities/input-origin.md)；
  [conversation 活文档](../architecture/conversation.md)；ADR 0017 / 0028 / 0057

## 背景

kernel-gap-analysis 把「输入来源 / 污染标记」判为无业务驱动、按规则挂起。此后工具面持续扩大（`http_request`、MCP 的
资源与 elicitation、文件输入、peer 消息），模型上下文里外部内容的比例越来越高，而内核对「这段内容是谁给的」没有任何
记录：业务想实现「读过外部网页之后，写文件先问人」这样的策略，无从得知上下文里有没有外部内容。

这不是产品功能。「内容从哪来」只有把内容送进内核的那一方知道，而且必须在送入的那一刻记下；事后无法补。
命中 ADR 0017 规则①（内核机制）与规则③（内核只定协议，策略走外部）。

## 决策

1. **来源由送入方声明，内核不推断**。业务在 Op 上带 `origin`，工具在 `ToolSpec.output_trust` 上声明。
   内核无从判断一段文本是否可信，推断只会制造虚假的安全感。
2. **未声明 ≠ 可信**。没有标记的条目不计入汇总，但也不被当作可信的证据；汇总只回答「有没有声明为不可信的内容」。
3. **派生内容继承不可信标记**：压缩摘要、`call_skill` 子 thread 的种子消息、`send_message` 发出的 peer 消息。
   否则不可信内容经一次压缩或一次派发就从汇总里消失，标记形同虚设。
4. **汇总交给工具与 hook，内核不裁决**。`ToolContext.extras["input_taint"]` 与 hook 上下文同源。拦截、改为询问、
   记审计，都是业务在 hook / 权限策略里的事。
5. **标记只在 metadata，不进 prompt**。把「此内容不可信」写进 prompt 是一种防御手段，但它改变模型看到的内容、
   改变缓存前缀，且效果因模型而异——属于业务策略，可由业务在注入时自己加。
6. **`label` 不透明**：内核只原样汇总。渠道、租户、数据源这些概念留在业务侧（R1）。
7. **内置的外部取数工具默认声明不可信**（`http_request`、MCP 绑定的工具），与 ADR 0057 对 MCP 工具副作用的
   保守默认同理。只影响 metadata，不改变行为。
8. **strict audit 会话拒绝带标记的 `UserMessage`**。`submission_accepted` 是版本化 DTO，加字段要动 canonical hash
   向量，应随 Journal 的下一次 schema 演进一起做；在那之前显式拒绝，不悄悄丢标记。

## 替代方案

- **内核内置「污染后高危工具需审批」**：哪些工具算高危、污染后是拒绝还是询问，都是业务策略；否决。
- **把可信度并入 `PermissionRequest.metadata`**：权限请求由各工具自行构造，内核改不了业务工具发出的请求；
  而 hook 在所有工具派发前统一经过；否决。
- **按条目粒度追踪「哪次调用受哪段输入影响」**：需要数据流分析，模型内部的信息流不可观测；上下文级汇总是
  能诚实给出的最细粒度；否决。
- **未声明的输入默认不可信**：既有业务的全部输入会一夜之间变成不可信，任何依据汇总的策略都会误伤；否决。

## 后果

- `ToolSpec` 新增 `output_trust`；四个输入类 Op 新增 `origin`（缺省不参与序列化）；`deliver_peer_message` 新增
  可选参数 `origin`。
- `compacted` 条目、子 thread 种子、peer 消息的 metadata 可能多一个 `origin` 键。
- `http_request` 与 MCP 工具的结果开始带来源标记。
- 每次工具派发多一次对 history 的线性扫描。
- 未覆盖：audit 路径的工具结果打标、经 `spawn_skill` 工具发起的 detached spawn 种子继承。
- R1–R5 见契约。

## 验证

`tests/conversation/test_input_origin.py`：打标与读回、形状、身份与其余 metadata 保持、None 原样返回、
畸形 metadata 报错、非法取值拒绝、汇总只计声明为不可信的条目、派生标记与标签拆回、超长截断。
`tests/loop/test_input_origin_wiring.py`：UserMessage 标记落史并到达工具、未声明时汇总为干净、
带不带标记模型请求逐项相同、注入类 Op 带标记、不可信工具结果影响之后的调用、hook 据汇总拦截、
子 skill 种子继承与干净父上下文、压缩摘要继承并落 transcript、audit 会话拒绝带标记的输入、
内置外部工具的默认声明。
