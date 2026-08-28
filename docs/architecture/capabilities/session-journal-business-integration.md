# SessionJournal 普通业务主链接入能力契约

> 状态：Experimental。关联 ADR 0025。依赖 `session-journal-core` Phase 1。

## 1. 范围

本能力只覆盖显式启用审计的新 Session：

```text
UserMessage → LLM → 基础 Tool / 同步 call_skill → assistant
```

SessionJournal 是执行事实和对话项的唯一可靠事实源。hot history、MessageStore 和 EventMsg 都是
Journal durable ack 之后的内存态或可重建投影，不得领先 Journal，也不得被声明为第二事实源。

未启用 audit 的 EnginePool/AgentEngine/MessageStore 行为保持不变。本阶段不支持已有 Journal、resume、
HITL/审批、suspend、compaction/rewind、memory、instruction 更新、hooks、orchestration、detached spawn、
barrier、peer、跨进程 recovery、Timeline/export 通用 redaction、加密、WORM 或外置 blob。LLM request intent
的写入前 data minimization 是本契约 §8 的强制安全边界，不属于上述未实现的投影视图 redaction。

## 2. 唯一事实源与提交顺序

进入对话历史的事实必须表示为 `conversation_item`，并与领域 outcome 在同一个 batch 提交：

```text
submission_accepted + conversation_item(user_message) + submission_applied
llm_response_committed + conversation_item(reasoning/assistant/function_call...)
tool_outcome_committed + conversation_item(function_call_output)
skill_dispatch_finished + thread_terminal + conversation_item(skill_outcome)
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

audit ToolSpec 必须声明 `effect_kind`、`reconciliation`、`can_suspend=False`。hooks、permission、HITL 和
可 suspend Tool 在 effect 前拒绝。

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

## 12. Capability gate

| 维度 | 允许 | 拒绝 |
| --- | --- | --- |
| Op | UserMessage、CancelTurn、Shutdown | 其他 Op |
| Session | 新建 | resume、已有 Journal |
| Store | 默认 JSONL 可重建投影 | custom store/directory、IndexHook |
| Hook/approval | 无 | hooks、permission、HITL |
| Context | 无 compressor/memory/instruction update | compaction、rewind、memory、instruction |
| Skill | atomic/composite、同步 call_skill | orchestration、suspension |
| Spawn/peer | 无 | detached spawn、barrier、peer |
| LLM | attempt-observable | opaque attempt/retry |
| Tool | audit metadata 完整且 non-suspending | metadata 缺失或可 suspend |

静态配置在 EnginePool 构造期验证，Op 在 submission gateway 验证，动态 effect 在 TurnRunner gate 再验证。
拒绝必须发生在 effect 前。

## 13. 验收门槛

- records/core/projector/coordinator/Engine/LLM/Tool/Skill focused tests 全绿；
- cancel 四窗口、并行部分完成、projection stale、Session 隔离和 lifecycle race 全覆盖；
- legacy mode 回归不变；
- full Ruff changed-files、full mypy、full pytest、Sim selfcheck、OpenSpec strict validation 全绿；
- living architecture 与 `docs/capability-matrix.md` 同步；
- 获得明确外部 provider 授权后运行真实 LLM capability matrix，并在最终代码 head 刷新两份 ledger。

最后一项未完成时，不得标 OpenSpec 完成、archive 或 merge。
