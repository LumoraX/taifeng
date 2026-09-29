# Capability: skill-authorization

> 状态：Experimental（opt-in）。关联 ADR 0017（规则②③）、0022、0024、0088、0089；
> 设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §4、§6 相位 4。
> 实现：`src/taifeng/skill/authorization.py`（协议、参考实现、可发现范围）、
> `src/taifeng/tool/builtins/skill_authorization.py`（`call_skill` 派发处的授权）、
> `skill/dispatch.py`（`DispatchPolicy.authorization`）。经 `taifeng.experimental` 导出。

## Purpose

认知回路 ⑥ 准入：让调用方够得到 `child_skills` 白名单之外的 skill，前提是每一次派发都过授权。
白名单是作者预授权的工作集；白名单之外的 skill 可以被发现、被读说明书，但派发由授权策略逐次裁决。

## 不变量

1. **默认白名单是硬边界**：未注入 `SkillAuthorizationPolicy` 时，召回池、工具暴露、system prompt、
   派发裁决与此前逐字节一致。
2. **授权在准入，不在发现**：`discoverable` 只决定能不能搜到、读到说明书；`search_skills` MUST NOT
   触发 `authorize`。
3. **授权只豁免白名单一层**：放行后，存在 / 深度 / 环 / entry 的结构性裁决、选择置信度分流门、
   `pre_skill_dispatch` hook、`skill_dispatch` 权限审批 SHALL 照常执行。
4. **不可发现 = 不存在白名单外通道**：目标不在可发现范围内时，拒绝与未启用本能力时逐字相同
   （`dispatch_rejected: not_in_whitelist`），且 MUST NOT 调用 `authorize`。
5. **失败关闭**：`authorize` 抛出异常按工具故障处理，不派发。

## 数据契约

### `SkillAuthorizationPolicy`（Protocol）

| 方法 | 契约 |
| --- | --- |
| `discoverable(caller, candidate) -> bool` | 无副作用的同步判断；每轮组装工具清单、每次召回、每次读说明书都会调用 |
| `async authorize(request, *, cancel) -> SkillAuthorizationDecision` | 裁决一次白名单外派发；MUST 可取消（R4） |

### `SkillAuthorizationRequest`

| 字段 | 含义 |
| --- | --- |
| `caller_skill_id` / `target_skill_id` | 发起方与目标 |
| `target_description` / `target_source` | 目标的描述与来源（`SkillDefinition.source`） |
| `origin` | 派发入口；当前只有 `"call_skill"` |
| `reason` | 模型自陈的派发理由 |
| `call_chain` | 调用栈，最深的在最后 |
| `call_id` | 本次工具调用的 id |
| `thread_id` / `submission_id` / `entry_skill_id` / `turn_index` | 会话上下文 |
| `metadata` | 业务透传的 `request_metadata`；内核不解析 |
| `permission_policy` | 当前生效的权限策略（子 turn 内是按 `subagent_approval_mode` 包装后的那个）；未配置为 `None` |

### `SkillAuthorizationDecision`

`granted: bool`、`reason: str`；构造用 `allow(reason="")` / `deny(reason)`。

### 参考实现

| 实现 | 授权方式 |
| --- | --- |
| `CallbackSkillAuthorization(authorize, *, discoverable=None)` | 业务回调（如权限接口） |
| `PermissionSkillAuthorization(*, discoverable=None)` | 向 `request.permission_policy` 发一条 `skill_authorization` 权限请求：规则、可复用授权（grant）、人工审批都沿用权限门；未配置权限策略时拒绝（`no_permission_policy`） |

两者的 `discoverable` 缺省为「注册表里的 skill 都可被发现」。

`PermissionSkillAuthorization` 发出的权限请求：`scope="skill_authorization"`、`target=<目标 skill id>`、
`reason=<模型自陈理由>`，`metadata` 在业务透传内容之上含 `caller_skill_id` / `target_source` / `origin` /
`call_id`。规则别名 `SkillAuthorization(<pattern>)`。`skill_dispatch` 的规则不作用于它。

### 可发现范围

`discoverable_outside(caller, snapshot, policy, capabilities=None, *, on_stack=())` 按 id 升序返回：

| 排除 | 原因 |
| --- | --- |
| 白名单内的、`caller` 自己 | 不属于「白名单之外」 |
| 调用栈上的 skill | 派发必然成环 |
| entry skill | `call_skill` 不能把入口作为子调用 |
| `exposure.model_invocable == False`、`requires` 不满足 | 与白名单内同一套可见性过滤 |
| `policy.discoverable` 返回 `False` | 业务收窄 |

## 行为契约

### 启用

`EnginePool.create(dispatch_policy=DispatchPolicy(authorization=<policy>), skill_recall=<后端>)`。
没有召回后端时模型无从发现白名单外的 skill，但按 id 直接派发仍会过授权。

### 发现

- `search_skills` 的召回池 = 白名单内可见的 child + `discoverable_outside(...)`。
- 白名单外的候选在结果里带 `"requires_authorization": true`；白名单内的候选不带这个键。
- `skill_search_invoked` 事件在有白名单外候选入池时多一个 `outside_pool_size`。
- 存在可发现的白名单外 skill 且有召回后端时，即便 child 列表是 inline 也暴露 `search_skills`，
  并在 system prompt 的 child 块之后追加一段说明。该判定只取决于注册表与策略，整 turn 稳定。
- `read_skill` 可读可发现范围内的 skill（正文与附属文件）；范围之外仍是 `skill_not_visible`。

### 准入（`call_skill`）

```
DispatchPolicy.check
  ├─ 通过 / 其他原因拒绝 → 与此前一致
  └─ not_in_whitelist 且注入了授权策略
       ├─ 目标不在可发现范围内 → dispatch_rejected: not_in_whitelist
       └─ authorize(request)
            ├─ 拒绝 → skill_authorization_denied: <reason>（data.reason = "authorization_denied"）
            └─ 放行 → DispatchPolicy.check(authorized_outside_whitelist=True)
                        → 选择置信度分流门 → pre_skill_dispatch hook → skill_dispatch 权限审批 → 派发
```

授权与 `skill_dispatch` 审批是两个问题：前者问「能不能够到白名单外的这个 skill」，后者问「这次派发
能不能进行」。两者都配置为人工审批时会各问一次；只想问一次时给 `skill_dispatch` 配放行规则或授权。

### 人工审批

`PermissionSkillAuthorization` 配合 `SuspendingPrompter`：turn 挂起，待答请求的 `detail.scope` 为
`skill_authorization`、`related_call_id` 为这次 `call_skill` 的 id。批准后这次调用在续跑的 turn 内重跑
（见 [suspend-resume](suspend-resume.md)）。重跑时授权这道门只占用一次批准，之后的门各自拿到自己的批准。

### 边界

- 分离派发（`spawn_skill`）不走白名单外授权，白名单外的目标仍是 `spawn_rejected: not_in_whitelist`。
- 声明式编排（`orchestration`）里的派发不走白名单外授权。
- 子 thread 内的 `call_skill` 因人工审批挂起后的恢复不在本能力范围内；子 skill 里用规则、
  可复用授权或 `subagent_approval_mode` 裁决。
- 审计模式（`AuditConfig`）不支持：注入授权策略时构造期拒绝，`audit_skill_authorization_unsupported`。

## 事件

| kind | data |
| --- | --- |
| `skill_authorization_granted` | `caller_skill_id`、`target_skill_id`、`call_id`、`origin`、`reason`（授权依据）、`request_reason`（模型自陈）、`call_chain` |
| `skill_authorization_denied` | 同上 |

两个事件都不进 LLM 视图。事件投递失败只记日志，不改变裁决。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| **R1 业务零侵入** | 授权口径、可发现范围都是注入件；请求只带通用上下文，业务语义走 `metadata` ✅ |
| **R2 Cache 友好** | 启用后 system prompt 多一段固定说明、工具清单多 `search_skills`，二者由注册表与策略决定、跨 turn 稳定；不触发压缩 ✅ |
| **R3 可观测** | `skill_authorization_granted` / `skill_authorization_denied`；`skill_search_invoked.outside_pool_size` ✅ |
| **R4 可取消** | `authorize` 接收 `CancellationToken`；参考实现调用前检查取消 ✅ |
| **R5 可 resume** | 授权裁决不落新持久化状态。人工审批的挂起与恢复沿用 suspend-resume；进程内「已获批准」的记忆丢失时至多多问一次 ✅ |
