# ADR 0070：审计 resume 按副作用分流收敛结果未知的工具调用；Journal 回放支持 Responses 协议

- 状态：Accepted
- 日期：2026-09-29
- 关联：Amends ADR 0053 §4（未结算 effect 一律 fail closed）与 ADR 0054 §4（只支持 Chat 协议录制）；
  [tool-crash-reconciliation](../architecture/capabilities/tool-crash-reconciliation.md) § strict audit 路径；
  [session-journal-business-integration](../architecture/capabilities/session-journal-business-integration.md) §13；
  [llm-client § Journal 确定性回放](../architecture/llm-client.md)；ADR 0025 / 0045

## 背景

两处缺口都来自前序 ADR 有意留下的边界：

1. **审计 resume 遇到结果未知的工具调用只能拒绝**。ADR 0053 让审计 Session 能从 Journal 接管续跑，但悬空的
   `tool_intent_committed`（崩溃在工具执行途中）与已 durable 为 `unknown` 的 `tool_outcome_committed`（取消 / 超时判
   不清、Session 当时即冻结）一律以 `audit_resume_recovery_required` 拒绝，且「不提供把 UNKNOWN 改判为已知结局的写入
   路径」。结果是：即便工具提供了回查函数、或本就幂等，这个 Session 也只能废弃。非审计路径（ADR 0045）早已按副作用
   分流：可安全重发 / 回查 / 交人。
2. **Journal 回放拒绝 Responses 协议录制**。Responses 输入项带 `sample_id` / `origin_sample_id`，按
   `{thread_id}:{submission_id}:turn:{i}:llm:{j}` 派生，每次运行不同，录制的 `canonical_attempt_sha256` 无法在回放中复算。
   ADR 0054 因此在构造期拒绝，并把 provider 专有回传状态（thinking 签名、Gemini `extra_content`、Responses 密文）列为
   「不还原」。

## 决策

### A. 审计 resume 的工具收敛

1. **分流规则与 ADR 0045 同序**，只作用于 root thread 的调用（子 thread 的调用必然伴随未结算的 skill 派发，整体仍
   fail closed）：有 `ToolSpec.reconcile` → 回查；否则悬空调用在「intent 落账声明」与「当前注册声明」**都**属
   pure / idempotent 时判可安全重发；其余交人。回查查不清 / 抛错 / 超时 / 返回值违约都视同 unknown。
2. **结论写成新记录，不改写历史**：新 record type `tool_recovery_committed`（`ToolRecoveryCommittedV1`，含 basis /
   verdict / 回查原始结论 / 落账时的 effect_kind / 接管 operation id），悬空调用同 batch 补一条
   `function_call_output` 会话项（ordinal 1，与 live outcome 的 ordinal 0 永不相撞）。整批用接管后的 lease 原子追加，
   hash chain / epoch / durable 语义由 core 照常保证；core strict verify 只校验结构，新记录天然被接受。resume 扫描把它
   视为对应 intent（及被改判的 unknown outcome）的终态；新记录按 DTO 严格校验，形状违约即 `audit_resume_journal_invalid`。
   旧 Journal 没有这种记录，冷读行为不变。
3. **回查只在持写者锁之后调用**：锁证明原 writer 已退出，回查不会与仍在运行的原调用赛跑。只读预检仍保留：
   无回查、不可安全重发、又没有人可问的调用在预检即拒绝，不写接管记录。
4. **全有或全无**：任一调用仍需人裁决，本次一条恢复记录都不写，拒绝并只列出需要人处置的 record。部分落账会让
   「本次 resume 到底改判了什么」难以复核，而恢复记录本身并不紧迫。
5. **已有 durable 结果的调用只接受两种结论**：回查确认未执行（模型已看到的错误结果与事实一致），或人接受未知
   （`abort`）。回查确认已完成、或人 `provide` 真实结果，都需要纠正模型已看到的那条结果——补第二条
   `function_call_output` 会破坏配对，改写原条目违反 append-only；这两种情况继续交人。
6. **「交人」在审计模式的表达**：审计会话不能挂起（capability gate 拒绝 HITL / suspension，也不接受 `Resume` Op），
   所以不复用 `TOOL_OUTCOME_UNKNOWN` 挂起，而沿用 ADR 0053 的既有语义——resume 拒绝并列出 record 就是交人。新增
   `AuditConfig.tool_outcome_resolver`：下次 resume 接管时向业务回调征求裁决（`provide` / `abort`），裁决以
   `ActorRef(kind="operator", principal_id=<operator_id>)` durable 落账；返回 None = 尚无裁决、继续拒绝；回调异常原样
   上抛（先释放 lease）；裁决不适用（如对已有结果 `provide`）→ `audit_resume_resolution_invalid`。
7. **不支持 `retry` 裁决、不自动执行工具**：审计模式的 effect 只能发生在有 durable intent 的 turn 内。需要重发时，
   让模型在续跑的 turn 里自己重调（「可安全重发」的回填文本即为此），新调用有自己的 intent / outcome。
8. **`EnginePool(tool_recovery="report")` 不作用于审计模式**：自动把 UNKNOWN 补成「结果未知」再续跑，正是 ADR 0053
   要防止的情形。

### B. Responses 协议录制的确定性回放

1. **判据：采样 id 一致双射重命名下逐字节相同**。采样 id 从不进入 provider wire（各 provider 只按输入项顺序与内容
   组装请求），只在内核里归组；因此对模型而言，两个请求在一致重命名下相同即是同一请求。不符合派生语法的 id
   （`legacy:*`）本就确定，不参与重命名。
2. **两段式匹配**：
   - 定位：请求安全投影（与 Journal `api_request_safe` 同形）中的运行派生采样 id 按首次出现顺序换成位置占位符，
     算摘要找候选；
   - 复核：把**完整**请求（含脱敏掉的图片正文与 provider 密文）按位置映射改写回录制时的采样 id，按录制侧同一
     preimage（`canonical_attempt_digest`）复算，必须与录制的 `canonical_attempt_sha256` 逐字节相等。
   第二段保证不因脱敏或重命名放宽匹配：密文 / 图片正文不同、采样归组不同，都判分叉（`ReplayDivergenceError`，
   消息标明「脱敏内容或归组不同」）。Chat 录制走同一算法（其输入项只有确定的 `legacy:*` id，等价于原先的摘要相等）。
3. **provider 回传状态按录制还原**：Responses 的 `normalized_items` 原样经 `normalized_output` 交回，reasoning 的
   `encrypted_content` 随之还原；Chat 录制从同一响应 batch 的会话项（`causation_id` 指向该响应）还原
   `provider_reasoning`（thinking 签名）与 function_call 的 `extra_content`。不需要扩展 Journal schema——这些状态本就
   durable 在会话项里。不还原的话，续传请求与录制不一致，回放在第二次请求即分叉。
4. **能力声明**：回放 client 默认按录制协议声明（Responses 接受持久化 provider state）；录制 client 若声明了图片等
   会影响 prompt 组装的能力，调用方须以 `capabilities=` 显式传入同样的声明。录制混杂两种协议、或显式声明与录制协议
   不符，构造期 `ReplayUnsupportedError`。

## 否决的方案

- **回放侧按录制的派生规则直接重建录制 id**（把新运行的 thread / submission 映射成录制的）：需要从 Journal 推断「新
  运行的第 N 个 submission 对应录制的哪一个」，并发子 turn 下没有稳定对应；位置映射在每个请求内部自洽，无需跨请求状态。
- **只比安全投影、不复核完整摘要**：脱敏字段（密文、图片正文）不同的请求会被误配——为了「能跑」放宽匹配。
- **把 UNKNOWN 自动补成「结果未知」再续跑**（`report` 模式进审计）：见 ADR 0053 §4。
- **部分落账**（能收敛的先写、其余拒绝）：见决策 A.4。
- **复用 `TOOL_OUTCOME_UNKNOWN` 挂起**：审计会话的 capability gate 明确拒绝 suspension，且 `Resume` Op 不在审计能力面内；
  为此放开挂起需要把整套 HITL 纳入审计契约，远超本缺口。

## 后果

- 审计 Session 在工具执行途中崩溃后，可回查 / 幂等的调用在 resume 时自动收敛；其余调用有了一条可审计的人裁决路径。
- 新增 resume code `audit_resume_resolution_invalid`；`AuditResumeError.record_ids` 在 `recovery_required` 时只列出本次
  无法自动结算、需要人处置的 record（存在工具以外的未结算 effect 时列出全部未结算 record）。
- `thread_resumed.recovered_tool_calls` 在审计 resume 时同样透出处置结论，新增取值 `operator_resolved`。
- 需回查或需征求 resolver 才能判定的调用可能在接管后才被拒，epoch 随之 +1（与「resume 后 Engine 构造失败」同类，不影响正确性）。
- 实验层 API 变化（ADR 0066）：`RecordedCall` 新增必填 `api_request_safe` 与可选的回传状态字段；
  `JournalReplayClient(calls, *, capabilities=None)` 与 `from_records(..., capabilities=)`；实验层导出
  `AuditToolOutcomeRequest` / `AuditToolOutcomeResolution`。

## 验证

- 审计 resume：`tests/loop/test_audit_resume_tools.py`（引擎级：handler 执行途中关闭 core 模拟进程死亡，另一 core 实例
  resume）覆盖回查完成 / 回查未执行 / 回查查不清与抛错 / 幂等可安全重发 / 非幂等预检即拒不写接管 / 人 provide 以
  operator actor 落账 / 人 abort 后二次崩溃冷读新记录视为已结算 / 已 durable unknown 的 outcome 回查未执行只落结论 /
  对已有结果 provide 判 invalid / resolver 抛错与返回 None；`tests/loop/test_audit_resume_resolution.py` 覆盖裁决 DTO
  构造期校验；`tests/loop/test_audit_resume_scan.py` 覆盖扫描结算；
  `tests/conversation/journal/test_recovery_records.py` 覆盖 DTO 组合矩阵与 core verify / 冷读。
- 回放：`tests/llm/test_journal_replay_responses.py`（真实 `OpenAIResponsesClient` / `AnthropicClient` 经 MockTransport
  在审计 pool 录制，新 thread / submission 回放）覆盖两轮 Responses 会话全部消费且密文还原、密文被替换判分叉、输入不同
  判分叉、Chat thinking 签名还原后续传请求一致；`tests/llm/test_replay_match.py` 覆盖重命名等价、归组差异、复核、
  内核采样 id 派生与匹配语法的同步守护。关闭重命名 / 关闭复核 / 关闭签名还原三项变异均使对应测试变红。
