# ADR 0090：按战绩规划的工作集生效，并引入 skill 来源信任分层

- 状态：Accepted
- 日期：2026-09-30
- 关联：[skill-working-set 契约](../architecture/capabilities/skill-working-set.md)；
  [skill-selection-gate](../architecture/capabilities/skill-selection-gate.md)、
  [skill-authorization](../architecture/capabilities/skill-authorization.md)；
  设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §6 相位 5；
  ADR 0017 / 0067 / 0077 / 0088 / 0089

## 背景

ADR 0077 让算分与规划以影子模式上线：结论只记录，不作用到任何内核路径，并把两件事留到后面——
让结论生效、skill 来源信任分层。战绩记录里的 `trust_tier` 字段自 v1 起恒为空。

命中 ADR 0017 规则②（认知回路原语）。

## 决策

### 生效

1. **生效组件 `SkillWorkingSet` 与影子组件并存**，不给影子加「生效开关」。影子的价值在于内核拿不到
   它的引用（ADR 0077 决策 5）；给它加开关等于放弃这条结构性保证。
2. **挂在 `DispatchPolicy.working_set`**，与授权策略同理（ADR 0089 决策 1）。
3. **战绩由内核在落定处直接交给工作集**，不经事件订阅。生效路径不能依赖业务是否记得挂 sink，
   也不能受事件队列背压影响。
4. **结论在 turn 开始时取一次快照，整 turn 使用**。工作集由全部会话共用，别的会话随时可能写入战绩；
   不取快照的话 system prompt 会在 turn 中途改变，缓存前缀失效，且模型在同一轮里看到的 child 列表
   前后不一。代价是提拔与隔离从下一轮起才对模型可见。
5. **提拔只作用于召回模式**：工作集里的 child 在 system prompt 里直接列出，免搜索。inline 模式本来就
   全列，不因提拔重排——重排会改动缓存前缀而不带来新信息。
6. **隔离的作用范围可配**（`flag` / `hide` / `block`），默认 `hide`。隔离依据是统计结论，可能误判；
   默认只对模型隐藏，作者在白名单里显式列出的 skill 仍可按 id 派发。要硬拦的业务选 `block`。
7. **状态不落盘**。工作集与隔离集由存储里的战绩与策略无状态重算（ADR 0077 决策 3），
   进程启动后首个 turn 重算一次即可。

### 来源信任分层

8. **三层：`trusted` / `standard` / `untrusted`**。设计稿提到的 bundled / managed / workspace 是
   具体产品的来源名；内核只定信任的高低，来源到层级的映射由策略给出。
9. **层级由加载目录决定**：`FilesystemSkillRegistry(..., sources={目录: 来源})`。此前注册表把所有
   skill 都标成 `user`，`SkillDefinition.source` 名存实亡。不从 SKILL.md 读层级——自己声明自己可信
   没有意义。
10. **层级只调门槛，不进战绩分**。`TierRule` 可以让某层级不可提拔、提高它的样本门槛、提前隔离或
    豁免隔离；过了门槛之后仍按战绩分排序。层级进分数会让可信来源的平庸 skill 挤掉外部来源的好 skill，
    也违反「只有真干成才涨分」。
11. **层级同时供前两道门使用**：召回候选带 `trust_tier`，`ThresholdSelectionPolicy(trial_tiers=...)`
    让指定层级的候选置信再高也须先试用；白名单外授权请求带 `target_trust_tier`。三层防假
    （长相、战绩、来源）各自独立，又能在同一次派发上叠加。
12. **`DispatchPolicy.trust` 可单独配置**。只配信任策略时只是打标签，不改变任何行为；
    审计模式下也可用。

## 替代方案

- **每次采样都读最新结论**：见决策 4。否决。
- **把工作集快照写进 history，作为一条系统注入**：结论变成会话内容，随压缩被折叠、随 rewind 回退，
  还会让不同会话的 history 因别人的战绩而不同。否决。
- **隔离默认 `block`**：一次误判就让业务流程断掉；且被隔离的 skill 不再产生战绩，无法靠表现自行解除。
  否决。
- **`hide` 时连按 id 派发也算进「隐藏」**：那就是 `block`。保留两档的区别。
- **给 `WorkingSetPolicy` 加按层级的独立预算**：每层各有名额。增加配置面而没有对应的需求；
  `promotable=False` 与样本门槛已经能表达「外部来源要更多证据」。
- **信任层级用整数**：便于比较大小，但引入「层级之间差多少」的伪精度，业务也无从对应。

## 后果

- 新增实验层符号：`SkillWorkingSet`、`WorkingSetView`、`WorkingSetChange`、`TierRule`、
  `SkillTrustPolicy`、`SourceTrustPolicy`。
- `DispatchPolicy` 多两个字段 `trust`、`working_set`；`WorkingSetPolicy` 多 `tier_rules`；
  `plan_working_set` 多 `tiers`；`visible_child_skills` 多 `hidden`；`FilesystemSkillRegistry` 多 `sources`；
  `SelectionCandidate` 多 `trust_tier`；`ThresholdSelectionPolicy` 多 `trial_tiers`；
  `SkillAuthorizationRequest` 多 `target_trust_tier`。全部有默认值，既有调用不变。
- 新增事件 `skill_promoted` / `skill_evicted` / `skill_quarantined` / `skill_released`。
- 启用工作集后，system prompt 会随战绩在 turn 之间变化，每次变化使缓存前缀失效一次。
  `promote_min_samples` 与 `budget` 越保守，变化越少。
- 每条战绩触发一次全量重算，写入串行化；skill 数量级在千以内时可忽略（同 ADR 0077）。
- 审计模式不支持工作集，注入时构造期拒绝（`audit_skill_working_set_unsupported`）。
- 未覆盖：分离派发与声明式编排不产生战绩，被隔离的 skill 经这两条路径仍可派发；战绩的时间衰减；
  真实模型是否会使用直接列出的 child（需真实 LLM 验证）。
- R1–R5 见契约。

## 验证

`tests/skill/test_trust.py`：默认分层、单独指定、自定义映射、非法配置、未配置时层级未知、
按目录指定来源、缺省来源、非法目录与来源。
`tests/skill/test_working_set_runtime.py`：层级提高提拔门槛、禁止提拔、豁免隔离、提前隔离、
层级未知用通用值、层级不改排序、提拔与变更内容、重复投递、超预算逐出、三种隔离作用范围、
好转后解除、放弃不计入、启动时重算、重算只做一次、层级来自注册表与战绩记录、构造期校验、
指定层级须先试用。
`tests/loop/test_working_set_enforcement.py`：提拔从下一轮起可见且本轮不变、inline 列表不重排、
被隔离的 skill 不进召回池与 child 列表、`flag` 不隐藏、`block` 拒绝派发、运行中的失败触发隔离、
未启用时行为不变、只配信任策略时只打标签、审计模式拒绝。
