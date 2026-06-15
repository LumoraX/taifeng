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

### 决策四：全树共享 + 两个互补的收窄键（各管一种嵌套）

grant 随单例 policy 默认全树可见（贴合现实）。收窄有两个**互补**的键，对应两种不同的子嵌套模型——这点初版漏分析，第三方 review 时纠正：

- `call_chain_prefix`：**仅管 call_skill（阻塞）嵌套子树**。那条路径上子 runner 继承父 call_stack（`turn.py` 子 runner `call_stack=parent_stack`），call_chain 含 root→…→child 全路径，故 prefix 匹配得上。
- `thread_id`：**管 spawn / peer detached 子 thread**。detached 子是独立根（`engine.py`「call_stack 留空 → 自判独立根 turn」），call_chain **重置不含父路径**，`call_chain_prefix` 对它永不命中；要收窄到某个 spawn/peer 子，**只能用其独立 `thread_id`**。

二者缺一不可：本功能动机是 spawn/peer 多 actor 委派，那恰是 `thread_id` 而非 `call_chain_prefix` 的场景。无需贯穿 pool/engine——grant 随 policy 天然共享。

### 决策五：grant 仅 inherit 模式生效（auto 子树硬墙）

`_SubagentAutoDecisionPolicy`（auto_deny / auto_allow 子 turn 用）**有意不消费 grant**——auto 模式语义即「子树不走交互式审批、按 fallback 裁决」，grant 是「缓存的交互式审批答复」同属被绕过。故 grant **仅 inherit 模式生效**；auto_deny 借此保持「子树一律拒绝」的硬隔离承诺不被 grant 削弱。这是审查中确认的安全取舍（备选「让 grant 穿透 auto_deny」会削弱隔离，被否）。

### 决策六：id 全局唯一 + 签名 dedup（实现健壮性）

审查暴露两个实现缺陷，已修：① `GrantStore.add` 自动 id 跳过已占用 id（修复 resume 重种后 `_counter` 不前移导致的撞车）、显式重复 id 直接 `raise`（禁 silent dup，否则 consume/revoke 按 id 找首个会锚定漂移）；② 按完整匹配签名（scope/target/args/prefix/thread）dedup（`issue_grant` 幂等、mint 不堆积，防无界增长）。

## 影响（R1–R5）

- **R1**：✅ grant 仅含内核中性维度，无业务语义；判批/持久层留 userspace。**但显式修订了「内核不持 session 权限状态」**——从「全留 userspace」改为「内核提供 grant 机制、策略仍 userspace」（见决策三）。
- **R2**：⚪ 不涉压缩/cache。
- **R3**：✅ `permission_grant_issued` / `permission_grant_hit` / `permission_grant_expired` 经既有 `PolicyTelemetryCallback`（permission 包不依赖 `loop/event`，与既有 `permission_prompt_timeout` 同通道）。
- **R4**：✅ `check()` 不新增阻塞、cancel 语义不变。
- **R5**：⚪（**审查修正：原标 ✅ 是过度声明**）。grant 是**内存 policy 态、同 `rules`**——进程内经 engine 单例 policy 跨 turn 存活；**内核不跨进程自动持久化**（`rules` 也不），业务用 `snapshot()` 序列化 + `issue_grant()` 重种，与重建 `rules` 同理。**不**像 `_preapproved_call_ids` 那样由 engine 在 resume 内部自动注入（那是针对某个挂起 call 的机制，grant 不是）。初版 ADR 写「镜像 preapprove 的 resume 路径」**有误**，已更正。

只动 `permission/`（+models 一个字段）+ `skill/dispatch.py` 一段 docstring，不碰 `{llm,loop,context,conversation}` 基础层 → 不触发真实回归台账红线。

> 真实 LLM 验证（如实记录）：grant 是策略层机制、不依赖模型行为触发，以 sim 单测覆盖（`tests/permission/test_grant.py` + `test_grant_policy.py`，含 id 唯一 / dedup / resume 重种闭环 / call_skill 子树命中 vs spawn chain 不命中）；capability-matrix 真实验证列标 sim 覆盖。契约见 `docs/architecture/capabilities/permission-gate.md` §可复用审批 grant。
