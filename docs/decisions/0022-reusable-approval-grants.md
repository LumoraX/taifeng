# ADR 0022: 可复用审批 grant —— 作用域化、确定性生命周期的「预先答好的 ask」

- 状态：Accepted
- 日期：2026-06-15
- 关系：立项依据 ADR 0017 规则①（内核机制缺口）；修订 `permission-gate` 契约「PermissionPolicy 内核不持久化决策」的 R1 边界（见下文「决策三」）

## 背景

2026-06-14 对照 5 个参照实现做差距分析时，claw-code（scoped approvals 跨子任务委派）被列为规则①候选。落地前先读真实代码核对，**backlog 原始描述有两处前提是错的**：

1. **「父批的子不认、peer 之间不互通」是错的**。`PermissionPolicy` 是 engine 级单例，按引用透传给所有 runner（spawn_skill 子 `engine.py` / call_skill 子 `turn.py`，G3 `_SubagentAutoDecisionPolicy` 包一层但 `inner` 仍同实例）。审批状态**本来就全树共享**——`_preapproved_call_ids` 已是「跨 spawn/peer 生效的内核侧授权状态」。委派不是缺口，全树共享是默认。
2. **「`remember_until` 内核不消费」不是 bug，是刻意的 R1 决策**（permission-gate 契约「内核不持久化决策」+ `policy.py` R1 注释）：内核不持 session 权限状态，业务侧在 prompter wrapper 自管 session/always。

收窄后**真正的缺口**：现有三个授权机制——`_preapproved_call_ids`（精确 call_id、一次性）、`_SubagentAutoDecisionPolicy`（整子树 auto 姿态）、`remember_until`+userspace wrapper——之间，**缺一个「作用域化（scope+pattern）、确定性生命周期、内核消费并打事件」的 grant**。userspace wrapper 理论上能做大部分，但有两点做不好：

- **R3 可观测**：grant 命中若发生在 userspace wrapper 里，taifeng telemetry 看不见「这次为什么没问人」——审计断层。这是最强的内核侧理由。
- **R5 resume 一致**：grant 是会话状态，resume 必须恢复；内核知道 resume 路径（`preapprove` 已是先例），userspace 自管易漏。

且它本就是 `_preapproved_call_ids` 的自然推广（精确 call_id 一次性 → scope+pattern 多次），符合规则①「内核机制缺口」。

## 决策

### 决策一：内核消费 grant，复用 PermissionRule 匹配

`permission/grant.py` 新增 `GrantStore`，`PermissionPolicy` 内置一个（`field(default_factory=GrantStore)`，与 `_preapproved_call_ids` 一样不进 `from_dict`/`from_capability_tier`，业务零改造获空 store）。`PermissionGrant`（frozen）的匹配维度全部白嫖 `PermissionRequest` 已携带的字段（scope/target/args/call_chain/thread_id），**匹配段直接构造临时 `PermissionRule` 调 `matches()`**，零重写。生命周期只用确定性 `max_uses` 计数（`src/` 禁 `Date.now`，挂钟 TTL 留 userspace `revoke_grant`）。

### 决策二：grant 是「预先答好的 ask」，绝不越过 deny（核心安全不变量）

`check()` 短路顺序：`0. preapprove → 1+2. 规则(allow/deny 先决) → 2.5 grant(仅 mode==ask 时) → 3. prompter`。grant 排在 deny **之后**，故它只省去重复弹窗、**绝不顶翻 admin 的 deny 规则、绝不提升权限上限**。这是与 `_preapproved_call_ids`（step 0，resume 专用，可越 deny）刻意的区别：resume 是「人已批过这个具体 call」，grant 是「人会在这个 ask 上点 yes」。

> 取舍：曾考虑把 grant 也放 step 0（与 preapprove 同级）。否决——那会让一张宽 grant 顶翻 deny 红线，破坏「deny 绝对」的安全心智模型。

### 决策三：修订「内核不持久化决策」的 R1 边界

本 ADR **部分修订** permission-gate 契约的「PermissionPolicy 内核不持久化决策」：

- `remember_until`（once/session/always）**仍 userspace 自管、内核不消费**，原决策不变；
- **新增** `decision.grant` 字段作为内核消费的复用路径：prompter 答复时顺带 mint 一张 grant，内核记账。

边界判据：内核提供的是**机制**（grant 的数据模型、匹配、生命周期、事件、resume 重种），**判什么该批、grant 存哪持久层仍全留 userspace**。grant 只含内核中性维度（scope/pattern/args/前缀/thread），无业务名词——R1 守住。

### 决策四：全树共享 + 可选 subtree 收窄

grant 随单例 policy 默认全树可见（贴合现实）；`call_chain_prefix` 可把 grant 收窄到某子树（子可用、父/兄弟不可用），实现「审批能下放、也能限域」。无需像 `doom_loop_config` 那样贯穿 pool/engine——grant 随 policy 天然共享。

## 影响（R1–R5）

- **R1**：✅ grant 仅含内核中性维度，无业务语义；判批/持久层留 userspace。**但显式修订了「内核不持 session 权限状态」**——从「全留 userspace」改为「内核提供 grant 机制、策略仍 userspace」（见决策三）。
- **R2**：⚪ 不涉压缩/cache。
- **R3**：✅ `permission_grant_issued` / `permission_grant_hit` / `permission_grant_expired` 经既有 `PolicyTelemetryCallback`（permission 包不依赖 `loop/event`）。
- **R4**：✅ `check()` 不新增阻塞、cancel 语义不变。
- **R5**：✅ grant 是内存会话态，经 `issue_grant` 重种（镜像 `preapprove` 的 resume 路径）；`snapshot()` 把剩余次数反映为 `max_uses` 便于原样重种。

只动 `permission/`（+models 一个字段），不碰 `{llm,loop,context,conversation}` 基础层 → 不触发真实回归台账红线。

> 真实 LLM 验证（如实记录）：grant 是策略层机制、不依赖模型行为触发，以 sim 单测覆盖（`tests/permission/test_grant.py` 15 + `test_grant_policy.py` 7）；capability-matrix 真实验证列标 sim 覆盖。契约见 `docs/architecture/capabilities/permission-gate.md` §可复用审批 grant。
