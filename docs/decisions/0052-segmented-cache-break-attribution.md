# ADR 0052：cache 失效分段归因（模型 / 已缓存消息前缀 / 工具 schema）

- 状态：Accepted
- 日期：2026-09-28
- 关联：[context-compression § Cache break](../architecture/context-compression.md)；ADR 0036（cache anchor 真相）

## 背景

结构指纹只有 skill 快照 / 工具名 / system 三段。模型切换、同名工具 schema 变化、已缓存前缀被
rollback 等非压缩路径改写时，cache 失效只能落到 `unknown_drop`——把可解释的失效当成 bug 信号，
淹没真正的异常。参照 claw-code `prompt_cache.rs` 的分段指纹。

## 决策

1. 工具段含描述与 schema（同名替换可识别）。
2. 新增 `model` 段与消息前缀段（发出时长度 + id 序列哈希）；前缀只比较上次发出的那一段，尾部增长不算变化。
3. 用 item id 而非内容哈希：内核改写内容的路径（压缩 / 剪枝）都换新 id 或走预期标记；按 id 比对是 O(n)
   字符串拼接，避免每轮对全量 payload 做序列化哈希。
4. 判定顺序：预期标记（压缩 / rewind）> snapshot > tools > system > model > 前缀；旧版本指纹缺新键时不误判。
