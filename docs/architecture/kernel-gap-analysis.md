# 微内核差距分析（kernel-lens）

> 最近更新：2026-09-29（第二轮全面 review：K8–K19；能力侧 review：C1–C16）
> 视角：把 taifeng 当作 **LLM agent 的 OS 微内核 / 调度器**，只问"内核机制长齐没"，
> 不问"某个特性建了没"。上游对标：codex / claw-code / hermes-agent / openclaw / opencode / deepagents。

## 与 `hermes-gap-roadmap.md` 的关系（不冲突，互补）

两份文档是**不同抽象层的两套视角**，不矛盾，但有重叠面，须交叉对账：

| | `hermes-gap-roadmap.md` | 本文（kernel-gap） |
| --- | --- | --- |
| 视角 | **能力 / 特性完善度**（逐 feature） | **内核子系统 / 机制健全度** |
| 回答 | "某个能力建了没？" | "作为内核，机制长齐没？" |
| 组织 | G1a/G3/G4… + P0/P1/P2 + commit 状态 | K1–K7 内核原语 + 机制/策略划分 |
| 用途 | 追踪已建/待建特性 | 判断架构是否缺整块子系统 |

**三处必须对账（否则两文像互相矛盾）：**

1. **MemoryProvider**：roadmap 把它当"P2 / R1 待讨论 / 要不要抄协议"；本文把**同一个东西升格为 [K3] 缺失的内核 swap 子系统**（高优先级）。以本文定性为准——它不是"可选能力"，是"内核还没长出的内存层级"。
2. **G6 的 PTY exec / checkpoint / turn-diff**：roadmap 列为"引擎 P2 缺口（暂缓）"；**本文判定它们是 userspace，根本不算内核差距**。以本文为准（见 §userspace）。
3. **覆盖盲区**：roadmap 没提 K1/K2/K4/K5/K6/K7——"数特性"的视角抓不到"内核机制是否健全"。这些是本文新增的真内核缺口。

> 一句话：**roadmap 管"特性进度"，本文管"内核是否缺子系统"。** 落地某个 K 项时，在 roadmap 里登记对应 feature 进度即可。

## 定位前提：内核的事 vs 宿主/userspace 的事

taifeng 是"可嵌入、一 session 一 engine"的微内核。换框后，一大批之前看似"差距"的东西**根本不归内核管**：

- **userspace（业务/应用层）**：memory/RAG 后端、PTY 持久 exec、checkpoint 回滚（shadow-git）、web_search、内容安全扫描、Mixture-of-Agents、todo/planning、各类具体工具实现。
- **host（调用方）的事**：**跨 engine 的调度 / 优先级 / 抢占 / 公平性**——engine 池的编排归宿主程序，不是 taifeng 内核。所以"没有全局调度器"**不是内核缺口**。

内核只负责**单 engine 内的机制**：进程模型（turn/sub-skill fork）、IPC（双总线）、内存管理（压缩=换页 + swap）、准入与资源强制、中断（cancel）、保护（permission）、syscall（tool dispatch）、自省。

## taifeng 内核已具备（对照确认，非差距）

单 actor run-loop + Submission/EventMsg 双总线 + pub/sub；cancel token 树（父→子级联）；RwLock syscall 层（parallel_safe 读/写两类，与 codex `parallel.rs` 同构）；深度/环/iteration 准入界（`skill/dispatch.py`）；cache-aware 压缩 + 配对完整性回滚（与 claw-code `compact.rs` 同契约）；permission 能力 + subagent 隔离；JSONL journal + resume；failure_class 分类 + recovery 配方 + retry 服务。

## 真·内核差距（机制层，四方收敛）

> 原则：内核提供**机制**，业务/host 提供**策略**。下表"机制=内核"的才纳入。

| # | 内核缺口 | 机制 vs 策略 | 对标证据 | R 线 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| **K1** ✅ | **广度准入控制**（fork-bomb：深度有界、广度无界）。**已落地** `838265c`：`loop/spawn.py::SpawnSlotRegistry`（RAII reserve，max_concurrent 广度 + max_total 兜底，单调）；`run_sub_skill` 超限→`SkillSpawnRejected`+ToolResult.error 不派发；engine/pool 注入 `max_concurrent_spawns`/`max_total_spawns`（默认 16/1000，开箱即防护） | 机制=内核（配额计数器+RAII）；策略=上限值 | codex `agent/registry.rs::reserve_spawn_slot` | R4 | ✅ 完成 |
| **K2** ✅ | **资源上限"告警"→"强制"**（OOM-killer）。**已落地** `dc633ff`：TurnRunner `max_session_tokens`+`_session_limit_exceeded`，超限且有后续 tool call→`ResourceLimitExceeded(turn_aborted)`+停采样；engine 跨 turn 累计 + pre-turn 守卫（触顶拒新 turn，`turn_refused`）；pool 注入 `max_session_tokens`（默认 None=不强制） | 机制=内核（强制点+abort）；策略=天花板 | codex token 计量驱动循环 + `UsageLimitReached` | R4 | ✅ 完成（资源护栏家族后续补齐两条正交维：denial 断路器 + IterationBudget 分层，见 [capabilities/turn-resource-guards.md](capabilities/turn-resource-guards.md)） |
| **K3** ✅ | **长期记忆 swap/缺页接口**（demand-paging）。**已落地** `1bd57b1`：`context/memory.py::MemoryStore` 协议（`prefetch` page-in 注入 prompt 尾部 / `writeback` dirty-page / `on_pre_evict` 换出抢救 digest 折进保留段 / `on_session_end` teardown）+ `NullMemoryStore`；engine/pool/TurnRunner（含子 turn）透传，默认 None=无内存层级；全 best-effort（钩子异常不打断 turn） | 机制=内核（协议：换入/写回/换出前抢救）；后端=userspace | hermes `agent/memory_provider.py`（剔除业务字段，无 R1 违例） | R2 | ✅ 完成 |
| **K4** ✅ | **总线流控**（入站背压 + 出站丢弃非静默）。**已落地** commit `630c738`：submission 队列 bounded（`submission_queue_size` 默认 256，submit await put → 满则业务侧阻塞）；event 满不再静默丢——累计 `engine.events_dropped` + WARNING（consumer 自检漏事件），但**不阻塞 emit**（慢 consumer 不拖死主 actor，lossy-but-accounted） | 机制=内核（流控） | codex bounded 入站 + 非阻塞出站 | R3 | ✅ 完成 |
| **K5** ✅ | **取消终态守卫**。**核对：双重终结/孤儿在 taifeng 不存在**（协作 token + 编排后 `_sample_once` 成对追加 fc+output → 结构性恰好一次；codex AtomicBool 针对的竞态不适用）。**已落地** commit `bc09ad9`：`_invoke` 按取消来源分流——token 取消→优雅终结为 cancelled 结果（恰好一次）；外部 `task.cancel`→不吞、向上传播（正确 asyncio 卫生）。grace：工具本就协作式（=默认 drain），无需额外档位 | 机制=内核 | codex `parallel.rs` `terminal_outcome_reached` | R4 | ✅ 完成 |
| **K6** ✅ | **/proc 自省面**。**已落地**（见下方 commit）：`engine.introspect()` 返回只读快照（在飞 submission / turn_index / spawn 配额 active+total / 会话 token / events_dropped / 上下文占用 / cache 健康度）；`pool.introspect()` → 各活跃 session 的快照。纯读无副作用。**收口增强**：`pending[]` 逐条暴露 `cancel_requested`（取消已请求但 turn 未收尾）——对标 claw-code `lane_board` 存活看板的**可纯读那一半**；staleness 阈值判定需墙钟+策略，按 R1 留宿主 | 机制=内核 | codex `thread_manager::list_live_thread_spawn_edges`；claw-code `task_registry::lane_board` | R3 | ✅ 完成 |
| **K7** ✅ | **depth/谱系从持久态可重导**。**已落地**（见下方 commit）：子 skill 派发把 `parent_thread_id` / `spawn_depth` / `stack_path` 写入 `ThreadMetadata.extra`（`create_thread(extra=...)` 全链路：MessageStore 协议 + _HookEmittingStore + JsonlMessageStore），resume 可从持久谱系重导深度/特权。**fork ≠ 持久快照那半判为刻意设计**：taifeng 给子 skill「干净 seed」是有意的隔离（非父 CoW bloat），非缺口 | 机制=内核（谱系持久）；隔离模型=刻意设计 | codex `flush-before-fork`；openclaw 从 lineage 重建 spawnDepth | R5 | ✅ 完成 |

## 第二轮全面 review（2026-09-28）：K8–K19

按 OS 内核十二维（进程 / IPC / 中断 / 内存 / syscall / 保护 / 资源配额 / 驱动 / 持久化 / 加载器 /
自省 / 认知回路）重新清点，四路并行对照参照仓并在源码逐条核实。K1–K7 的机制仍成立，但发现**现有
保证不成立**与**机制缺口**共 12 项，已全部补齐：

| # | 缺口 | 维度 | 落地 | ADR |
| --- | --- | --- | --- | --- |
| **K8** ✅ | 上下文 token 只靠 `len/3.5` 粗估，看不到 system / 工具开销，CJK 严重低估；Anthropic `input_tokens` 不含缓存 | 内存管理 | 实测锚点 + 增量粗估、usage 口径归一、`output_reserve_tokens`（真实 LLM：粗估误差 98% → 校准 4.1%） | 0043 |
| **K9** ✅ | 会话 token 上限（K2）可被 call_skill / spawn 子树绕过：只有根 turn 入账 | 资源配额 | 整棵 turn 树共享 `SessionUsageMeter`，采样即入账，按 skill / thread 归因 | 0044 |
| **K10** ✅ | 工具执行中崩溃：Chat 路径意图丢失、resume 后副作用静默重复；`effect_kind` 恢复时无人读 | 持久化 | 写前 `tool_intent` + 冷恢复按副作用分流（可重发 / 回查 / `TOOL_OUTCOME_UNKNOWN` 交人） | 0045 |
| **K11** ✅ | 原生 Anthropic / Gemini 不保留 thinking 签名，工具续传被拒 | 驱动 | 不透明 `reasoning_state` 通道 + Gemini `thoughtSignature` 往返 | 0046 |
| **K12** ✅ | 工具参数不按 schema 校验，缺字段 / 类型错直进 handler | syscall | 派发前子集校验 + 改参反馈，全路径单一入口 | 0047 |
| **K13** ✅ | 工具集不能运行时增删；MCP 忽略 `list_changed`、只有 stdio | syscall | `unregister` / `replace` / `tool_set_changed`；`bind_mcp_tools`；streamable HTTP | 0048 |
| **K14** ✅ | 取消不带原因、无墙钟截止时间；provider 首字节前阻塞时取消迟迟不生效 | 中断 | `CancelReason` 级联、`deadline_seconds`、`interrupt_on_cancel` 原地打断 | 0049 |
| **K15** ✅ | 后台任务完成只能轮询 | IPC | 完成回调 + 经 peer mailbox 投回发起 thread | 0050 |
| **K16** ✅ | shell 类工具不经执行器协议，沙箱无统一 seam；`shell_exec` 不响应取消 | 保护 | `CommandExecutor` seam + 取消即 kill | 0051 |
| **K17** ✅ | cache 失效只有三段指纹，模型切换 / 前缀改写 / 同名 schema 变化都落 `unknown_drop` | 自省 | 分段指纹（model / 消息前缀 / 工具 schema） | 0052 |
| **K18** ✅ | 审计 Journal 不能 resume，跨进程写者无互斥与接管 | 持久化 | 见 ADR 0053 | 0053 |
| **K19** ✅ | 录制的 Journal 无法用于确定性回放 | 可测试性 | `JournalReplayClient` 按请求摘要匹配 | 0054 |

**判为非缺口（userspace / 宿主）**：model failover 链、客户端限流、成本定价（均可按断路器范式做成
`ModelClient` 包装器）；cron / 定时唤醒（宿主到点提交）；优先级抢占；会话 fork；计划模式（可变
`PermissionPolicy`）；子 agent 结构化返回；输入来源 / 污染标记（无业务驱动，按规则挂起）。

## 能力侧 review（2026-09-28 / 29）：C1–C16

机制齐之后再看「能力是否真的可用」：同样四路对照参照仓并逐条核实，发现既有能力实际失效（P0）、该补的能力（P1）
与只定协议的候选（P2），已全部处置：

| # | 问题 | 级别 | 落地 | ADR |
| --- | --- | --- | --- | --- |
| **C1** ✅ | 原生 Anthropic / Gemini 丢弃历史中段 system（压缩摘要 / pinned / 预算提示 / 记忆全失效） | P0 | 原位改写为 `<system-reminder>` user 文本 | 0055 |
| **C2** ✅ | MCP 工具一律落 `pure`，崩溃恢复引导重发写操作 | P0 | 默认 `external_non_idempotent`；`trust_annotations` 才按 hint 细分 | 0057 |
| **C3** ✅ | `reasoning_effort` / `temperature` / `max_output_tokens` 从未被设置 | P0 | SKILL.md `inference` 块，按 entry skill 下发 | 0056 |
| **C4** ✅ | skill 加载静默跳过 / 截断 / `bool()` 误读 | P0 | 严格加载，加载期报错 | 0058 |
| **C5** ✅ | 压缩把用户原话一并转述；续接提示语只在文档里 | P1 | 压缩条目 = 续接前言 + 最近用户原话 + 摘要（新增当前工作 / 错误与修复段） | 0059 |
| **C6** ✅ | `read_skill` 读不到附属文件（渐进加载缺第三层） | P1 | `read_skill(skill_id, path)`，限定 skill 目录 | 0060 |
| **C7** ✅ | PostToolUse 不能改写输出；工具结果无统一上限 | P1 | `output_override` + `ContextBudget.max_tool_result_bytes` | 0061 |
| **C8** ✅ | 只在 anchor 打一个缓存标记；`ttl_seconds` 是死字段 | P1 | Anthropic 尾部滚动断点 + 5m / 1h TTL | 0062 |
| **C9** ✅ | MCP 结果丢图片 / structuredContent；server 请求无人应答；版本不一致 | P1 / P2 | 无损投影 + `ElicitationHandler` + 2025-06-18 协商 | 0063 |
| **C10** ✅ | 模型找文件只能借 shell；记忆只能被动 page-in | P2 | opt-in `glob` / `grep` / `memory` 工具 | 0064 |
| **C11** ✅ | todo 清单只在压缩后钉回 | P2 | `reinject_every_turns` 周期重注 | 0065 |
| **C12** ✅ | 公共 API 无分层、无弃用机制 | P2 | 稳定 / 实验 / 内部三层 + 快照 + `DeprecationWarning` | 0066 |
| **C13** ✅ | 战绩散落各 thread，无聚合入口 | P2 | `SkillFitnessStore` + sink 适配（只沉淀不决策） | 0067 |
| **C14** 📝 | 文件（PDF）输入 | P2 | 预留契约 `llm-file-input`，待有输入路径时实现 | 0067 |
| **C15** 📝 | 多模型路由 / 回退 | P2 | 组合契约 `model-routing-composition`（userspace 实现） | 0067 |
| **C16** ⛔ | `allowed-tools` 作为 `tool_names` 别名 | P1 | 判为不做：免审批 ≠ 可见白名单，保留在 `frontmatter_raw` | 0060 |

## 优先级与建议

- ✅ **K1–K7 全部补齐**：K1 广度准入（`838265c`）/ K2 资源强制（`dc633ff`）/ K3 swap 内存层级（`1bd57b1`）/ K4 总线流控（`630c738`）/ K5 取消终态守卫（`bc09ad9`）/ K6 /proc 自省（`aadced5`）/ K7 谱系持久（本批）。
- **内核机制层至此长齐**：进程模型（fork/spawn 配额）、IPC（双总线+流控）、内存管理（压缩换页+swap）、资源准入与强制、中断（取消终态）、保护（permission）、自省、谱系持久——七维全覆盖。
- 2026-09-28 第二轮 review 发现上述机制中有「保证不成立」的实现缺陷（K8–K11）与新机制缺口（K12–K19），已补齐，见上节。

## 业务可消费性收口（last-mile：机制齐 ≠ 开箱可用）

> 第二轮四方复核（2026-05-30）确认 **K1–K7 机制无新增缺口**——参考实现里唯一的候选（claw-code `lane_board` 的 submission 状态机 + 心跳存活检测）拆解后：staleness 阈值=宿主职责（同"跨 engine 调度=宿主"一类），内核侧那一丝已由 K6 `introspect().pending[].cancel_requested` 补上。但**"机制接到构造器"与"业务照文档/类型就能上手"是两件事**，收口如下：

- ✅ **`configurable-knobs.md` 补 K1–K4 旋钮 + K6 自省面**：之前该权威清单 `grep` K 旋钮 = 0 命中（业务读文档发现不了 `max_concurrent_spawns` / `max_total_spawns` / `max_session_tokens` / `memory_store` / `submission_queue_size` / `introspect()` / `events_dropped`）。已补 §1.0 内核资源旋钮表 + §3.1 自省面。
- ✅ **K6 `introspect()` 增强**：`pending[]` 逐条取消态（见 K6 行）。
- ⛔ **`SpawnLimitError` 刻意不导出**（修正一轮 audit 的误判）：它在 `loop/turn.py` 被捕获并转成 **`SkillSpawnRejected` 事件**，**从不逃到业务层**——K1 的业务正确面是那个**已导出的事件**，不是捕异常。导出这个内部异常是 cargo-cult，故不做。
- **结论**：作为微内核，**机制已具备、可支撑业务接入**；本轮把"开箱即用面"（文档 + 自省增强）补齐。

> 与既有进度的关系：P0/P1/P2（见 roadmap）补的是**机制的完善度**（压缩正确性、错误分类、可见性）；本文 K1–K7 暴露的是**缺失的子系统**。下一阶段的重心从"打磨已有机制"转向"补齐内核子系统"。

## 落地纪律

- 每个 K 项落地前先在 `docs/architecture/capabilities/` 定/更新契约，**显式声明对 R1–R5 的影响**（K1–K7 多触 R2/R4/R5）。
- 机制进内核（`src/`），策略（上限值 / 后端 / 配额数）留给业务注入——守 R1。
- 落地后在 `hermes-gap-roadmap.md` 登记对应 feature 进度，保持两文一致。

## 引用入口

- R1–R5 红线：`CLAUDE.md` / `AGENTS.md`
- 能力进度追踪：`docs/architecture/hermes-gap-roadmap.md`
- 架构总览：`docs/architecture/overview.md`
- 四方参照源码：`<opensource>/{codex, claw-code, hermes-agent, openclaw}`
