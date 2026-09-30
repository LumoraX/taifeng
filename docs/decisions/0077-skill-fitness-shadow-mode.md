# ADR 0077：按战绩算分与工作集规划先以影子模式上线

- 状态：Accepted
- 日期：2026-09-30
- 关联：[skill-working-set 契约](../architecture/capabilities/skill-working-set.md)；
  [skill-outcome-record § 战绩聚合](../architecture/capabilities/skill-outcome-record.md)；
  设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §6 相位 5；ADR 0017 / 0024 / 0067

## 背景

工具认知回路的相位 2（召回与验证）、战绩沉淀与聚合已落地。相位 5（按战绩提拔 / 逐出 / 隔离）在设计稿里
约定的上线策略是「先 shadow：算 fitness、记日志、不真提拔，战绩信号验证可靠后再开自动提拔 / 逐出 / 隔离」，
但连影子模式都尚未开始——没有算分口径，也就无从积累可供核对的数据。

命中 ADR 0017 规则②（模型认知回路原语）。

## 决策

1. **算分 = 成功率的 Wilson 置信下界**，可选按平均 token 成本折减。设计稿的 `fitness = f(频次, 成功率, 成本)`
   里，频次不单列：Wilson 下界随样本数收紧，「用得多且干得成」已由它一并表达；单列频次项会让高频但成功率
   平庸的 skill 靠次数挤进工作集。
2. **放弃不进分母**。`abandoned` 是取消、人拒绝等终态，不是 skill 的成败。
3. **规划是无状态重算**：目标工作集与隔离集只由当前全部战绩分和策略决定，当前状态只用于算变更。好处是
   确定、可复核、重启后自动一致；代价是每条战绩触发一次全量重算——skill 数量级在千以内时可忽略，
   更大规模由业务实现增量口径的 `FitnessScorer` / 自行调用 `plan_working_set`。
4. **隔离条件只看战绩**：成败样本数达标且成功率不高于阈值。设计稿的「高选中率 ÷ 低成功率」里，
   「高选中」由样本数下限表达——被选中并跑到终态才会产生样本。
5. **影子评估做成 TelemetrySink，不进内核构造链**（沿用 ADR 0067 的接线方式）。内核路径拿不到它的引用，
   「不生效」由结构保证，而不是靠一个可能被误设的开关。
6. **遍历能力单列协议 `SkillFitnessCatalog`**，不加进 `SkillFitnessStore`：后者已有业务实现，给
   `runtime_checkable` 协议加方法会让它们的 `isinstance` 检查失败。
7. **聚合增加成本累计与 `discovered_selections`**，字段都有默认值，既有构造不受影响。
   `discovered_selections` 只计数——选择置信度仍不进入聚合。

## 替代方案

- **指数衰减 / 时间窗口**：让近期战绩权重更高。合理，但需要注入时钟并定义窗口语义，且影子阶段的首要目标是
  验证基础信号是否可靠；留待数据说明需要时再加（`FitnessScorer` 是协议，可替换）。
- **在 `EnginePool` 增加 `skill_working_set=` 参数并以 `mode="shadow"` 运行**：为生效路径预留接线。
  但影子阶段这样做等于让内核持有一个它不该读的对象；生效路径需要哪些接线应在启用时按实际读取点设计；否决。
- **把隔离做成有状态闩锁**（一旦隔离须人工解除）：被隔离的 skill 不再被选中、也就不再产生战绩，闩锁与
  无状态重算在生效后效果相同；而解除的自然方式就是重置该 skill 的战绩；否决。
- **用选择置信度与成功率的背离度算隔离**：把长相引入战绩决策，违反根原则；否决。

## 后果

- 新增实验层符号：`FitnessScorer`、`WilsonFitnessScorer`、`SkillFitnessScore`、`WorkingSetPolicy`、
  `WorkingSetPlan`、`plan_working_set`、`SkillFitnessShadow`、`ShadowEvaluation`、`ShadowObserver`、
  `SkillFitnessCatalog`、`SkillFitnessLedger`。
- `InMemorySkillFitnessStore` 聚合出的 `SkillFitness` 多出成本累计（此前恒为缺省 0 的字段不存在）。
- 未覆盖：让结论生效、skill 来源信任分层——另行立项。
- R1–R5 见契约。

## 验证

`tests/skill/test_working_set.py`：零样本零分、放弃不进分母、样本越多分越高、Wilson 公式核对、成本折减、
分数值域、预算内取最高分、样本不足不提拔、超预算逐出最低分、掉线逐出、无记录逐出、隔离与不得提拔、
少量失败不隔离、解除隔离、零预算、同分排序、重复 skill 拒绝、策略参数校验。
`tests/skill/test_fitness_shadow.py`：聚合成本与发现计数、结论与假想变更、隔离标记、
选择置信度不影响分数、重复投递不重复评估、无关事件忽略、observer 异常上抛、存储缺遍历能力构造期拒绝、
真实 pool 上挂不挂影子模型请求逐项相同。
