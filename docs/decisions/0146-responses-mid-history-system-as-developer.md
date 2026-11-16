# ADR 0146：OpenAI Responses 把历史中段 system 消息原位改用 developer 角色

- 状态：Accepted
- 日期：2026-11-16
- 修订：[ADR 0055](0055-mid-history-system-as-tagged-user.md) 决策 3 中「OpenAI 系原样透传中段 system」——对 OpenAI Responses 客户端不再成立；Chat / openai_compat / DeepSeek / LiteLLM 不变
- 关联：[capabilities/llm-provider-native.md § mid-history-system](../architecture/capabilities/llm-provider-native.md)；[llm-client.md](../architecture/llm-client.md)

## 背景

2026-11-16 以 `provider=openai / protocol=responses` 跑真实能力矩阵，`budget_awareness`、`pinned_periodic`、
`compaction_continuity` 三个场景失败，同一端点上以 codex provider 跑均通过。三者共同点是在历史中段注入
system 消息（预算提示 / pinned 重注 / 压缩摘要），端点返回 HTTP 400 `System messages are not allowed`。

`OpenAIResponsesClient` 早已把顶层 `system_prompt` 改用 `developer` 角色下发，理由正是这类端点拒收 system；
但中段 system item 仍经 `_input_item` 原样透传 `role="system"`。处置只做了一半：没有注入时请求正常，一旦压缩 /
预算提示 / pinned 重注触发，整轮就失败。

## 决策

1. **OpenAI Responses 客户端把中段 `role="system"` 的 message item 原位改为 `role="developer"`**，内容与位置不变。
   Responses 协议中 developer 是 system 的后继角色，语义等价；官方端点两者都接受。
2. **不改写为 `<system-reminder>` user 文本**（Codex / Anthropic / Gemini 的做法）：Responses 原生支持中段
   developer 消息，没有必要降级为 user 角色，也就不会混淆系统注记与用户原话。
3. **只改 Responses 客户端**：OpenAI Chat / openai_compat 的 wire 形状与审计摘要保持不变（Chat 下的 developer 支持
   因端点而异，本次没有证据需要改）。
4. cache 影响（R2）：注记保持原位，前缀不变；每条注记只是角色字段不同，不引入新的 cache 失效。

## 验证

- 单测 `tests/llm/test_openai_responses.py::test_responses_mid_history_system_uses_developer_role_in_place`：修复前红、修复后绿。
- 真实回归：同一端点以 openai/responses 全量重跑 `capability_matrix.py`，三个场景转为 PASS（见台账）。
