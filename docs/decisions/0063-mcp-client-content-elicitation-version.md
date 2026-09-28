# ADR 0063：MCP 客户端结果无损投影、elicitation 注入口与 2025-06-18 版本协商

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/mcp-client.md](../architecture/capabilities/mcp-client.md)；
  [tool-image-attachment.md § MCP 桥接工具](../architecture/capabilities/tool-image-attachment.md)；
  [mcp-server.md § initialize](../architecture/capabilities/mcp-server.md)；ADR 0048（MCP 绑定 / HTTP 传输）/
  0057（MCP 工具副作用分类）

## 背景

taifeng 作为 MCP 客户端有三处与规范（2025-06-18）脱节：

1. **结果投影有损**。`extract_text_content` 把 image 降级成 `[image: mime]`、整个丢弃 `structuredContent`、
   把其余内容块 `json.dumps` 进文本（base64 正文随之进模型上下文）。内核早有工具图片附件机制
   （`ToolResult.attachments`），MCP 工具却用不上；只返回结构化结果的工具，模型读到的是空字符串。
2. **server → client 请求无人应答**。两种传输只认 `notifications/tools/list_changed`；server 发来的
   `elicitation/create`、`ping` 等带 id 请求记 debug 后丢弃，server 只能干等超时。stdio 读循环先按 id 匹配本端
   请求，server 请求与本端请求撞号时被误当成响应、错误结算本端调用。
3. **版本各写各的**。stdio 声明 `2024-11-05`、HTTP 声明 `2025-03-26`、server 恒回 `2024-11-05`；客户端从不
   校验 server 回的版本。elicitation 自 2025-06-18 才进规范，不对齐就无法合规声明该能力。

## 决策

1. **版本单一来源**（`mcp/protocol.py`）。客户端恒声明 `LATEST_PROTOCOL_VERSION = "2025-06-18"`；server 回的版本
   须在 `SUPPORTED_PROTOCOL_VERSIONS = (2025-06-18, 2025-03-26, 2024-11-05)` 内，否则（含缺失）关闭连接并抛
   `McpProtocolVersionError`（`McpToolError` 子类，code -32602）。HTTP 协商后每个请求带
   `MCP-Protocol-Version: <协商版本>`，initialize 本身不带。server 侧请求版本受支持原样回，否则回最新版。
   支持清单保留旧版的理由：本客户端用到的协议面（initialize / tools / list_changed / ping / cancelled）在三版间
   线上兼容，2025-06-18 的新东西（elicitation、structuredContent、resource_link）都是增量——旧 server 只是
   不会发，协商到旧版不会让任何已实现能力失真。
2. **结果按类型投影**（`mcp/content.py`）。image → `ImageAttachmentV1` 进附件，与业务取图工具同走落盘前
   admission；structuredContent → `ToolResult.data["structured_content"]`，文本侧缺与之 JSON 等价的 text 块时
   补序列化 JSON；text 资源标注 uri 内联；blob / audio / resource_link / 未知类型给带 MIME 与字节数的显式占位，
   base64 不进文本；形状非法 → 该次调用判错（`mcp_invalid_content`）。新增 `attach_images`（默认 True）。
   `extract_text_content` 保留为同规则的纯文本投影。
3. **server → client 路由器**（`mcp/server_messages.py`，两种传输共用）。先看 `method` 再按 id 结算（修撞号）；
   `ping` 回空 result；`elicitation/create` 交宿主注入的 `ElicitationHandler`，未注入回 -32601；其余请求 -32601；
   `notifications/cancelled` 取消在飞应答且不回响应；一切以 task 调度，`close()` 统一取消。
4. **elicitation 注入口**（`mcp/elicitation.py`）。`ElicitationHandler` 为异步 Protocol（可直接传 async 函数），
   入参 `ElicitationRequest{message, requested_schema, server_info}`，返回
   `ElicitationResult{action ∈ accept/decline/cancel, content?}`（构造期校验）。注入才声明
   `capabilities.elicitation`。handler 抛异常 → -32603 且只回异常类型名；accept 的 content 先按
   `requestedSchema` 子集校验。内核不设 handler 超时。
5. `McpPrompter`（taifeng 作为 server 发 elicitation）认定稿的 `decline`；草案期的 `reject` 继续兼容。

## 否决的方案

- **只接受 2025-06-18，其余一律断开**：大量已部署 server 仍回 2024-11-05 / 2025-03-26，本客户端对它们没有任何
  协议不兼容；一刀切只会让既有绑定（含 `examples/mcp_showcase`）全部建连失败，而换不来任何正确性。
- **版本不在清单内时「尽量继续」**：规范说客户端不支持时 SHOULD 断开；继续意味着以未知语义解释对端消息，
  失败会在更远处以更难归因的形式出现。
- **`attach_images` 默认 False**：兼容性好，但开启了 `ImageInputPolicy` 的宿主还得再记得翻一个开关，漏翻时模型
  静默看不见图（只剩占位）。默认附件让「策略没开」以 `tool_attachment_rejected` 在模型与日志里如实暴露——与
  tool-image-attachment 契约「准入期失败如实报」一致；不看图的宿主一个参数显式降级。
- **structuredContent 取代 content 进模型视野**（codex 的做法）：会丢掉同一结果里的图片与人类可读摘要；taifeng
  两者都保留，只在 text 块缺等价 JSON 时补上。
- **structuredContent 无条件追加 JSON**：遵守规范 SHOULD 的 server 已把同一 JSON 放进 text 块，无条件追加会让模型
  读两遍、白占上下文。
- **内核给 handler 设默认超时**：规范把请求超时与取消通知归于请求发送方（server）；客户端再加一层会与 server 的
  时限相互截断，且「超时后回什么动作」没有语义正确的答案（cancel 表示用户关了对话框，并未发生）。宿主要时限可在
  handler 内自行限时。
- **handler 异常把消息原文回传 server**：异常文本可能含宿主内部信息，server 未必可信；原文只进本地日志。
- **未注入 handler 时静默忽略 elicitation 请求**：server 会等到超时才知道没人应答；-32601 立即告知且符合
  「未声明的能力」语义。

## 验证

`tests/mcp/test_protocol.py`（协商接受 / 拒绝 / 缺失、stdio 断开子进程、HTTP 版本头与会话结束、server 协商）、
`tests/mcp/test_content.py`（各内容类型、非法形状、structuredContent 去重与补全、桥 handler、经 loop admission
落 fco 的策略启用 / 未启用两侧）、`tests/mcp/test_elicitation.py`（stdio 与 HTTP 两种传输：能力声明、accept /
decline、未注入 -32601、handler 抛错、撞号、ping、未知方法、server 取消、close 打断）、
`tests/mcp/test_server_messages.py`（路由器单测）、`tests/mcp/test_prompter.py`（decline）。
`scripts/verify_examples.py` 中 `mcp_showcase`（server 回 2024-11-05）照常通过。

已知未做：`outputSchema` 校验、本端超时 / 取消时发 `notifications/cancelled`、sampling / roots，见契约「能力边界」。
