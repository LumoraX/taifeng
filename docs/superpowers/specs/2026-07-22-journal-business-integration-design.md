# SessionJournal 普通业务主链接入设计

## 背景

`taifeng.conversation.journal` 已交付隔离的 Phase 1 durable core，具备 RFC 8785
canonical JSON、SHA-256 hash chain、原子 batch、同进程 live lease、durable ack 与
strict verification。独立正式验收确认该 core 的 68 项测试通过，但
`AgentEngine`、LLM、Tool、Skill、HITL 和审批均没有运行时引用 Journal。

本变更交付第一个业务接入纵向切片：新 Session 的普通
`用户输入 → LLM → Tool/call_skill → outcome → assistant` 主链。它采用 Journal-first、
fail-closed 语义，不把 EventMsg 或 legacy transcript 提升为审计事实源。

## 目标

- 为新 Session 建立唯一的 Session 级 audit coordinator 和连续 Journal 序列。
- 在 LLM、Tool、Skill 外部动作前 durable 记录 intent，动作后 durable 记录 outcome。
- 完整保存用户输入、LLM 请求/最终响应、工具参数/结果和 Skill lineage。
- Journal 失败时只冻结当前 Session、取消在途操作并拒绝新 effect。
- 保持未启用 Journal 的现有 EnginePool/Engine 行为兼容。
- 让 legacy MessageStore 暂时作为 prompt/resume 所需的对话投影，并明确其非审计权威地位。

## 非目标

- 不迁移或打开已有 Session/Journal，不实现 `open_existing`。
- 不实现跨进程 fencing、recovery lease、repair/reconcile/unfreeze。
- 不接入 HITL、审批、detached spawn、compaction、rewind 或 Timeline UI。
- 不把 SessionJournal 公共协议从 `taifeng.conversation` 顶层稳定导出。
- 不静默截断、redact 或降级为 metadata-only；完整 payload 的保留/加密策略另立变更。
- 不立即删除 legacy transcript，也不宣称生产 resume 已迁移到 Journal。

## 方案选择

采用方案 A：Journal-first、fail-closed。

未采用 shadow dual-write：Journal 失败后继续业务执行会让审计与实际 effect 分叉。
未采用立即替换 MessageStore：这会把历史迁移、重放、恢复和 Timeline 一次引入，超出
可独立验证的切片范围。

## 组件边界

### JournalRecordFactory

新增 `conversation/journal/records.py`，只负责把领域输入变成 versioned
`JournalRecord`，不依赖 loop/Engine。所有业务记录包含：

- `schema_version`
- `session_id`
- `thread_id`
- `submission_id`
- `turn_index`
- `call_id` 与可选 `parent_call_id`
- `actor`
- `causation_id`
- record-specific 完整 payload

record id 从稳定 operation identity 与 record kind 派生，保证同一逻辑操作的重试可走
core 幂等检查，而不同 payload 使用相同 id 时明确冲突。

### SessionAuditCoordinator

新增 `loop/audit.py`，作为 loop 与私有 Journal core 之间的适配器。每个活跃 Session
恰有一个 coordinator，持有 lease、expected seq、writer identity 和健康状态。职责：

- 串行提交单条/批次 record，并只从 durable ack 推进 seq。
- 暴露 `record()`、`record_batch()` 和 `ensure_healthy()`。
- 首次 Journal 异常时原子转入 `FROZEN`，保存第一原因，触发 root cancellation。
- 冻结后所有新记录和 effect gate 返回同一个稳定 `SessionAuditFrozenError`。
- 不导入业务模块，不解释 payload 中的宿主字段。

coordinator 是单 writer。子 Skill/子 thread 共享 root coordinator，通过 lineage 字段区分，
不各自创建 SessionJournal writer。

### EnginePool / AgentEngine / TurnRunner

- `EnginePool.create()` 接收显式私有 Journal core/factory 配置；默认 `None` 保持现状。
- `get_or_create()` 在新建空 transcript thread 后创建 Journal Session。Journal durable
  初始化成功前不暴露 Engine，也不接受用户输入或执行 effect。
- Journal 模式暂时拒绝 `resume_thread_id`，因为 Phase 1 没有安全 `open_existing`。
- `AgentEngine` 持有 coordinator，用户输入在 MessageStore append 前写
  `user_input_recorded`。
- `TurnRunner` 共享 coordinator，负责 turn/LLM/tool/skill 的 intent/outcome 记录。
- `EventMsg` 保持实时、可丢失投影；不能用 EventMsg 成功替代 Journal ack。

## 记录契约

首个切片新增以下 kind：

| Kind | Durable 时机 | 主要 payload |
|---|---|---|
| `user_input_recorded` | MessageStore 写入前 | text、attachments、输入来源 |
| `turn_started` | TurnRunner 执行起点 | entry skill、snapshot version、model、budget |
| `llm_request_intent` | provider session 创建前 | 完整 ApiRequest、iteration、结构 cache 信息 |
| `llm_response_outcome` | 流明确完成/失败/取消后 | assistant text、reasoning、tool calls、usage、request id、status/error |
| `tool_call_intent` | dispatch_batch 前 | name、原始/解析参数、parallel_safe、iteration |
| `tool_call_outcome` | batch 返回后、回填 history 前 | output、is_error、duration、reason、suspend 标志 |
| `skill_dispatch_intent` | child thread/TurnRunner 创建前 | target、arguments、call stack、selection provenance |
| `skill_dispatch_outcome` | child 明确完成/失败后 | child thread、end reason、final text/error、usage |
| `thread_created` | child transcript 创建后 | child thread descriptor、parent thread |
| `thread_bound` | child 与 call lineage 绑定时 | child thread、call id、root session |
| `turn_completed` | 正常终态发布前 | end reason、usage、iterations |
| `turn_failed` | 失败终态发布前 | failure class、稳定错误摘要、effect state |
| `turn_cancelled` | 取消终态发布前 | cancellation reason、effect state |

错误 payload 保存稳定分类与安全 `repr`，不保存 Python traceback 对象。完整业务正文仍保存，
不复用 EventMsg 的 preview/redaction 字段。

## 数据流

### Session bootstrap

1. EnginePool 校验 entry skill。
2. MessageStore 创建一个不含业务输入的空 root thread，取得 thread id。
3. 用该 thread descriptor 原子创建 Journal，写既有三条初始化记录。
4. 构造 coordinator 和 Engine；只有 durable ack 后才返回 Engine。
5. Journal 初始化失败时不启动 actor。允许遗留无业务数据的空 transcript 壳，后续清理另行处理。

### 用户输入与 turn

1. AgentEngine 收到 `UserMessage` 后先 `ensure_healthy()`。
2. durable 写 `user_input_recorded`。
3. 写 MessageStore 投影并建立 TurnRunner。
4. TurnRunner durable 写 `turn_started`，再进入采样循环。

### LLM

1. build/preflight 完成并确认确实要发送 provider 请求。
2. durable 写完整 `llm_request_intent`。
3. 创建 provider session 并消费 stream；delta 只投影到 EventMsg。
4. 明确完成、provider error 或 cancellation 后，durable 写一个完整
   `llm_response_outcome`。
5. outcome ack 后才允许 tool dispatch、history 投影或 turn 终态继续。

不为每个 delta 写 Journal；最终 outcome 必须包含已累积的完整响应与结束状态。

### Tool batch

1. 按 provider 请求顺序，把整批 `tool_call_intent` 用一个 Journal batch durable 写入。
2. 运行现有并发 `dispatch_batch`。
3. 按 call index 排序，把整批 `tool_call_outcome` durable 写入。
4. outcome batch ack 后才把 function call/output 投影进 history 并开始下一次 LLM。

这种做法保留并发执行，不要求 Journal 并发写；审计顺序表达逻辑批次顺序，而不是不可复现的
壁钟完成先后。

### call_skill

`call_skill` 同时有 tool 层和 Skill 语义层记录。`run_sub_skill()` 在创建 child thread 前写
`skill_dispatch_intent`；child 创建后批量写 `thread_created/thread_bound`。子 TurnRunner 共享
coordinator。子 Skill 明确完成/失败后写 `skill_dispatch_outcome`，外层再写对应
`tool_call_outcome`。两层通过 causation/call id 关联，不视为重复事实。

## 失败与冻结语义

- intent 写失败：对应 LLM/Tool/Skill effect 不得开始；coordinator 冻结 Session。
- outcome 写失败：effect 可能已经发生，Session 立即冻结，内存状态标记
  `effect_state=unknown`；本切片不自动重试 effect。
- MessageStore 投影失败：Journal 已有事实，但 prompt 投影不可靠，Session 同样冻结。
- 首次冻结触发 root cancellation；在途并发 tool 尽力取消，随后禁止新 effect。
- 冻结状态进入 Engine introspection；EventMsg 可 best-effort 发告警，但不能作为冻结依据。
- 一个 Session 冻结不取消 EnginePool root，也不影响其他 Session。
- Phase 1 无恢复入口；释放/重建进程不能绕过 frozen Session 继续执行。

## 兼容性与配置

- Journal 依赖仅通过构造参数注入，`src/` 不读取环境变量。
- 未注入时所有现有 API、存储、resume 和测试保持原行为。
- 注入即表示 `audit_required`，没有 fail-open 或 shadow 模式开关。
- Journal-enabled 模式只接受新 Session；显式 resume 立即返回稳定错误。
- 不新增 tenant、业务实体或宿主模块 import。

## 测试策略

### 单元测试

- record factory 的稳定 id、schema、lineage、完整 payload 与非法值拒绝。
- coordinator seq 推进、批次、首次原因冻结、重复冻结和 Session 隔离。
- intent 失败时 effect spy 调用次数为 0。
- outcome/投影失败时冻结，后续 effect gate 拒绝。

### SimClient 集成测试

- 普通文本 turn 的精确 Journal 顺序和完整请求/响应。
- tool 成功、tool error、超长 output、并行 batch 的 intent/outcome 顺序。
- `call_skill` 成功、子失败和三层嵌套 lineage。
- provider error、取消、turn failed/cancelled。
- 一个 Session 冻结、另一个 Session 正常完成。
- 每个场景执行 strict verify，并核对 hash chain、payload 与 MessageStore 投影。

### 仓库门槛

- 新模块定向 pytest、focused mypy/ruff。
- 全量 `PYTHONPATH=src uv run mypy src/taifeng`。
- 全量 `PYTHONPATH=src uv run pytest tests/ -q`。
- 更新 `docs/architecture/conversation.md`、`agent-loop.md` 和 capability contract/index。
- 先运行 real-LLM selfcheck，再运行完整 capability matrix，提交更新后的两份 ledger。
- OpenSpec strict validate、`git diff --check` 和独立正式 review。

## 后续变更

1. HITL/审批、suspend/resume、detached spawn、compaction/rewind 的领域记录。
2. 持久化 fencing epoch、`open_existing`、recovery lease、repair/reconcile/unfreeze。
3. MessageStore/Timeline 从 Journal 重建并完成 legacy transcript 迁移。
4. payload 保留、加密、redaction、blob 外置和 WORM 策略。
