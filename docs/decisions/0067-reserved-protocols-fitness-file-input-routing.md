# ADR 0067：预留协议——skill 战绩聚合、文件输入、多模型路由组合

- 状态：Accepted
- 日期：2026-09-29
- 关联：[skill-outcome-record § 战绩聚合](../architecture/capabilities/skill-outcome-record.md)；[llm-file-input](../architecture/capabilities/llm-file-input.md)；[model-routing-composition](../architecture/capabilities/model-routing-composition.md)；ADR 0017 / 0042 / 0066

## 背景

能力侧 review 的 P2 列出三项「先定协议」的候选：skill 战绩的跨会话沉淀、用户文件（PDF）输入、多模型路由与回退。三者都已有业务需求的影子，
但都不宜由内核直接实现完整功能：战绩的使用策略（召回加权 / 提拔 / 逐出）尚未设计；文件输入没有输入路径；路由与回退按 ADR 0017 规则④属 userspace。

## 决策

1. **skill 战绩聚合：落代码，只沉淀不决策。** `SkillFitnessStore` 协议 + `SkillFitnessRecorder`（TelemetrySink 适配已有的
   `skill_outcome_recorded` 事件）+ `InMemorySkillFitnessStore` 参考实现，导出在 `taifeng.experimental`。用 sink 适配而非在
   `EnginePool` 新增注入参数：事件已是完整的数据源，无需再穿透 pool → engine → runner 构造链。内核任何路径不读 fitness。
2. **文件输入：只写预留契约，不写代码。** 固定 `FileAttachmentV1` / `FilePart` 形状、`"file"` 能力门控、策略、脱敏与四家 provider 映射。
   不先加一个无人产出、各 provider 全拒绝的类型：那是死代码，且新 part 进入 content union 需同步改所有 provider 分支，应与真实输入路径一起落地。
3. **多模型路由 / 回退：写组合契约。** 内核不实现路由；契约规定包装器的叠加顺序（回退在每个 endpoint 的断路器之外）、零产出才回退、
   按 failure_class 决定是否回退、能力取交集、不同协议不混组、可观测与缓存影响。

## 否决的方案

- **在 EnginePool 增加 `skill_fitness_store=` 参数**：需要穿透多层构造（`pool.py` 已超 800 行），而事件流已提供全部数据。
- **FilePart 先入类型系统、各 provider 统一拒绝**：见决策 2。
- **内置 fallback 链**：规则④；且回退策略与业务的成本、合规、模型评估强相关。

## 验证

`tests/skill/test_fitness.py`（4 例）：payload 往返、缺字段显式失败、内存实现计数与 call_id 去重、真实 pool 中
call_skill 终态经 recorder 聚合进 store。文件输入与路由组合为文档契约，无代码验证。
