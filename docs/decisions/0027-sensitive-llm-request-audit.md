# ADR 0027：敏感 LLM Request 使用安全投影与全量 Digest 审计

- 状态：Accepted
- 日期：2026-08-28
- Amends：ADR 0025（仅修订敏感 LLM request intent 的 payload 形状）

> 当前字段级约束见
> [Codex Responses Provider 能力契约](../architecture/capabilities/llm-codex-provider.md) §6。

## 背景

ADR 0025 要求 `llm_request_committed` 保存实发 request，并要求原始 Journal 保存完整 payload 或不可变加密
blob 引用。图片输入与 Responses reasoning state 引入了两类不应复制到额外 durable sink 的正文：图片
base64/Data URL 和 `encrypted_content`。

canonical `MessageStore` / `SessionJournal` conversation item 和最终 response checkpoint 已经承担会话恢复
事实源；再把相同正文复制进 request intent、request capture、日志或 telemetry 会扩大泄漏面。当前内核也不
提供独立的加密 blob store、密钥轮换和销毁协议，不能把一个未实现的 blob 系统写成现状。

## 决策

对含敏感字段的 LLM request，`llm_request_committed` 不保存可反解的敏感正文，改为保存：

1. 与原 request 结构同形的安全投影：图片仅保留 media type、size、SHA-256、detail descriptor；
   `encrypted_content` 键和值删除并留下 redaction marker。
2. redaction manifest：使用 RFC 6901 JSON Pointer 列出被删除字段，按 path 的 UTF-8 bytes 排序、不重复，
   kind 只允许 `image_base64` 与 `provider_encrypted_content`。
3. 对 `provider + model + 脱敏前 ApiRequest.model_dump(mode="json")` 的 RFC 8785 canonical bytes 计算
   SHA-256；它绑定 provider-neutral attempt intent，不是最终 wire-body digest。原文字节不得先写旁路文件。

Provider wire 的 Data URL 是 canonical `ImagePart` 的确定性临时投影；唯一事实源仍是授权的 canonical
conversation/provider-state store。attempt observer、普通 request capture、OTel、日志和 debug repr 都只允许
取得安全投影，不得取得敏感原文。

这项修订只缩窄 ADR 0025 的敏感 request intent 内容，不改变以下规则：

- SessionJournal 仍是 strict Session 的唯一可靠事实源；
- user/assistant 正文、canonical 图片和 verified provider state 可在授权 recovery store 中完整保存；
- response checkpoint 可保存恢复所需的 verified `encrypted_content`；
- Journal hash chain、definite ack、checkpoint-before-delta、freeze/UNKNOWN 语义保持不变。

## 后果

### 正向

- request intent 能证明某个 canonical dispatch 意图已 durable 提交；关联 checkpoint 只证明 attempt 已进入
  受审计 client 执行阶段并形成 durable 已知/未知终态，不证明请求字节实际离开进程，同时不会复制图片正文
  或 reasoning ciphertext。
- 不需要在本阶段引入密钥管理和加密 blob 生命周期。
- request capture、strict observer 与 telemetry 可以复用同一 fail-closed redactor。

### 代价

- 仅凭 request intent 不能独立重建完整 request；恢复依赖授权的 canonical conversation/provider-state store。
- redactor、manifest 和 digest 必须有 canonical JSON conformance 测试；新增敏感字段若未登记必须 fail closed，
  不能默认为普通值透传。

## 被否决方案

1. 在 request intent 中重复保存图片 base64 与 `encrypted_content`：扩大泄漏面，否决。
2. 只保存脱敏 request、不保存 digest/manifest：无法稳定关联实发 request，否决。
3. 本阶段新增加密 blob store：缺少密钥、保留期、销毁和恢复协议，超出本变更范围，否决。
