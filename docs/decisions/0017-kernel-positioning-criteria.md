# ADR 0017: 内核定位与立项判定准则 —— LLM OS 微内核,不是开箱即用产品

- 状态:Accepted
- 日期:2026-06-10
- 关系:固化 CLAUDE.md / AGENTS.md「项目身份」与 R1 红线的**立项裁决规则**;与 kernel-gap-analysis 的「机制 vs 策略」分界一致并将其升格为正式决策;不推翻任何既有 ADR

## 背景

三轮 codex / openclaw / hermes 对比分析把 P0/P1 清零后,剩余 backlog 的立项依据开始模糊:hermes 是"内核 + 自带电池"的发行版形态(todo、delegate、memory 后端全部内置),逐项对齐它的功能表会让 taifeng 滑向产品化——与微内核定位冲突。todo builtin 落地时这个张力暴露出来:它的必要性论证里"hermes 有"占比偏大,需要一个明确的裁决准则,避免每个后续 change 重新争论一遍定位。

## 决策

### 一、定位:taifeng 是 LLM OS 的内核,**用来开发开箱即用产品,自身不是产品**

类比操作系统:taifeng 提供内核机制(调度 / 上下文 / 取消 / 恢复 / 通信)与内核能力(模型认知原语),业务方在其上构建发行版(求本 MDT 即第一个)。对标三家的正确用法是**在别人的实现里找我们内核缺的机制**,不是对齐功能清单。

### 二、立项判定:四条裁决规则(按序适用)

1. **内核机制** —— 调度、上下文压缩/cache、取消、挂起/恢复、spawn/通信、可观测等 OS 级原语的缺口 → **做**。
   (例:reactive-compaction-recovery、peer-mailbox、turn-resource-guards)
2. **模型认知回路需要的** —— 长程 LLM 在执行过程中维持自身认知状态所需的原语:自我 review、自我检查、任务清单(工作记忆)、状态穿越压缩 → **做**。
   这是"AI 模型内部需要的"判据:todo builtin 据此正名——它不是产品功能,是模型的**工作记忆原语**(`PinnedStateSource` 协议 + `TodoStore` 参考实现)。
3. **外部成熟服务能承担的** —— DB、向量记忆服务、知识库、对象存储等 → **内核只定协议接口,实现走外部**。
   既有形态即范本:`MessageStore`(R5,业务落 DB 自实现)、`MemoryStore`(K3,向量库/RAG 后端是 userspace)、`TelemetrySink`(R3,后端不绑定)。**禁止**在 src/ 内置任何具体后端。
4. **以上皆否**(仅"别家有"/纯产品功能,如 PTY exec、checkpoint、turn-diff、持久化 todo、多清单管理)→ **不做**,判 userspace,业务侧自建。

辅助判据:taifeng 有真实第一用户(求本 MDT 集成)。规则 1/2 存疑时,以「求本或可预见的嵌入方是否真的会用到」二次裁决;无人拉动的协议扩展(如 E2 可插拔 slot)一律挂起等需求。

### 三、`tool/builtins/` 的角色边界

builtins 是"随内核发货的可选电池货架"(全部 opt-in、不默认注册、零业务语义),收录标准 = 规则 2(模型认知原语:todo_write、request_user_input)或通用 IO 原语(file/shell/http)。**不**收录:带持久化的状态管理、面向最终用户的产品功能、特定外部服务的客户端。

## 后果

- 剩余 backlog 据此重审:spawn reject 分类细化(规则 1,可观测)与 A4 多模态驱逐(规则 1,等真实负载)保留;A5 低优先;E2/E3、拓扑路径寻址挂起等需求拉动;hermes 侧未对齐的产品级功能(持久化 todo 等)正式关闭。
- 后续每个 change 的 proposal SHALL 注明命中哪条规则;四条皆不命中的提案直接拒绝,不进 backlog。
- R1 红线不变,本 ADR 是其上层的立项裁决层(R1 管"代码里不许出现什么",本 ADR 管"什么值得进内核")。
