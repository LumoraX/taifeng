# web_ui 集成「跨根 turn 异步交互」demo 设计（detached-spawn + turn-rewind）

> 状态：设计已与用户确认（脊柱方案 + 三阶段 + 每专家一卡 UX）。下一步：writing-plans。
> 关联能力契约：`docs/architecture/capabilities/detached-spawn.md`、`docs/architecture/capabilities/turn-rewind.md`。
> 关联 ADR：0015（detached-spawn）、0014（turn-rewind）。

## 目标

把两个「根 turn 完成后仍有异步交互」的内核能力接进 `examples/web_ui/`，让浏览器能实时演示：

1. **multi_expert_consult** —— detached-spawn 并发多专家 + 错峰 HITL + join-barrier 自动联合会诊聚合。
2. **turn_rewind** —— 一条自治链跑完后，回退到任意可寻址节点重跑（`re_reason` / `retry_tool`）。

两者**共享一条后端脊柱**：事件桥（bridge）必须能在「根 turn 完成」之后继续跟踪异步活动（spawn 在后台跑、Rewind 重跑都是新 submission），而现有 bridge 在根 turn 完成即退出、且按单一 `submission_id` 过滤。

## 范围与非目标

**范围**：仅 `examples/web_ui/`（server.py + static/index.html）与两个 demo 的 SKILL.md 接线校验。新增一个 MockClient 驱动的 smoke 验证脚本。

**非目标**：
- 不改 `src/taifeng/` 内核（detached-spawn / turn-rewind 能力已实现并测试）。如实现中发现内核缺口，单独提 issue/spec，不在本 spec 内顺手改。
- 不做客户端（customer-frontend）集成。
- 不做鉴权 / 多租户（web_ui 是单机 demo）。
- 不追求真实 LLM 下 100% 稳定触发（demo 性质；靠 SKILL.md 指令 + sample_prompt 引导）。

## 关键事实（已核对源码）

- **engine 是 per-session 的**：`pool.get_or_create(session_id=f"{demo_id}:{session_id}")` 一个 session 一个 engine；`engine.subscribe_all()` 拿到的事件天然全属本 session。现有 bridge 的 `submission_id` 过滤只为区分「同一 engine 内并发提交」。
- **spawn 事件的 submission_id ≠ chat sub_id**：`spawn_driver.py` 里 spawn 生命周期事件用 `submission_id=handle_id`、barrier 事件用 `submission_id=barrier_id`、Resume 续跑事件用该 Resume 的 `sub.id`。故现有 `if ev.submission_id != sub_id: continue` 会**丢掉所有 spawn/barrier 事件**。
- **`has_live_spawns()` 把 suspended 计为存活**（`spawn_driver.py:542`，引用计数保活）：专家等 HITL 期间 engine 不会被释放，chat bridge 也据此保持存活。
- **事件普遍带 thread_id**：turn 事件 `data.thread_id`；spawn_started `data.child_thread_id`；spawn_suspended `data.thread_id` + `data.pending[].request_id`；spawn_completed `data.result`；join_barrier_registered `data.{barrier_id,handle_ids}`；join_barrier_fired `data.then_thread_id`。
- **Rewind 接口已就绪**：`engine.rewind_nodes() -> list[RewindCheckpoint]`、`Rewind(node_id, mode in {re_reason,retry_tool}, new_args?)` op、`RewindCheckpointRecorded`/`RewindRejected` 事件。RewindCheckpoint 字段：`node_id / kind / history_len / cache_anchor / iteration_index / call_id? / target_id? / inner_history_len? / args_digest?`。

## 架构脊柱：bridge 的 detached 变体（不新建组件）

`DemoMeta` 增两个布尔，旧 demo 默认 `False`、行为零变化：

| 字段 | 含义 |
| --- | --- |
| `streams_detached: bool = False` | bridge 走 detached 分支（跨提交跟踪 + spawn-aware 退出） |
| `wants_spawn_tools: bool = False` | pool 构建时把 `spawn_skill/await_skills/join_skill/kill_skill` 注入 `extra_tools` |
| `wants_rewind: bool = False` | 前端在根 turn 完成后拉节点表、显示 rewind 交互（仅 turn_rewind demo 置 True） |

`_bridge_events(demo_id, session_id, engine, sub_id, *, detached: bool)`：

- **非 detached（现状不变）**：按 `submission_id == sub_id` 过滤；根 turn `turn_completed/turn_failed(is_root)` 或根 thread `turn_suspended` 退出。
- **detached 分支**：
  - 不按 `submission_id` 过滤（engine 已 session 隔离，全转发）。
  - 维护三个 bookkeeping：
    - `root_done: bool` —— 见 `turn_completed/turn_failed` 且 `data.is_root` 置 True。
    - `open_barriers: set[str]` —— `join_barrier_registered` 加 `barrier_id`；`join_barrier_fired` 移除。
    - `pending_then_threads: set[str]` —— `join_barrier_fired` 加 `then_thread_id`；该 thread 的 `turn_completed/turn_failed` 移除。
  - **退出谓词**（每事件后判定）：`root_done and not engine.has_live_spawns() and not open_barriers and not pending_then_threads`。
  - `shutdown` 事件兜底退出（沿用 `_get_shutdown_event()`）。

`/api/resume`：`if not meta.streams_detached:` 才 `create_task(_bridge_events(...))`。detached demo 的 chat bridge 仍存活（`has_live_spawns()` 含 suspended），Resume 续跑事件经它回流——**避免重复推送**。

> 退出谓词为纯事件驱动、无 timeout：joint-consult（then_thread）跑完前 `pending_then_threads` 非空，bridge 不会提前退出，最终会诊报告文本不丢。

## Phase 0 —— 共享脊柱（server.py）

**改动**：
1. `DemoMeta` 加 `streams_detached` / `wants_spawn_tools` 两字段（含中文 docstring）。
2. `_bridge_events` 增 `detached` 形参与 detached 分支（上节谓词 + 三集合）。
3. `/api/chat` 调 `_bridge_events(..., detached=meta.streams_detached)`。
4. pool 构建处（`_get_or_create_pool` 内 `extra_tools` 拼装段）：`if meta.wants_spawn_tools:` 追加四个 spawn 工具（import `make_spawn_skill_tool/make_await_skills_tool/make_join_skill_tool/make_kill_skill_tool`）。
5. `/api/resume`：detached demo 不另起 bridge。

**前端脊柱**：detached demo 的 SSE 流不再隐含「单提交」语义；前端按 **事件 kind + handle_id/thread_id** 渲染，不假设单一 submission。

## Phase 1 —— multi_expert_consult

**后端**：注册 DemoMeta：
```
"multi_expert_consult": DemoMeta(
    demo_id="multi_expert_consult",
    title="🩺 多专家会诊 (并发 spawn + 错峰 HITL + 联合会诊)",
    description="orchestrator 一个 turn 内 spawn 多个专科专家（各自 detached child thread），"
                "各专家错峰 HITL，全终态 → join-barrier 自动起 joint-consult 聚合。",
    skills_dir=EXAMPLES_DIR / "multi_expert_consult" / "skills",
    entry_skill_id="orchestrator",
    sample_prompt="我最近血压偏高、体重也涨了，帮我看看身体情况。",
    hitl_on_skill_dispatch=False,
    streams_detached=True,
    wants_spawn_tools=True,
    wants_user_input_tool=True,
)
```

**SKILL.md 校验/微调**：`orchestrator/SKILL.md` 必须指示真实 LLM：① 对每个相关专科 `spawn_skill`，② 之后 `await_skills([句柄...], then_skill_id="joint-consult")` 登记 barrier。demo.py 用 Mock + 手调 `set_join_barrier`，web 走真 LLM 必须由 LLM 经 `await_skills` 自登记。实现时先 `skill show` 看现状，缺指令则补 skill body（仅补指令文字，不改 frontmatter 契约）。

**前端（static/index.html）—— 每专家一张卡**：
- 新增「专家面板」容器（detached demo 才显示）。卡片以 `handle_id` 为键。
- 卡片状态机（事件驱动）：
  - `spawn_started{skill_id, handle_id, child_thread_id}` → 建卡（running，标题=skill_id）。
  - `spawn_suspended{handle_id, thread_id, pending:[{request_id, detail:{prompt, response_schema}}]}` → 卡内渲染**独立表单**（复用 `renderForm` 的 schema 渲染逻辑，但锚点存在卡上：`{thread_id, request_id, schema}`），状态→「等你回答」。
  - 卡内表单提交 → `POST /api/resume {demo_id, session_id, thread_id, request_id, payload}` → 卡→running。
  - `spawn_completed{handle_id, result}` → 卡→完成，显示 result。
  - `spawn_failed{handle_id, error}` / `spawn_cancelled{handle_id}` → 卡→错误/已取消。
- **并发**：多卡可同时处于「等你回答」（错峰：A 已完成、B 仍待答）。每卡表单锚点独立，互不覆盖。
- barrier：`join_barrier_registered{barrier_id,handle_ids}` → 面板顶部「会诊 barrier：待 N 位专家」指示；`join_barrier_fired{then_thread_id}` → 「联合会诊进行中」，then_thread 的 `assistant_text` 汇入「最终会诊报告」区。
- timeline：为 `spawn_started/suspended/completed/failed/cancelled`、`join_barrier_registered/fired` 加 CSS class（`.evt.k-spawn_*` 等）+ 事件渲染分支。

## Phase 2 —— turn_rewind

**前置：把 turn_rewind 的 skill 落盘（单一真相）**：现状 `examples/turn_rewind/demo.py` 把 `orchestrator`(entry:true, child_skills=[analyzer]) + `analyzer`(entry:false) 内联成字符串、运行时 `_write_skills` 写临时目录。web_ui 需要磁盘 skills 目录，故：
- 抽出两个 SKILL.md 到 `examples/turn_rewind/skills/{orchestrator,analyzer}/SKILL.md`（内容即现有 `ORCHESTRATOR_SKILL`/`ANALYZER_SKILL`，body 指令对真实 LLM 同样成立：orchestrator 先 `call_skill("analyzer")` 再综合）。
- demo.py 改为从该磁盘目录加载（删 inline 字符串 + `_write_skills`），保持单一真相、Mock 路由不变。

**后端**：
- 注册 DemoMeta：`streams_detached=True`（rewind 重跑回流），`wants_spawn_tools=False`，`wants_rewind=True`，`skills_dir=EXAMPLES_DIR / "turn_rewind" / "skills"`，`entry_skill_id="orchestrator"`。
- `GET /api/rewind_nodes/{demo_id}/{session_id}` → `engine = await pool.get_or_create(...)`；返回 `[{node_id, kind, target_id, args_digest, iteration_index} for cp in engine.rewind_nodes()]`。
- `POST /api/rewind` `{demo_id, session_id, node_id, mode, new_args?}` → `engine.submit(Rewind(node_id=..., mode=..., new_args=...))` + `create_task(_bridge_events(..., detached=True))`。

**前端**：
- `wants_rewind` demo 的根 turn 完成后，拉 `/api/rewind_nodes` 渲染**节点表**（每行：node_id、kind、target_id、args_digest）。
- 点节点 → 选 `re_reason` / `retry_tool`（dispatch 节点可填 new_args JSON）→ `POST /api/rewind` → 重跑经 bridge 回流到 timeline。
- `RewindRejected` 事件 → 行内提示原因；`RewindCheckpointRecorded` → 可选刷新节点表。

## 错误边界

| 场景 | 行为 |
| --- | --- |
| Resume 未知/非挂起 request_id | engine `SuspensionResolver` 拒 → 错误经事件流回 → 前端卡片标红，不静默吞 |
| Rewind 未知 node_id | `RewindRejected` 事件 → 前端节点行提示 |
| 无 LLM key | spawn/rewind demo 依赖 LLM；沿用现有 chat 路径（无 model 不建 pool / 503），不另造降级 |
| bridge 卡死（spawn 永不终态） | `shutdown` 事件兜底打断；`spawn_failed` 终态化解除保活 |
| 重复推送 | detached demo 的 `/api/resume` 不另起 bridge → 杜绝 |
| 多 session 并发同 demo | engine 按 `demo_id:session_id` 隔离，bridge 各自独立 |

## 测试 / 验证（DoD）

`examples/` 不在 `tests/`（`testpaths=["tests"]`），CI 禁真 API。故：

1. **自动化 smoke（MockClient，无需 key，可复跑）**：新增 `examples/web_ui/smoke_detached.py`：
   - 用 `httpx.ASGITransport` 挂载 web_ui `app`，并以 MockClient 注入（`RoutingMockClient`，复用 multi_expert_consult/demo.py 的路由标记）。
   - multi_expert：POST `/api/chat` → 订阅 `/api/events/...` → 断言依次出现 `spawn_started`×2 → `spawn_suspended` → POST `/api/resume`（按 thread/request_id）→ `spawn_completed` → `join_barrier_fired` → 最终报告文本。
   - turn_rewind：POST `/api/chat` → 根完成 → `GET /api/rewind_nodes` 拿到节点 → POST `/api/rewind` → 断言重跑事件回流。
   - 退出码非 0 即失败（便于纳入本地校验）。
2. **内核回归**：`PYTHONPATH=src uv run pytest tests/` 全绿（本次不动内核，跑一遍兜底）。
3. **真 LLM 人工确认**（.env 真 key）：`PYTHONPATH=src uv run python examples/web_ui/server.py` → 浏览器跑两 demo 各一遍，确认卡片状态机 / 节点表交互正常。key 仅经现有 bootstrap 注入，不读取/打印/提交。

## R1–R5 影响声明

- **R1 业务零侵入**：仅改 `examples/web_ui/`（本就是 demo 层，不打包），`src/` 不动。两个新 DemoMeta 字段是 demo 配置，非内核概念。
- **R2 Cache 友好**：不涉及压缩路径。
- **R3 可观测**：完全复用既有 EventMsg（spawn_*/join_barrier_*/rewind_*）；不新增事件 kind。
- **R4 可取消**：bridge 由 `shutdown` 事件兜底退出；不阻塞主 actor。
- **R5 可 resume**：不改 store；rewind/resume 走既有 op。

## 文件清单

- 改：`examples/web_ui/server.py`（DemoMeta 三字段、bridge detached 分支、spawn 工具注入、resume 分流、rewind 两端点、两 DemoMeta 注册）。
- 改：`examples/web_ui/static/index.html`（专家卡面板 + 并发表单 + spawn/barrier 事件渲染 + rewind 节点表）。
- 校验/微调：`examples/multi_expert_consult/skills/orchestrator/SKILL.md`（真 LLM 自登记 barrier 的指令）。
- 新增：`examples/turn_rewind/skills/{orchestrator,analyzer}/SKILL.md`（从 demo.py 内联字符串抽出落盘）。
- 改：`examples/turn_rewind/demo.py`（改读磁盘 skills，删 inline 字符串 + `_write_skills`）。
- 新增：`examples/web_ui/smoke_detached.py`（MockClient ASGI smoke）。
- 同步：`examples/web_ui/README.md`、`examples/README.md`（新增两 demo 入口 + detached bridge 说明）。
