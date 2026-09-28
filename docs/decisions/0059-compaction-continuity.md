# ADR 0059：压缩后的衔接——保留最近用户原话 + 续接前言 + 摘要新分段

- 状态：Accepted
- 日期：2026-09-28
- 关联：[context-compression.md § 压缩条目的组装](../architecture/context-compression.md)；ADR 0004（cache-aware 压缩）/ 0055（中段 system 渲染）

## 背景

2026-09-28 能力侧 review 发现三处衔接缺陷：

1. 被压缩区间里的用户消息一并交给 LLM 转述。用户的原始意图与约束（「写三段」「不要改公共 API」）经转述最易走样，
   压缩后再也看不到原话。codex 会把最近约 20k token 的用户消息原样放回历史。
2. `context-compression.md` 写了续接提示语（「直接继续，不要确认收到摘要」），代码里没有：摘要以
   `[Compacted history summary]` 直接插入，模型常先复述进度再开工。
3. 摘要分段缺「压缩前最后在做什么」与「踩过的坑」，压缩后易重复已修过的错误。

## 决策

1. 压缩条目正文 = 英文续接前言 + `<recent_user_messages>`（被压缩区间最近用户消息原文，默认 20k token 预算，最新一条
   超预算则中段截断）+ `<summary>`（LLM 摘要）。
2. **全部放进同一个 `compacted` item**，而不是像 codex 那样另插 user 条目：冷加载的 `replaced_range` 折叠、token 估算、
   provider 中段 system 渲染（ADR 0055）都无需改动，压缩仍是「一个区间 → 一个条目」。
3. 摘要提示词新增「当前工作」「错误与修复」两段，并纳入非致命分段检查。
4. 质量审计改为审组装后的正文：原话里原样保留的标识符不算丢失。
5. `HandoffCompactionStrategy(preserve_user_message_tokens=)` 可调，0 关闭原话保留。

## 否决的方案

- **把用户原话作为独立 user item 放回历史**：冷加载重建要额外记录「哪些 item 被复制回来」，`replaced_range` 语义被打破；
  且中段出现的真实 user 角色消息会被模型当成新请求重复执行。
- **续接前言用中文**：会把非中文对话的回复语言带偏；摘要本身按原对话语言生成。

## 影响

- R2：只改变压缩条目正文，压缩的 anchor / cache 语义不变。R1 / R3–R5 无变化。
- 压缩条目比以前长（最多多约 20k token 的原话）；需要更紧凑的业务可调小预算或设 0。

## 验证

`tests/context/test_handoff_continuity.py`（10 例）：预算内按序选取、最新一条超预算截断、预算 0 关闭、跳过空白与非用户条目、
组装布局、无原话时不出现该段、提示词含新分段、压缩条目含区间原话且不重复收录保留段、原话中的标识符不触发重生成。
