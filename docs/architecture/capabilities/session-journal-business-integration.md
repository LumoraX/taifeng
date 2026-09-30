# SessionJournal 普通业务主链接入能力契约

> 状态：Experimental。关联 ADR 0025、ADR 0053、ADR 0070。依赖 `session-journal-core`（Phase 1 + Phase 2 写者接管）。

## 1. 范围

本能力覆盖显式启用审计的新 Session，以及从 Journal 接管恢复的已有 Session（§13）：结果未知的工具调用在接管时
按副作用分流收敛（§13.1），从未登记意图的调用判未执行（§13.2），被中断的同步 `call_skill` 派发沿派发树自底向上
收敛（§13.3），其余未结算 effect 仍 fail closed：

```text
UserMessage → LLM → 基础 Tool / 同步 call_skill → assistant
采样之间：折叠式上下文压缩 / 预算提示（§15）
工具调用处停下等人（审批 / 填表 / 给数据）→ Resume → 续跑（§17）
分离式派发：子 skill 在独立子 thread 上后台运行（§18）
join-barrier：一批派发全部结束后起聚合 turn（§19）
peer 消息：同一 Session 里的 agent 之间点对点发消息（§20）
```

SessionJournal 是执行事实和对话项的唯一可靠事实源。hot history、MessageStore 和 EventMsg 都是
Journal durable ack 之后的内存态或可重建投影，不得领先 Journal，也不得被声明为第二事实源。

未启用 audit 的 EnginePool/AgentEngine/MessageStore 行为保持不变。hook 与权限裁决见 §16，
在工具调用处停下等人作答的挂起与恢复见 §17，分离式派发见 §18，join-barrier 见 §19，peer 消息见 §20。
本阶段不支持其余原因的挂起（子 skill 挂起、失败处置、资源护栏、带到期时间的挂起）、手动压缩与溢出自愈、原地改写条目的压缩策略、rewind、memory、instruction 更新、hooks、orchestration、
后台 shell 任务、
LLM attempt / submission 未结算 effect 的 repair/unfreeze、Timeline/export 通用 redaction、
加密、WORM 或外置 blob。LLM request intent
的写入前 data minimization 是本契约 §8 的强制安全边界，不属于上述未实现的投影视图 redaction。

## 2. 唯一事实源与提交顺序

进入对话历史的事实必须表示为 `conversation_item`，并与领域 outcome 在同一个 batch 提交：

```text
submission_accepted + conversation_item(user_message) + submission_applied
llm_response_committed + conversation_item(reasoning/assistant/function_call...)
tool_outcome_committed + conversation_item(function_call_output)
skill_dispatch_finished + thread_terminal + conversation_item(skill_outcome)
context_compacted + conversation_item(compacted)
budget_hint_injected + conversation_item(system_injection)
hook_evaluated          （单条；先于它所约束的动作）
permission_decided      （单条；先于它所约束的动作）
```

只有覆盖这些 record id 的 `JournalAck` 返回后，调用方才能：

1. 更新 hot history；
2. 更新 MessageStore 物化投影；
3. 发布受 checkpoint 约束的 LLM delta；
4. 开始下一个 effect 或 terminal transition。

Tool outcome 不得重复写 LLM response 已记录的 `function_call`；output 通过稳定 item/call identity 引用
唯一 call。

## 3. 版本与 identity

业务 payload 使用 frozen、`extra="forbid"` 的版本化 Pydantic DTO，并在进入 core 前转成 canonical
JsonValue。除明确升级的 record 外，当前 payload 均为 V1、含 `payload_version=1`；
`llm_request_committed` reader 必须先检查 `payload_version`，将 `1` 解析为只读兼容
`LlmRequestCommittedV1`、将 `2` 解析为 `LlmRequestCommittedV2`，其他版本 fail closed，writer 只允许产生
V2。现有 Phase 1 初始化三记录是 V0 canonical vectors，保持原 bytes 和 record id。

| 对象 | Identity |
| --- | --- |
| submission | `submission_id` |
| root/child turn | `{thread_id}:{submission_id}:turn:{turn_index}` |
| LLM logical call | `{turn_id}:llm:{iteration}` |
| LLM attempt | `{llm_operation_id}:attempt:{retry_ordinal}` |
| Tool call | `{turn_id}:tool:{call_id}` |
| 上下文压缩 | `{turn_id}:compaction:{ordinal}` |
| 预算提示 | `{turn_id}:budget_hint:{ordinal}` |
| hook 裁决 | `{turn_id}:hook:{ordinal}` |
| 权限裁决 | `{turn_id}:permission:{ordinal}` |
| Skill dispatch | `{tool_operation_id}:skill:{target_skill_id}` |

除初始化 V0 外，record id 固定为：

```text
{operation_id}:{record_type}:{attempt_id-or-none}:{ordinal}
```

相同 id 与相同完整 record 是幂等重试；相同 id 与不同内容必须冲突。

## 4. SessionAuditCoordinator

每个 active Session 恰有一个 coordinator 和一个 SessionJournal lease。root 与全部 child thread 共用该
coordinator，通过 thread/turn/call lineage 区分。

coordinator 必须：

- 串行 append，并只从 durable ack 推进 expected seq；
- 在 effect 前检查 health/lifecycle gate；
- 第一次 Journal IO、integrity 或 ack-uncertain 失败时保存稳定首因、关闭 effect gate、设为
  `RECOVERY_REQUIRED` 并取消 Session root/children；
- 让其他 Session 的 coordinator 保持独立；
- 把投影失败标成 stale，但不冻结 Journal execution；
- 通过唯一、幂等的 finish future 提交 terminal records 并释放单 Session lease。

effect 遵循：

```text
durable intent → at-most-one live dispatch → durable outcome or UNKNOWN
```

不承诺跨进程 exactly-once。

## 5. core 单 Session 关闭

```python
async def close_session(self, lease: SessionLease) -> None: ...
```

`close_session` 必须在 registry/per-session lock 下验证 session id、writer id、writer epoch、lease id，等待该
Session 在途 append，标记 writer closed，并只移除该 writer。错误 lease 必须拒绝；其他 Session writer
继续可写。

`close_session` 不写 `session_ended`。正常终结由 EnginePool 先提交领域 terminal batch，再调用一次
`close_session`。全局 core 归调用方所有，EnginePool 不调用 `core.close()`。

## 6. Bootstrap 与投影

audited bootstrap 固定顺序：

1. 静态 capability validation；
2. 预分配 root thread id；
3. `create_session` durable 提交 V0 初始化三记录；
4. 创建 coordinator；
5. projector 用同一 id 建立带 `audit_required`、Journal Session id、schema version 的空 transcript；
6. 成功后才构造并启动 Engine。

projector 只接受 durable ack 覆盖的 `conversation_item` envelope，按 Journal seq 写默认 JSONL 物化层并
维护 projected seq。投影失败返回 stale；投影可以删除并从 Journal 重放。带 audited marker 的 transcript
不得走 legacy resume。

投影异常按事实边界分类：

- scope 准入的非 identity `ProjectionLifecycleError`、metadata/directory IO、snapshot 解析以及 append
  target/path race 属于可重建物化层故障，projector 返回稳定 stale；Journal health、hot history 和
  effect gate 不变；
- Journal Session、thread、audited metadata 或 envelope/ack 顺序不变量失败抛 `ProjectionOrderError`，
  Engine 同步冻结 Session coordinator，关闭 effect gate，不得伪装成普通 stale；
- `ProjectionIdentityError` 只表示前一类 audited identity 不变量；普通文件 inode/path 竞争仍是可恢复
  `ProjectionLifecycleError`，避免把 derived target 的并发替换升级成 Journal 故障。
- projector 若仍泄漏未分类的普通 `Exception`，Engine 也必须 fail-closed；`CancelledError` 原样传播且
  不冻结，`KeyboardInterrupt` / `SystemExit` 作为进程级 fatal 不得被普通异常边界捕获。

## 7. Submission 与 lifecycle

audited `AgentEngine.submit()` 与 lifecycle 共用同一个 admission lock。UserMessage 必须先 canonicalize 并
durable 提交 acceptance batch，再把携带 ack 的 token 入 actor queue；禁止 enqueue-first。队列内因此不
存在未 accepted submission。

公开 legacy `Submission` 保持可变的 `id + op` 序列化/schema。audit-required 路径在首次 await 前把
submission id、时间、文本和附件复制为内部 frozen snapshot；随后在独立 admission sequencing lock 中分配
唯一 durable `turn_index`，并保持该锁直到 acceptance ack 与 enqueue 完成。该锁不持有 Session lifecycle
lock，因此首 turn 阻塞时并发排队仍得到单调且不重复的 index。

无法形成 canonical V1 的 UserMessage 使用安全结构描述计算 `input_descriptor_hash`，只 durable 写一条
`submission_rejected`，不保存非法原文、任意 `repr` 或 traceback，也不冻结 healthy Session。rejection
append 失败仍按 Journal uncertainty 冻结；若 intake 已是 FINISHING/CLOSED，则 lifecycle 优先且不写
rejection。pending rejection 与 finish 共用 lifecycle reservation，terminal seal 不会越过其 durable 结果。
安全结构 walker 只读取 exact builtin，限制深度、全局节点数、单容器条目数与字符串/键长度，并把超出
RFC 8785 safe-integer 域、超长值、超宽容器、环、深度溢出、非法键和 unsupported object 映射为稳定
bounded marker；不得调用任意对象的 `repr` / `str` / `hash` / iterator hook。

actor 应用 queue token 前必须重建完整三-envelope receipt，并复用 Journal strict codec 重算每条
`payload_hash` / `record_hash` 与 batch `previous_hash` chain，同时核对 ack ids、连续 seq、tail hash、
Session/writer identity 和业务 lineage；只协调修改业务 payload 而保留旧 hash 仍必须 fail-closed。

lifecycle 是 `OPEN → FINISHING → CLOSED`：

- 进入 FINISHING 的胜者关闭 intake、快照全部 durable-accepted queued/in-flight submissions，并创建唯一
  finish future；
- accepted-but-queued work 必须在 `session_ended` 前收敛；
- `AcceptedWork.complete()` 通过 coordinator async callback，在 shield 与 lifecycle lock 内校验
  reservation/work identity、立即退休 map entry 并 set completion Event，只跟踪 pending 或
  accepted-incomplete work；已快照 finish waiter 不丢唤醒，double complete 幂等，CLOSED 后晚完成只清
  unresolved introspection、不复活 Session；
- durable acceptance 已得到 definite ack、但并发 freeze/CLOSED 使 ownership 无法交给 caller 时，必须在
  抛错前 shielded 精确退休该未交付 work；durable fact 仍保留供 recovery，finish 不得等待隐藏 token；
- strict receipt load 的 `KeyboardInterrupt` / `SystemExit` / `CancelledError` 会先冻结稳定首因并
  cancellation-independent 退休 work，再把原异常类型向上传播；不得改写成 `SessionAuditFrozenError`；
- definite ack 后的 queue handoff 若因 bounded backpressure cancellation、actor 已终止或其他异常未取得
  ownership，则以稳定 `accepted_work_handoff_failed` 进入 recovery-required，并在抛错前 shielded 退休
  未交付 work；fatal/cancel 原类型继续传播，已经成功入队的 token 不得同时按失败退休；
- audited mailbox 在 `queue.put` 前登记 token；actor dequeue 后先标记 claimed，但 mailbox 保留
  reservation，直到 child outer retirement `finally` 已安装并在同一 lock 完成 started handshake。
  actor finalizer 原子关闭 mailbox、唤醒 blocked put、快照全部 registered/claimed token 并
  cancellation-independent 冻结/退休；started token 只由 operation finally 收敛。finalizer 与 child
  handshake 只能有一方取得 retirement ownership，迟到 child 不得应用输入或双退；
- EnginePool 的 graceful shutdown 与 admission sequencing lock 串行：先关闭 intake，再把内部 Shutdown
  排在此前 accepted token 之后；actor 在读取下一 queue item 前不仅安装 operation ownership，还等待该
  token 的 hot-history + projection application checkpoint，确保 terminal 不越过已 accepted input；并发
  audited runner 收尾按完整 `ResponseItem` 身份（`id/kind/thread_id/payload/created_at/metadata`）
  append-only 幂等合并，旧 history snapshot 不得覆盖后来 durable-applied 输入；相同 id 的完整内容不一致
  必须以稳定 `audit_history_item_conflict` fail-closed，且合并整体成功前不得回写 cache anchor、rewind
  checkpoints、prompt fingerprint、compaction count 或 usage；
- FINISHING/CLOSED 后的新请求不得 durable accept 或 enqueue，返回 `SessionFinishingError`；
- 并发 release/close 只等待同一 canonical future value，但每个 caller 得到对象与嵌套 failure 独立的
  防御性副本；并发不同 Shutdown id 在 acceptance 前拒绝。

上述 hot-history merge 是 Task 7 以 Journal seq 建立 authoritative writeback 前的临时 audit-only
边界；当前内存列表顺序受 durable application 与 runner 完成顺序影响，不承诺等于 Journal seq。legacy
路径仍沿用单 runner 整表回写，不受该临时合并规则影响。

accepted work 收敛后，finish 持有 append lock，重读最新 committed thread-terminal 集合，设置不可逆
terminal seal 并直接提交去重后的 `thread_terminal* + session_ended`。seal 或 CLOSED 后的普通 append 必须
在 core dispatch 前拒绝，因此 `session_ended` 是最终 durable record。

finish result 与 `SessionAuditSnapshot` 分开报告两个事实：

- `audit_complete`：terminal batch 收到 definite durable ack；
- `lease_released`：normal/emergency close 已确定释放 lease。

两者成功为 `true/true`；terminal ack 后 close 失败为 `true/false`，保留 terminal ids 且 health 为
recovery-required；terminal 失败但 emergency close 成功为 `false/true`；两者失败为 `false/false`。
`close_session()` 只释放资源，不能推翻或制造 `session_ended` 事实。

CancelTurn 只取消 target turn 及其 child effect subtree。freeze 和 Shutdown 才取消 Session root。Journal
不可用时 CancelTurn/Shutdown 可作为安全降级动作执行，但不得伪造 durable record，health/introspection
必须报告 `audit_complete=false`；若 emergency close 确定成功则独立报告 `lease_released=true`。

## 8. LLM request intent data minimization 与 checkpoint-before-delta

所有新写入的 `llm_request_committed` 使用 `LlmRequestCommittedV2`；V1 只用于读取已有 records，不得继续
产生。V2 在 `effect_kind/idempotency_key/reconciliation` 之外固定包含：

```python
class RedactionEntryV1:
    path: str  # RFC 6901 JSON Pointer
    kind: Literal["image_base64", "provider_encrypted_content"]

class LlmRequestCommittedV2:
    payload_version: Literal[2]
    turn_index: int
    iteration: int
    provider: str
    model: str
    api_request_safe: Mapping[str, JsonValue]
    redactions: tuple[RedactionEntryV1, ...]
    canonical_attempt_sha256: str  # 64 位小写 hex
    effect_kind: str
    idempotency_key: str | None
    reconciliation: str
```

`api_request_safe` 从 provider-neutral `ApiRequest.model_dump(mode="json")` 生成，不是最终 provider wire body：

- image part 删除 `base64_data`，保留 `type/media_type/size/sha256/detail`，并加入
  `content_redacted={"kind":"image_base64","redacted":true}`；
- provider-state payload 删除 `encrypted_content`，保留已批准字段，并加入
  `provider_state_redacted={"kind":"provider_encrypted_content","redacted":true}`；
- 若原对象已含将要生成的 marker key，或发现已知敏感 key 出现在未批准的结构位置，必须 fail closed；
- 非敏感字段逐值保留，不得 trim 或用 `repr`/`str` 改写。

每个被删除值产生一条 manifest entry。`path` 指向原完整 `ApiRequest` 中被删除字段，按 RFC 6901 对 `~`/`/`
转义；entries 必须按 path 的 UTF-8 bytes 升序排列，path 不得重复。当前只允许上述两个 kind，未知 kind 拒绝。
无敏感字段时 `api_request_safe` 与原 request 相同且 `redactions=()`。

digest preimage 精确为：

```json
{"provider":"<provider>","model":"<model>","api_request":<脱敏前 ApiRequest.model_dump(mode="json")>}
```

对该对象使用仓库 RFC 8785 canonical JSON bytes 后计算 SHA-256。它绑定 provider-neutral attempt intent，
不声称是最终 wire-body digest，也不单独证明已经 dispatch；关联同一 `request_record_id` 的 attempt
checkpoint 只证明 attempt 已进入受审计 client 执行阶段并形成 durable 已知/未知终态，也不证明请求字节
实际离开进程。

Canonical conformance vector：

```text
bytes = {"api_request":{"cache_breakpoints":[],"input_items":[{"content":"ping","output_index":null,"role":"user","sample_id":null,"type":"message"}],"max_output_tokens":null,"messages":[{"content":"ping","reasoning":null,"role":"user","tool_call_id":null,"tool_calls":null}],"metadata":{},"model":"gpt-5.6-luna","parallel_tool_calls":true,"reasoning_effort":null,"response_format":null,"system_prompt":[],"temperature":null,"tools":[]},"model":"gpt-5.6-luna","provider":"codex"}
sha256 = ca2f8ff5fcb8a45b8725d71e1943da15346e5ae2006adc6232e4b1cbd8fc13eb
```

attempt observer 只能取得 V2 安全投影/manifest/digest；完整敏感 request 只允许在内存中传给 provider client，
不得进入 observer、request capture、日志或 telemetry。

### 8.1 checkpoint-before-delta

audit 模式只接受能暴露每个真实网络 attempt 的 ModelClient。每个 attempt：

1. `before_attempt` durable 写 `llm_request_committed`；
2. ack 后才 dispatch；
3. provider event 先缓冲；
4. complete/error/cancel 后在 cancellation-independent bounded shield 中 durable 写
   `llm_response_checkpoint`；
5. checkpoint ack 后才按原序发布 delta 或开始下一 attempt；
6. 最终 logical response 与 conversation items 原子提交。

observer/ack 不确定使 attempt 为 UNKNOWN、冻结 Session、禁止 retry。未来 provider 若增加内部 retry，必须
显式接入 observer 才能进入 audit 模式。

## 9. Tool 终态收敛

audit ToolSpec 必须声明 `effect_kind`、`reconciliation` 与布尔值的 `can_suspend`。hook 与权限裁决先落账再生效（§16）。
停下等人作答的调用在答复之前保持未结算（§17）；未声明 `can_suspend=True` 的工具自行挂起是能力违约：
记 error 终态后冻结 Session。

一批 call 先按 call index 原子提交所有 executable/rejected intents，再 dispatch。每个 committed intent
必须得到一个 terminal outcome：`success | error | rejected | cancelled | unknown`。

- 只有未越过 dispatch gate，或 runtime 明确证明 effect 未发生/已知终止，才是 cancelled；
- dispatch 后仅收到取消异常、超时或外部结果不明时是 unknown；
- parent cancel 时 best-effort 取消 sibling，但必须在独立 shield 中收敛全部 intents；
- outcomes 与 function call outputs 按 call index 原子提交；
- 任一 unknown 冻结 Session，不进入下一次 LLM。

## 10. 同步 call_skill

outer Tool intent durable 后提交含完整 definition/body/hash/arguments/provenance 的 `skill_selected`。quota
rejection 写 finished(rejected)，不创建 child。

quota 通过后预分配 child thread id，原子提交 started/thread-created/thread-bound/child-seed。ack 后 child
使用 hot history 运行；投影失败只标 stale。child success/error/cancel 后原子提交
finished/thread-terminal/skill-outcome，outer Tool 随后提交自己的 outcome/output。

child turn identity 包含 child thread id 和 parent submission id。unexpected suspension 是 capability 声明
违约：在产生 HITL effect 前拒绝，并在可能时写 error 后冻结。

## 11. 稳定错误与附件

`StableErrorV1` 字段：`code`、稳定 `class_name`、`failure_class`、可选安全 message/descriptor hash、
`retryable`。禁止持久化任意 `repr()`、traceback、内存地址或 secret。

附件只接受完整 inline base64 content，并在 acceptance 前校验 media type、size、SHA-256、单项和总大小
上限。临时路径、引用型输入、缺失正文、digest/size 不符或超限写安全 submission rejection，不冻结。

附件有两种 durable 形状，按 `kind` 区分（ADR 0095）：

| `kind` | DTO | 字段 |
| --- | --- | --- |
| `file` | `FileAttachmentRecordV1` | `media_type`（首批仅 `application/pdf`）、`size`、`sha256`、`encoding`、`content`、`filename`（展示名，可空；不得含路径分隔符与控制字符） |
| 其余 | `AttachmentV1` | `kind`、`media_type`、`size`、`sha256`、`encoding`、`content`、`detail` |

去掉 `payload_version` 后即是对话项里的附件形状，与非审计路径逐项相同。图片附件的 canonical bytes
没有因引入文件附件而改变。文件的准入与非审计路径同一口径：模型声明支持文件输入、文件策略启用、
数量与大小在策略之内、PDF 结构合法。

工具结果里的图片附件随 `function_call_output` 对话项落账（完整正文）；`tool_outcome_committed.data`
只记摘要 `{"attachments": [{kind, media_type, size, sha256}]}`。附件在落账前先过图片策略，再过
Session 的附件字节上限；不合格时这次调用的结果变成错误（`tool_attachment_rejected: …`，
`status=error`），不落图、不冻结。

LLM request 落账时图片与文件的正文都按 §8 脱敏（`image_base64` / `file_base64`）。

## 12. Capability gate

| 维度 | 允许 | 拒绝 |
| --- | --- | --- |
| Op | UserMessage、CancelTurn、Shutdown、Resume（§17） | 其他 Op |
| Session | 新建；resume（Journal 接管，§13；root 工具调用的 UNKNOWN 按 §13.1 收敛） | 已终结 Session、存在无法收敛的未结算 effect、writer 仍存活 |
| Store | 默认 JSONL 可重建投影 | custom store/directory、IndexHook |
| Hook/approval | 内核的 `HookRunner`；内核的 `PermissionPolicy`（规则、可复用授权、当场作答或挂起式的 prompter）（§16、§17） | 其他类型的 hook 运行器 / 权限策略对象 |
| Context | 无压缩策略，或全部策略声明 `audit_support` 为 `fold` / `fold_model`（§15） | 未声明或原地改写条目的压缩策略、ContextEngine、rewind、memory、instruction |
| Skill | atomic/composite、同步 call_skill | orchestration、子 skill 内的挂起 |
| Spawn/peer | 分离式派发：`spawn_skill` / `kill_skill` / `join_skill` / `wait_peer` / `wait_any`（§18）；join-barrier：`await_skills`（§19）；peer 消息：`send_message`（§20） | 后台 shell 任务（`run_in_background` / `wait_for_task`）；`SendToPeer` Op（§20.5） |
| LLM | attempt-observable | opaque attempt/retry |
| Tool | audit metadata 完整；结果可带图片附件；声明 `can_suspend=True` 的工具可以停下等人（§17） | metadata 缺失；未声明却自行挂起（运行期冻结） |
| 附件 | 用户消息里的图片与文件（PDF）、工具结果里的图片 | 引用型输入、Data URL、超限、策略未启用或模型不支持的模态 |

静态配置在 EnginePool 构造期验证，Op 在 submission gateway 验证，动态 effect 在 TurnRunner gate 再验证。
拒绝必须发生在 effect 前。

## 13. Resume（Journal 接管恢复）

`EnginePool.get_or_create(session_id, entry_skill_id, resume_thread_id=...)` 在 audit 模式下按固定顺序恢复；
任一步失败都不构造 Engine、不产生 effect：

1. metadata-only 读取 `resume_thread_id` 投影的 audited marker，取得 `journal_session_id`；必须等于请求的
   `session_id`；
2. 只读预检（不持锁）：strict 读取 committed envelopes，做下述第 4 步校验——注定被拒的请求不写接管记录；
3. `open_existing(journal_session_id, writer_id=AuditConfig.writer_id, operation_id="<session>:resume:<随机>")`
   以 epoch+1 接管（跨进程写者锁保证原 writer 仍存活时 Busy）；
4. 持锁后权威重读：初始化 batch 的 root thread 必须等于 `resume_thread_id`；收集**未结算 effect**——
   `llm_request_committed` 无对应 `llm_response_checkpoint`、`tool_intent_committed` 无对应
   `tool_outcome_committed` / `tool_recovery_committed`、`skill_selected` 无同 operation 的
   `skill_dispatch_finished`、`submission_accepted` 无对应 `submission_applied`，或任一终态已 durable 为
   `unknown` 且未被 `tool_recovery_committed` 改判（ADR 0025：未匹配 intent 一律 UNKNOWN）。root thread 的工具
   调用按 §13.1 收敛并把结论原子追加；已随模型回复落账、却从未登记意图的调用按 §13.2 收敛；被中断的同步
   `call_skill` 派发连同其子 thread 上的调用按 §13.3 收敛；其余任一未结算 effect，或任一工具调用仍需人裁决，
   即 fail closed。恢复从不自动重复任何 effect；
5. 用 root thread 已提交 `conversation_item`（含 §13.1 补写的 output）按 seq 重建 initial history；audited turn
   index 从 Journal 已 accepted 的最大值 +1 续编；
6. coordinator 使用新 lease 与 `expected_seq` = 最后一次 ack 的 `last_seq`（有恢复 batch 时为它，否则为接管 ack）；projector 复用既有投影 thread，并以
   Journal 为真相核对：投影是 Journal items 的前缀（含相等）则补齐缺失后缀、watermark = 最后 conversation
   seq，并关闭 generation replay 窗口；投影领先 / 分叉只标 stale（不改写、不冻结，可删除重放）；Session
   identity 不符按 audited 不变量违约拒绝；
7. 构造并启动 Engine，发 `thread_resumed`。

第 3 步之后的任何失败（含 Engine 构造 / warmup 失败）只释放 lease，**不写** `thread_terminal` /
`session_ended`——resume 失败不是 Session 的终结，之后可再次接管（epoch 继续递增）。resumed Session 的正常
release 与新建 Session 相同：terminal batch + `session_ended` + `close_session`。

audited Session 已在本 pool live 时，`resume_thread_id` 等于其 root thread 则返回缓存 Engine，否则拒绝。

所有拒绝抛 `AuditResumeError`（`code` 稳定，`record_ids` 仅 recovery_required / resolution_invalid 时非空，不携带
底层异常文本）：

| code | 含义 |
| --- | --- |
| `audit_resume_projection_unavailable` | pool 无默认 JSONL 投影 store |
| `audit_resume_marker_missing` | thread 不存在或不是 audited 投影（禁止把 legacy thread 升级为审计） |
| `audit_resume_marker_invalid` | marker 读取失败或 directory / 文件 marker 不一致 |
| `audit_resume_session_mismatch` | marker 的 `journal_session_id` 不等于请求 Session |
| `audit_resume_journal_missing` | Journal 文件不存在 |
| `audit_resume_journal_invalid` | Journal 完整性 / 解码违约，或缺初始化 batch |
| `audit_resume_busy` | 另一进程 / 实例仍持有 writer |
| `audit_resume_session_ended` | Journal 已有 `session_ended`，终结 Session 不可重开 |
| `audit_resume_recovery_required` | 物理尾损，或存在本次无法收敛的未结算 effect（`record_ids` 列出需要人处置的 record：有工具以外的未结算项时列出全部未结算 record，否则只列仍需人裁决的工具调用） |
| `audit_resume_resolution_invalid` | `tool_outcome_resolver` 的裁决不适用于该调用（返回类型不对，或对已有结果的调用 `provide`；`record_ids` 为该 record） |
| `audit_resume_thread_mismatch` | Journal root thread 与 `resume_thread_id` 不符 |
| `audit_resume_projection_conflict` | 投影 Session identity 不变量违约 |
| `audit_resume_open_failed` | 其他 core 错误（含恢复 batch 追加失败），或 core 返回值 / 恢复后重读不满足 trust boundary |
| `audit_resume_session_active` | 同一 Session 已 live 且绑定另一 thread |

### 13.1 结果未知的工具调用收敛（ADR 0070）

root thread 上的悬空 `tool_intent_committed` 与 durable 为 `unknown` 的 `tool_outcome_committed` 按
[tool-crash-reconciliation § strict audit 路径](tool-crash-reconciliation.md) 分流：有 `ToolSpec.reconcile` →
回查；悬空调用的落账与当前 `effect_kind` 都属 pure / idempotent → 可安全重发；其余经
`AuditConfig.tool_outcome_resolver` 征求人裁决（`AuditToolOutcomeResolution(action="provide"|"abort", operator_id)`，
以 `operator` actor 落账）；都给不出结论即拒绝。

- 结论写成新的 `tool_recovery_committed`（`ToolRecoveryCommittedV1`）+ 需要时补写的 `function_call_output` 会话项，
  全部调用都有结论才作为**一个** batch 以接管 lease 原子追加；不改写任何历史记录。
- 回查与 resolver 只在持写者锁之后调用；只读预检把「无回查、不可安全重发、无 resolver」的调用直接拒绝、不写接管记录；
  需回查或需征求 resolver 才能判定的调用若最终被拒，接管记录已写（epoch 已 +1），与 Engine 构造失败同类。
- 已有 durable 结果的调用只接受「回查确认未执行」或人 `abort`，且不补第二条 output。
- 恢复结论随 `thread_resumed.recovered_tool_calls` 透出（`reconciled` / `safe_to_retry` / `operator_resolved`；
  §13.2 的调用为 `not_dispatched`）。
- core strict verify 只校验结构与 hash chain，新记录类型天然通过；resume 扫描按 DTO 严格校验，形状违约即
  `audit_resume_journal_invalid`。没有该记录的旧 Journal 冷读行为不变。

### 13.2 从未登记意图的工具调用收敛（ADR 0075）

模型回复与工具意图是两个相继的 batch。进程死在两者之间时，Journal 里没有任何未结算 effect，root thread 末尾却
留着没有结果的 `function_call`。意图先于任何派发落账，这些调用确定没有执行：恢复时为每个调用追加
`tool_call_undispatched`（`ToolCallUndispatchedV1`）+ 补写的 `function_call_output` 会话项（`not_executed:` 开头、
`is_error=true`），与 §13.1 的结论同属一个 batch。数据契约与识别规则见
[tool-crash-reconciliation § 从未登记意图的调用](tool-crash-reconciliation.md)。

- 不回查、不看副作用声明、不征求 resolver；恢复不执行工具。
- call id 无法构成 operation identity 的调用交人：预检即拒，`record_ids` 为该 `function_call` record。
- 处置结论以 `not_dispatched` 随 `thread_resumed.recovered_tool_calls` 透出。

### 13.3 被中断的同步 call_skill 派发收敛（ADR 0076）

子 skill 执行途中崩溃留下「父 thread 悬空 `call_skill` 意图 → 未结算 `skill_selected` → 子 thread 待收敛调用」
的链。恢复沿派发树自底向上收敛：子 thread 的调用按 §13.1 / §13.2 结算 → 派发落
`skill_dispatch_finished(cancelled, process_recovery)` + 子 thread `thread_terminal` → 父调用落
`tool_recovery_committed(basis=dispatch)` 并补一条列出子调用处置的结果。数据契约与谱系形态表见
[tool-crash-reconciliation § 沿 skill 派发树收敛](tool-crash-reconciliation.md)。

- 可收敛 thread = root + 被中断派发的子 thread（逐层传递）；不属于任何被中断派发的子 thread 上的未结算项仍 fail closed。
- 派发从未启动（无 `skill_selected`，或有 selected 无 started）判未执行；子 skill 已 durable 结束而父调用结果未落账
  时用已落账的终态结算父调用。
- 全有或全无覆盖整棵树：任一层有调用仍需人裁决，整批不写。
- 被中断的执行不写 `skill_outcome`；恢复不续跑子 skill。
- 未结算的 LLM attempt（请求已落账、无 checkpoint）仍不在收敛范围，出现在任一 thread 上都 fail closed。

## 14. 验收门槛

- records/core/projector/coordinator/Engine/LLM/Tool/Skill focused tests 全绿；
- cancel 四窗口、并行部分完成、projection stale、Session 隔离和 lifecycle race 全覆盖；
- legacy mode 回归不变；
- resume 端到端：崩溃（writer 消失、无 `session_ended`）后新 pool 接管，history 与投影一致、续跑新 turn、
  verify 通过且含 epoch 2 接管记录；正常关停后拒绝；无回查、非幂等且无 resolver 的未结算 tool intent 拒绝并列出
  record 且不写接管；writer 存活 Busy；resume 后 Engine 失败只释放 lease、可再次接管；
- 工具收敛（§13.1）：回查完成 / 未执行 / 查不清 / 抛错、幂等可安全重发、人 provide / abort（operator actor）、
  resolver 返回 None / 抛错 / 裁决不适用、durable unknown outcome 只落结论、二次崩溃冷读恢复记录视为已结算；
- 从未登记意图的调用（§13.2）：单个 / 并行批次全部收敛且顺序一致、结论 durable 且 verify 通过、二次崩溃不重复
  收敛、call id 无法构成 identity 时预检即拒且不写接管、正常跑完的调用不被误判；
- 派发树收敛（§13.3）：子 thread 调用回查完成 / 未执行 / 人裁决、无人可问时预检即拒且只列子 thread 调用、selected
  未 started、意图未 selected、子 skill 已结束父结果缺失、两层嵌套逐层收敛、二次崩溃不重复收敛、子 thread 投影补齐、
  不属于被中断派发的子 thread 仍 fail closed；
- full Ruff changed-files、full mypy、full pytest、Sim selfcheck、OpenSpec strict validation 全绿；
- living architecture 与 `docs/capability-matrix.md` 同步；
- 获得明确外部 provider 授权后运行真实 LLM capability matrix，并在最终代码 head 刷新两份 ledger。

最后一项未完成时，不得标 OpenSpec 完成、archive 或 merge。

## 15. 上下文压缩与预算提示（ADR 0094）

### 15.1 允许的压缩

压缩策略以类属性 `audit_support` 声明对审计模式的支持；协调器里任一策略没有声明或声明了别的值，
构造期以 `audit_compressor_unsupported` 拒绝。

| `audit_support` | 含义 | 内置策略 |
| --- | --- | --- |
| `fold` | 折叠式、不调用模型：一段 history 被一条 `compacted` 条目替代 | `SlidingWindowStrategy` |
| `fold_model` | 折叠式、调用模型，且只经 `CompressionContext.model_session` 调用 | `HandoffCompactionStrategy` |
| （无） | 原地改写条目、落盘、后台执行 | `SurgicalTrimStrategy`、`MultimodalEvictionStrategy`、`OffloadStrategy`、`BackgroundCompactionStrategy` |

原地改写的策略被拒的原因：改写后的条目不进 Journal，hot history 会与 Journal 不一致。

压缩只在采样之间发生（`pre_turn` / `mid_turn`）。手动压缩（`CompactNow`）不在允许的 Op 之列；
溢出自愈不启用——它要对同一次 LLM 调用重采样，而审计下每个 LLM operation 只发生一次，
上下文溢出仍使 turn 失败。

### 15.2 记录

`ContextCompactedV1`（record type `context_compacted`）：

| 字段 | 含义 |
| --- | --- |
| `phase` | `pre_turn` / `mid_turn` |
| `strategy` | 执行压缩的策略名 |
| `ordinal` | 本 turn 内第几次压缩（从 0 起） |
| `tokens_before` / `tokens_after` | 压缩前后的上下文占用估算 |
| `replaced_range` | 被折叠的区间 `[start, end)`，坐标系是压缩前的逻辑 history |
| `removed_item_count` | 被折叠的条目数，等于区间长度 |
| `summary_item_id` | 同批提交的 `compacted` 对话项的 id |
| `cache_invalidated` / `anchor_preserved_until` | 对缓存前缀的影响（R2） |
| `quality_warnings` / `detail` | 摘要质量警告、策略自报的计数 |
| `llm_request_record_ids` | 为这次压缩发起的 LLM 调用的 request record id，按发起顺序 |

`BudgetHintInjectedV1`（record type `budget_hint_injected`）：`used_tokens`、`context_window`、`soft_limit`、
`hard_limit`、`item_id`。

对话项新增两种 kind：`compacted`（`summary`、`replaced_range`、`cache_invalidated`）与
`system_injection`（`text`、`source`，`source` 只允许 `budget_hint`——截断类 marker 会改写 history，不落账）。
摘要条目的 metadata 带压缩后的占用估算（压缩增量基线）与继承的来源标记。

### 15.3 顺序

```text
压缩策略经 model_session 发起的每次调用：
    llm_request_committed → dispatch → llm_response_checkpoint
策略返回后：
    每次已收敛的调用各补一条 llm_response_committed（无对话项；调用失败也补）
压缩成功：
    context_compacted + conversation_item(compacted)   一个 batch
    ack → 投影 → 改 hot history、缓存锚点、校准锚点
```

- 压缩发起的 LLM 调用各是独立的 logical LLM operation，`iteration` 从 1 000 000 起编号。
- 压缩未成功（策略返回失败、摘要质量不过、产物引入悬空调用）时不写 `context_compacted`，history 不变；
  已发起的 LLM 调用照样落账。
- 策略声明了折叠式却没有给出 `compacted` 摘要条目：不应用，`compaction_completed.reason` 为
  `audit_requires_summary_item`。
- 任一 Journal 写入不确定按 §4 冻结 Session。

### 15.4 hot history 与恢复

- runner 记下本轮被折叠的条目 id；turn 结束回写 engine history 时这些条目不并回，
  其余条目仍按完整身份核对，不一致以 `audit_history_item_conflict` 冻结。
- 投影按 Journal seq 追加对话项，`compacted` 条目排在它替代的条目之后；读取方按标记重放得到逻辑 history。
- resume 重建 root history 时同样按标记重放，得到的与崩溃前的 hot history 一致。

### 15.5 验收

折叠式策略放行、改写式与未声明的策略拒绝；压缩结论与摘要条目同批且相邻；hot history、投影、
Journal 重放三者一致；strict verify 通过；resume 后 history 与崩溃前一致且续跑看到折叠后的上下文；
不同 turn 的压缩各有独立 identity；摘要调用的 request / checkpoint / response 齐全且先于压缩结论；
摘要失败时调用照样落账而 history 不变；预算提示与它的 record 同批；记录与对话项的形状校验；
被折叠的条目不并回、期间应用的输入保留、冲突仍被检出。

## 16. hook 与权限裁决（ADR 0096）

### 16.1 允许的配置

| 参数 | 允许 | 拒绝 |
| --- | --- | --- |
| `hooks` | `HookRunner`（含子类） | 其他对象：`audit_hooks_unsupported` |
| `permission_policy` | `PermissionPolicy`（prompter 当场作答或以挂起方式审批，后者见 §17） | 其他对象：`audit_permission_unsupported` |

业务注入的 handler 与策略原样运行；内核在 runner 构造时按 turn 绑定一层，裁决返回给调用方之前先落账。
子 skill 的 runner 重新绑定到自己的 thread；子 turn 的权限策略照常按 `subagent_approval_mode` 包装。

### 16.2 记录

`HookEvaluatedV1`（record type `hook_evaluated`）——每个 handler 的每次裁决一条：

| 字段 | 含义 |
| --- | --- |
| `hook_kind` | hook 类型 |
| `handler_index` | 该类型下第几个 handler（注册顺序，从 0 起） |
| `allow` / `reason` | 是否放行、拒绝理由 |
| `subject` | 裁决针对的对象：hook 输入里的 `call_id` / `tool_name` / `target_skill_id` / `caller_skill_id` / `script_name` / `phase` / `iteration` / `end_reason` / `depth` 中存在的那些 |
| `overrides` | handler 要求的改写：`args_override` / `output_override` / `text_override`。值无法规范化时记 `{"not_canonical": true}` |
| `metadata_keys` | handler 给出的其余 metadata 的键名（只记键名） |
| `error_class` | handler 抛出异常时的异常类名；此时 `allow=false`、`reason="hook_error"`，异常原文不落账 |

`PermissionDecidedV1`（record type `permission_decided`）——每次 `check` 一条：

| 字段 | 含义 |
| --- | --- |
| `scope` / `target` | 权限范围与对象 |
| `request_reason` | 请求方（模型）自陈的理由 |
| `call_id` / `call_chain` | 所属工具调用、skill 调用栈 |
| `request_metadata` | 请求携带的上下文（工具参数、业务透传内容） |
| `granted` / `mode` / `decision_reason` | 裁决、裁决方式、依据（命中的规则、授权 id、审批人的说明） |
| `remember_until` | 审批人声明的记忆范围 |
| `minted_grant` | 随裁决签发的可复用授权的匹配条件；没有签发为空 |

### 16.3 顺序与编号

- 裁决记录 ack 之后调用方才拿到裁决：`pre_tool_use` 的记录先于工具执行，`post_tool_use` 的记录先于
  `tool_outcome_committed`，权限裁决先于被批准的动作。
- 同一 turn 内 hook 记录连续编号，engine 层的 `pre_turn` / `post_turn` 与 runner 层的 hook 共用一个计数；
  权限裁决另起一个计数。编号按 thread 分别计，子 skill 的裁决记在子 thread 名下。
- 串行运行的 handler 里，拒绝之后的 handler 不运行，也就没有记录。
- 请求携带的上下文无法规范化时，这次请求被拒绝（`decision_reason =
  audit_permission_request_not_canonical`），业务的策略不被调用，记录里 `request_metadata` 为空。
- 裁决记录不是 effect：恢复时不参与未结算判定。
- 任一裁决记录写入不确定按 §4 冻结 Session；冻结不会被当成 handler 自己的错误吞掉。

### 16.4 边界

- `post_turn` 在 `turn_completed` 事件之后触发；Session 紧接着被关闭时，它可能来不及运行或落账
  （与非审计模式下该 hook 的时序相同）。
- handler 给出的自由 metadata 只记键名；需要留存其值的业务自行记录。
- 以挂起方式进行的审批：裁决不在当场产生，挂起时不写 `permission_decided`；获批的调用在续跑里重跑时
  写一条 `decision_reason = resume_preapproved` 的裁决，被拒的调用由 `suspension_resolved` 记录处置（§17）。

### 16.5 验收

静态门放行内核的 hook 运行器与权限策略、拒绝其他对象；参数改写先于工具执行落账；
拒绝落账且工具不执行；多个 handler 逐个落账、拒绝之后的不运行；输出改写落账且到达模型；
handler 出错落账且不含原文；`pre_turn` 拒绝落账且不发起 LLM 调用；turn 内各层 hook 连续编号、
下一 turn 重新编号；子 skill 的裁决记在子 thread；strict verify 通过且恢复不受裁决记录影响；
规则裁决、人工审批与签发的授权、命中已签发的授权、skill 派发审批、子 skill 按 subagent 模式裁决、
无法规范化的请求被拒、策略的其余入口仍可用。

## 17. 挂起与恢复（ADR 0097）

turn 可以在工具调用处停下等人作答，之后由 `Resume` 带着答复继续。挂起、答复、处置都是 Journal 里的记录；
Session 可以在等待期间被释放，由另一个进程接管后继续。

### 17.1 允许的挂起

| 等待原因 | 发起方 | 条件 |
| --- | --- | --- |
| `permission` | 权限策略（`SuspendingPrompter`） | 任何工具调用都可能遇到 |
| `form` / `data` | 工具自己（抛 `SuspendSignal`） | 工具声明 `can_suspend=True`（内置的 `request_user_input` 已声明） |

待答请求必须指向发起它的那次调用（`related_call_id` 等于调用 id），且不带到期时间，并且调用发生在
root thread 上。其余情形是能力违约，处置同 §9：未声明的工具自行挂起、子 thread 上的调用停下等人
（同步 `call_skill` 的子 skill、分离式派发的子 skill）、失败处置、资源护栏、带 `ttl_seconds` 的挂起。

### 17.2 记录

| record type | 何时 | 要点 |
| --- | --- | --- |
| `turn_suspended` | turn 停下 | `suspension_id`、`awaited`（每项：`request_id` / `reason` / `call_id` / `intent_record_id`）；同批提交 `suspension` 对话项 |
| `resume_accepted` | `Resume` 提交时，入队之前 | 答复原文 `resolutions`、针对的 `suspension_id` 与 `turn_suspended` record、续跑 turn 的 `turn_index` |
| `suspension_resolved` | 处置时 | 各请求的处置 `approved` / `denied` / `answered` 与已结算调用的 outcome record；同批提交结清标记（`system_injection`，`source = suspend_resolved`） |
| `resume_applied` | 结清之后、续跑之前 | `result_status`：`resumed` / `aborted` / `rejected`；被拒时带 `rejection_reason` |
| `session_detached` | Session 在等待期间被释放 | 仍在等待的 `suspension_ids`；不写 `thread_terminal` / `session_ended` |

`turn_suspended` 与 `suspension_resolved` 的 operation 是 `{turn_id}:suspension:{n}`；`resume_accepted` 与
`resume_applied` 的 operation 是这次 `Resume` 的 submission id。

### 17.3 顺序

```text
tool_intent_committed（整批）
tool_outcome_committed + function_call_output      同批里已有结果的调用照常结算
turn_suspended + suspension 对话项                  等人的调用保持未结算
    ── turn 以 suspended 结束；Session 可以被释放（session_detached）、被接管 ──
resume_accepted                                     先于任何处置
tool_outcome_committed + function_call_output      被拒 / 直接作答的调用各自结算
suspension_resolved + 结清标记
resume_applied
    ── 续跑的 turn ──
permission_decided（resume_preapproved）            获批的调用重跑前的裁决
tool_outcome_committed + function_call_output      获批的调用结算
llm_request_committed …                             续跑照常采样
```

- 等过人的调用的结果记在它原来的 operation 下，`intent_record_id` 指向挂起前落账的意图；不重写意图。
- 被拒的调用结果状态为 `rejected`，直接作答的调用状态为 `success`，答复的 JSON 即调用结果。
- 续跑的 turn 使用新的 `turn_index` 与这次 `Resume` 的 submission id；它发出终态事件之后不再写任何记录。
- 续跑里重跑的调用可以再次停下等人（下一道审批），此时落一条新的 `turn_suspended`。
- 续跑的 turn 登记为可取消的目标：`CancelTurn` 指向这次 `Resume` 的 submission id 即可取消它。

### 17.4 `Resume` 的准入

审计模式下一次 `Resume` 必须答复该挂起的每一个请求。不适用的 `Resume` 在入队之前被拒，
落一条 `submission_rejected`（`op_kind = resume`，不含答复原文），`submit()` 抛 `AuditedResumeRejectedError`：

| `reason` | 含义 |
| --- | --- |
| `resume_thread_not_root` | `thread_id` 不是这个 Session 的 root thread |
| `no_active_suspension` | 没有在等待的挂起 |
| `resume_must_resolve_every_request` | 答复的请求集合与挂起的请求集合不一致（少答、多答、答了不存在的请求） |
| `resume_resolutions_not_canonical` | 答复内容无法规范化 |
| `suspension_not_journaled` | 挂起不在 Journal 里 |
| 答复形状错误的既有错误码（如 `invalid_payload_shape`） | 同 [suspend-resume](suspend-resume.md) 的边界校验 |

被拒不改变任何状态：挂起仍在等待，可以再次提交。

### 17.5 释放、接管与中断

- Session 在等待期间被释放：写 `session_detached`，释放写者；Session 没有终结，之后可以按 §13 接管。
  接管后再次释放仍是 `session_detached`；挂起结清之后的释放照常写 `session_ended`。
- 进程在等待期间崩溃：接管时在等人作答的调用不算未结算 effect，不触发 §13.1 的收敛。
- 处置到一半中断（`resume_accepted` 已落账、`suspension_resolved` 没有）：挂起仍在等待，重新提交同样的
  答复即可；已经结算过的调用沿用已有的结果，不重复结算。
- 结清之后、获批的调用重跑结束之前中断：该调用此时是普通的未结算 effect，按 §13.1 收敛。

### 17.6 边界

- `CancelTurn` 指向挂起中的 turn 不核销挂起（没有在跑的目标可取消）。要放弃一次挂起，提交 `Resume`
  拒绝其中的请求。
- 答复原文进 Journal（`resume_accepted.resolutions`）。答复里的敏感内容由业务在提交前处理。
- 子 skill 内的挂起、带到期时间的挂起、失败处置挂起不在范围内。

### 17.7 验收

静态门放行挂起式审批与声明可挂起的工具；挂起落账且调用保持未结算、恢复扫描不把它算作未结算；
同批里已有结果的调用照常结算；未声明的工具自行挂起冻结 Session；获批的调用以原 identity 重跑并结算；
被拒的调用不执行且先于结清结算；答复成为发问工具的结果；两个等人的调用一次答复；
五类不适用的 `Resume` 被拒且状态不变；没有挂起时的 `Resume` 被拒；等待期间释放写 `session_detached`、
接管后继续、最终正常终结；等待期间崩溃后接管；处置到一半中断后重新提交不重复结算；
`CancelTurn` 不核销挂起；strict verify 通过。

## 18. 分离式派发（ADR 0098）

`spawn_skill` 把子 skill 放到独立的子 thread 上后台运行，发起方不等它结束。子 thread 上的 LLM 调用、
工具调用、hook 与权限裁决都按本契约落账，记在子 thread 名下。

### 18.1 允许的操作

| 操作 | 入口 | 落账 |
| --- | --- | --- |
| 发起 | `spawn_skill` 工具、`engine.spawn_skill()` | `spawn_started` 批次 |
| 终止 | `kill_skill` 工具、`engine.kill_spawn()` | 子 turn 以 cancelled 结束后落 `spawn_settled` |
| 查询 | `join_skill` 工具、`engine.spawn_status()` | 只读；经工具调用时结果在该调用的 outcome 里 |
| 等待 | `wait_peer` / `wait_any` 工具 | 同上 |

### 18.2 记录

两条记录共用一个 operation：句柄 id。

`SpawnStartedV1`（record type `spawn_started`）：

| 字段 | 含义 |
| --- | --- |
| `handle_id` | 句柄 id |
| `skill_id` | 被派发的 skill |
| `child_thread_id` | 子 thread；由 Session id 与句柄 id 确定性派生 |
| `parent_thread_id` | Session 的 root thread |
| `reason` | 发起方自陈的理由 |
| `arguments` | 交给子 skill 的种子输入 |
| `definition_hash` / `body_hash` | 派发时该 skill 定义与正文的摘要 |
| `deadline_seconds` | 墙钟上限；没有为空 |

`SpawnSettledV1`（record type `spawn_settled`）：

| 字段 | 含义 |
| --- | --- |
| `handle_id` / `child_thread_id` | 句柄与子 thread |
| `started_record_id` | 对应的 `spawn_started` record |
| `status` | `done` / `error` / `cancelled` |
| `end_reason` | 子 turn 的结束方式；接管时补写的为 `process_recovery` |
| `result` | `done` 时是子 skill 的最终文本，`error` 时是错误说明，`cancelled` 时为空 |

### 18.3 顺序

```text
tool_intent_committed（spawn_skill）                经工具发起时
spawn_started + thread_created + thread_bound + conversation_item（种子，子 thread）
tool_outcome_committed（spawn_skill）               结果含 handle_id 与 child_thread_id
    ── 子 thread：llm_request_committed … tool_intent_committed … ──
spawn_settled + thread_terminal（子 thread）
```

- 发起批次 ack 之后子 skill 才开始运行。
- 种子输入无法规范化时拒绝发起（`SpawnRejectedError`，分类 `arguments_not_canonical`），什么都不写，
  不占配额。
- 子 thread 的 turn 从 0 编号，submission id 取子 thread id。
- 终态的顺序是「记录 → 句柄状态 → 事件」：查询与等待看到终态时，终态记录已经写下。
- Session 已冻结或已封口时终态不写；这次派发在接管时按 18.5 处置。

### 18.4 对话项

写进 thread 的对话项只有子 thread 的种子消息。非审计模式下落在父 thread 的 `spawn` 锚、落在子 thread 的
`spawn_settled` 锚在审计模式下都不写：句柄表是运行态，它的事实在记录里；子 skill 结束的时刻父 thread
上可能正有 turn 在写，往父 thread 追加条目会让两个写者交错。

### 18.5 释放与接管

- 释放 Session 时仍在运行的派发被取消，各自落 `spawn_settled(cancelled)`，之后才是 Session 的
  terminal batch。
- 接管时句柄表由 `spawn_started` / `spawn_settled` 记录重建，已结束的派发可以继续查询结果。
- 接管时没有终态的派发（进程死的时候还在运行）不续跑：子 thread 上的工具调用按 §13.1–13.3 结算，
  然后落 `spawn_settled(cancelled, process_recovery)` 与子 thread 的 `thread_terminal`，同属一个恢复批次。
  子 thread 上仍有调用需要人裁决时整批不写，resume 被拒。

### 18.6 边界

- 子 thread 上的调用不能停下等人作答（§17.1）：那次调用记 error 终态，Session 冻结。
- 审计模式下一次工具调用必须在收敛期限内给出结果（§9）。`wait_peer` / `wait_any` 的等待时长因此
  不超过收敛期限的一半；到点返回 `timeout`，可以再等一次。
- 发起它的工具调用与这次派发经 outcome 里的 `handle_id` 关联，记录之间没有直接的引用字段。
- 被唤醒重跑、子 thread 的 rewind 不在范围内。

### 18.7 验收

静态门放行五个工具、拒绝后台 shell 任务；发起批次先于发起它的工具调用结算、子 skill 的 LLM
调用记在子 thread 名下且在发起批次之后；终态记录与子 thread 的 `thread_terminal` 相邻；任何 thread
上都没有锚点条目；查询与等待工具读回结果；两个派发并行；kill 落 `cancelled`；无法规范化的种子输入
被拒且不占配额；子 thread 停下等人冻结 Session；等待时长有上限；释放时取消仍在运行的派发并正常
终结；接管后由 Journal 重建句柄表；崩溃时仍在运行的派发在接管时落终态；strict verify 通过。

## 19. join-barrier（ADR 0099）

barrier 等一批派发全部结束，然后在一个新 thread 上起聚合 turn。聚合 thread 上的 LLM 调用、工具调用、
hook 与权限裁决按本契约落账，记在聚合 thread 名下。

### 19.1 记录

三条记录共用一个 operation：barrier id。

| record type | 何时 | 要点 |
| --- | --- | --- |
| `barrier_registered` | 登记 | `handle_ids`（按登记顺序，不得重复）、`then_skill_id`、`then_args_template`（没有为空） |
| `barrier_fired` | 成员全部结束之后 | `registered_record_id`、`then_thread_id`、`members`（各成员点火时的终态）、`arguments`（交给聚合 skill 的输入）、聚合 skill 定义与正文的摘要 |
| `barrier_settled` | 聚合 turn 结束 | `fired_record_id`、`status`（`done` / `error` / `cancelled`）、`end_reason`、`result` |

`then_thread_id` 由 Session id 与 barrier id 确定性派生。

### 19.2 顺序

```text
tool_intent_committed（await_skills）               经工具登记时
barrier_registered
tool_outcome_committed（await_skills）              结果含 barrier_id
    ── 成员各自 spawn_settled ──
barrier_fired + thread_created + thread_bound + conversation_item（种子，聚合 thread）
    ── 聚合 thread：llm_request_committed … ──
barrier_settled + thread_terminal（聚合 thread）
```

- 登记记录 ack 之后 barrier 才进入 barrier 表；自定义输入无法规范化时拒绝登记
  （`ValueError: then_args_not_canonical`），什么都不写。
- 点火批次 ack 之后聚合 turn 才开始运行。登记时成员已经全部结束的，登记之后立即点火。
- 每个 barrier 至多点火一次。
- 不往任何 thread 写 `join_barrier` / `join_barrier_fired` 锚点条目（理由同 §18.4）。

### 19.3 释放与接管

- 释放 Session 时仍在运行的聚合 turn 被取消，落 `barrier_settled(cancelled)`，之后才是 Session 的
  terminal batch。
- 接管时 barrier 表由 `barrier_registered` 重建，有 `barrier_fired` 的记为已点火。
- 登记了、没点火的 barrier：接管把没有终态的成员落 `cancelled`（§18.5）之后，barrier 随即点火，
  聚合输入里这些成员的终态是 `cancelled`。
- 已点火、聚合 turn 没有终态的 barrier：聚合 thread 上的工具调用按 §13.1–13.3 结算，然后落
  `barrier_settled(cancelled, process_recovery)` 与聚合 thread 的 `thread_terminal`。不再点火。

### 19.4 边界

- 聚合 thread 上的调用不能停下等人作答（§17.1）。
- 聚合 turn 的结果只在 `barrier_settled` 里，不登记为句柄，不能用 `join_skill` 查询。
- 仅「全部结束」触发；任一结束触发、超时触发不在范围内。

### 19.5 验收

静态门放行 `await_skills`；成员没有全部结束时不点火；点火在成员终态之后、点火批次带着聚合 thread 的
创建与种子、聚合 skill 的 LLM 调用记在聚合 thread 名下；默认输入是各成员的终态与结果；
经工具登记且成员已结束时立即点火、自定义输入原样交给聚合 skill；无法规范化的自定义输入被拒；
任何 thread 上都没有锚点条目；释放时取消仍在运行的聚合 turn；登记了没点火的 barrier 在接管后点火；
被中断的聚合 turn 在接管时落终态且不再点火；strict verify 通过。

## 20. peer 消息（ADR 0100）

同一个 Session 里的 agent 之间点对点发消息：子 thread 发给 root、root 发给子 thread、子 thread 之间互发。

### 20.1 两个事实，两个写者

| 事实 | 记录 | 谁写 | 何时 |
| --- | --- | --- | --- |
| 发出 | `peer_message_sent` | 发送方 | 发送方那次工具调用结算之前 |
| 进入对话 | `conversation_item`（`user_message`，`payload.source = "peer"`） | 目标 thread 的写者 | 目标的 runner 到达迭代边界时 |

消息进入对话由目标 thread 的写者完成。迭代边界是调用与结果都已配对的位置：消息不会落在一次工具调用
和它的结果之间，接管时按 Journal 重建出的 history 与运行时一致。

### 20.2 记录

`PeerMessageSentV1`（record type `peer_message_sent`，operation 是消息 id，`thread_id` 是发送方 thread）：

| 字段 | 含义 |
| --- | --- |
| `message_id` | 消息 id，也是它进入对话后的对话项 id |
| `from_thread_id` / `to_thread_id` | 发送方与目标 thread |
| `mode` | 发送方要求的投递方式：`queue_only` / `trigger_turn` |
| `mode_downgraded` | 要求唤醒、实际只排队 |
| `address` | 发送方写的拓扑地址（`sibling:<skill>` / `child:<skill>`）；直接寻址时为空 |
| `item` | 消息本身：它将以什么样子进入目标 thread 的对话 |

进入对话的对话项 `source_record_id` 指向这条发出记录，`metadata.peer_record_id` 同值。

### 20.3 投递

| 目标 | 处置 | 返回的 `delivered_via` |
| --- | --- | --- |
| root，有 turn 在运行 | 进 root 的收件队列，运行中的 turn 在下一个迭代边界收下 | `pending_input` |
| root，空闲 | 进 root 的收件队列，下一个 root turn 开始时收下 | `inbox` |
| 正在运行的子 thread | 进它的 runner 的收件队列，下一个迭代边界收下 | `pending_input` |
| 已经结束的子 thread | 拒绝：`ValueError: peer_target_not_running`，什么都不写 | — |

- root 的收件队列跟着 Session 走，不跟着某个 turn。
- `trigger_turn` 打正在运行的目标降级为排队（`mode_downgraded = true`）；打 root 被拒
  （`trigger_turn_root_forbidden`，与非审计模式相同）。审计模式下没有唤醒。
- turn 收尾之后、子 thread 落终态之前到达的消息照样写进对话；模型没有看到，但消息在 thread 里。
- 消息内容无法规范化时拒绝发出，什么都不写。

### 20.4 释放与接管

- Session 终结时还在 root 收件队列里的消息没有进入对话；Journal 里有它们的发出记录。
- 接管时，发给 root、还没进入对话的消息从 Journal 回到收件队列，下一个 root turn 收下。
- 发给子 thread、还没进入对话的消息不再投递：那个子 thread 在接管时被落为 `cancelled`（§18.5）。

### 20.5 边界

- `SendToPeer` Op 仍在能力面之外（动态门拒绝）：它是业务从外部注入的消息，不是 agent 之间的消息。
- 已经结束的子 thread 不接受消息，也不会被唤醒重跑。
- 消息全文进 Journal。

### 20.6 验收

静态门放行 `send_message`；发出先于发送方那次工具调用的结算；进入对话在目标的工具调用结算之后、
下一次采样之前，回指发出记录；目标的模型在下一次采样看到消息；hot history、投影与 Journal 顺序一致；
root 发给正在运行的子 thread、`trigger_turn` 降级；root 空闲时收到的消息等到下一个 root turn；
已经结束的子 thread 拒收且什么都不写；接管后未进入对话的消息回到收件队列且只进入对话一次；
strict verify 通过。
