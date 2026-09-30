# ADR 0089：白名单外 skill 的派发授权

- 状态：Accepted
- 日期：2026-09-30
- 关联：[skill-authorization 契约](../architecture/capabilities/skill-authorization.md)；
  [skill-dispatch](../architecture/capabilities/skill-dispatch.md)、
  [permission-gate](../architecture/capabilities/permission-gate.md)、
  [suspend-resume](../architecture/capabilities/suspend-resume.md)；
  设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §4、§6 相位 4；
  ADR 0017 / 0022 / 0024 / 0088

## 背景

`child_skills` 白名单同时承担两件事：作者预热的工作集，和派发的授权边界。相位 2 的召回因此被限制在
白名单内——调用方永远够不到作者没有预先列出的 skill。skill 数量大、由多个团队各自维护时，
要求每个入口的作者穷举它可能用到的全部 skill 并不现实。设计稿约定的相位 4 是「白名单快速路径 +
非白名单走授权钩子」，此前未实现。

实现过程中发现一个既有缺口：`call_skill` 自身因人工审批挂起后，恢复时在 engine 层用最小上下文重跑，
缺调用栈与调度器，结果是 `call_skill misconfigured`。既有测试都先放行 `skill_dispatch` 以避开它。
白名单外授权的人工审批必须经过这条路径，故一并修复。

命中 ADR 0017 规则②（认知回路原语）与规则③（内核定协议，授权后端由业务承担）。

## 决策

1. **授权策略挂在 `DispatchPolicy.authorization`**。派发策略已经贯穿 pool → engine → runner → 工具上下文，
   授权是派发裁决的一部分；不为它另开一条构造参数链。
2. **协议两个方法：`discoverable`（同步、无副作用）与 `authorize`（异步、逐次）**。发现与准入分开：
   前者决定召回池、`read_skill` 的可见范围和 `search_skills` 是否暴露，后者只在派发时调用。
3. **可发现范围由内核先过滤**：白名单内的、调用方自己、调用栈上的、entry skill、对模型隐藏的、
   `requires` 不满足的一律排除，然后才问策略。策略只能收窄，不能放宽内核的可见性规则。
4. **授权只豁免白名单一层**。`DispatchPolicy.check` 增加 `authorized_outside_whitelist` 参数，
   放行后重新裁决；深度、环、entry、分流门、hook、`skill_dispatch` 审批都不因授权而跳过。
5. **不可发现的目标得到与未启用时逐字相同的拒绝**，且不调用 `authorize`。模型无法借拒绝文本的差异
   探测哪些 skill 存在但对它关闭。
6. **参考实现 `PermissionSkillAuthorization` 复用权限门**，新增权限范围 `skill_authorization`
   与规则别名 `SkillAuthorization(...)`。规则、可复用授权（ADR 0022）、人工审批都不另造。
   它使用请求里携带的**当前生效**权限策略：子 turn 内是按 `subagent_approval_mode` 包装后的策略，
   授权因此自动遵从子树的隔离模式；恢复时 engine 的预批准也落在同一个策略对象上。
7. **授权与 `skill_dispatch` 审批保持为两个问题**。合并成一次询问会让 `skill_dispatch` 的拒绝规则
   被绕过。代价是两者都配置为人工审批时问两次；契约里写明如何只问一次。
8. **`PermissionSkillAuthorization` 记住经人批准的调用**（按 thread 与 call id，上限 256 条，用后即删）。
   批准后整次调用重跑，预批准按 call id 一次性消费；不记住的话，授权这道门每次重跑都会先消费掉
   批准，后面的 `skill_dispatch` 审批永远拿不到，形成反复询问。
9. **启用后即便 child 列表是 inline 也暴露 `search_skills`**，并在 system prompt 追加一段说明。
   否则小白名单的入口无从发现白名单外的 skill。
10. **`call_skill` 获批后在续跑的 turn 内重跑**。Resume 只登记预批准，续跑的 turn 在采样前补跑；
    复用 retry_tool 已有的补跑入口并扩成一批。续跑只在 record 全量核销后发生，故这类批准必须出现在
    结清 record 的那次 Resume 里，否则显式拒绝（`dispatch_approval_requires_full_resolution`）。

## 替代方案

- **在 `EnginePool.create` 加 `skill_authorization=` 参数**：要沿 pool / engine / runner / 子 runner /
  恢复链逐层透传，其中三个文件已在行数上限附近；而 `DispatchPolicy` 已经在这些位置上。否决。
- **白名单外的 skill 须先在本轮被召回过才能派发**：防模型凭空猜 id。但 `discoverable` 已经限定了
  范围，猜中范围内的 id 与搜到它没有安全上的区别；跨 turn「先搜后用」反而会被挡住。否决。
- **在 SKILL.md 增加「允许被谁发现」的声明字段**：属于具体的授权口径，由业务实现
  `discoverable` 读取自己的声明即可；内核不扩 frontmatter。
- **授权通过后跳过 `skill_dispatch` 审批**：见决策 7。否决。
- **恢复时在 engine 层构造一个临时 runner 只为重跑工具**：重跑可能再次挂起（下一道审批、子 skill
  挂起），engine 层要重新实现 turn 的挂起落盘与事件；放进续跑的 turn 则全部沿用。否决。
- **部分核销时先记下批准、等结清后再重跑**：批准只存在于进程内存，重启后丢失，而 record 仍显示
  该请求未结；显式拒绝更诚实。

## 后果

- 新增实验层符号：`SkillAuthorizationPolicy`、`SkillAuthorizationRequest`、`SkillAuthorizationDecision`、
  `CallbackSkillAuthorization`、`PermissionSkillAuthorization`。
- `DispatchPolicy` 多一个字段 `authorization`，`check` 多一个关键字参数；`PermissionScope` 多一个取值。
- 新增事件 `skill_authorization_granted`、`skill_authorization_denied`；`skill_search_invoked` 在有
  白名单外候选入池时多一个 `outside_pool_size`。
- 行为变化（缺陷修复）：根 thread 上 `call_skill` 因审批挂起后批准，现在真正派发；此前回填的是
  `call_skill misconfigured`。
- 审计模式不支持，注入授权策略时构造期拒绝（`audit_skill_authorization_unsupported`）。
- 未覆盖：分离派发与声明式编排的白名单外授权；子 thread 内 `call_skill` 因审批挂起后的恢复
  （仍是 engine 层重跑）；其余依赖 TurnRunner 的工具（如 `spawn_skill`）在恢复时的重跑。
- 每轮采样多一次可发现范围的计算，代价与注册表规模成正比。
- R1–R5 见契约。

## 验证

`tests/skill/test_authorization.py`：可发现范围的各项排除、策略收窄、`check` 只豁免白名单一层、
回调透传与取消、无权限策略拒绝、独立的权限范围与请求字段、`skill_dispatch` 规则不授权、规则别名、
可复用授权、批准不被重复消费。
`tests/loop/test_skill_authorization.py`：未启用时行为不变、召回池与标记、inline 下暴露搜索、
读说明书、放行 / 拒绝 / 不可发现 / 白名单内、授权后端出错、分离派发不变、权限门照常生效、
与分流门叠加、规则授权、人工审批的批准与拒绝、两道审批各问一次、审计模式拒绝。
`tests/loop/test_call_skill_resume.py`：批准后派发、拒绝、一次批准两个、部分批准被拒且状态不变。
