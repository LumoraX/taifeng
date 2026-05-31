# Real LLM 端到端验证报告

> 日期：2026-05-23
> Provider：OpenAI-compat gateway (lumorax.ai) → Gemini 3.1 Pro Preview
> 配置：`api/.env` 中的 `LLM_BOOTSTRAP_OPENAI_*` 三件套

## 测试矩阵

| 场景 | 脚本 | 结果 |
| --- | --- | --- |
| Skill 加载 + 单 entry + read_skill + 多轮 | `examples/real_llm/e2e.py` | ✅ 完整跑通 |
| Composite skill + call_skill 递归派发 | `examples/real_llm/composite.py` | ✅ LLM 主动调 call_skill，子 turn 独立 LLM 调用 |
| Hooks (PreToolUse) 真实拦截 | `examples/real_llm/with_hooks.py` | ✅ hook 拦截 1 次，arguments 透传正确 |

## 发现的问题

### 🐛 BUG-1: TurnRunner 硬编码 `gpt-4o-mini` fallback（已修）

`src/taifeng/loop/turn.py:207` 之前：
```python
model=self.entry_skill.model or "gpt-4o-mini",
```

这会**覆盖业务侧通过 `LiteLLMClient(model=...)` / `OpenAICompatClient(model=...)` 配置的 default_model**，导致 SKILL.md 没声明 `model` 字段时回退到一个**完全脱离配置**的硬编码默认值。

修复：改为空字符串，让 provider 通过 `req.model or self._model` 自动 fall back。
```python
model=self.entry_skill.model or "",
```

**触发条件**：SKILL.md 没有 `model:` 字段 + 用 OpenAICompatClient / LiteLLMClient + provider 不支持 `gpt-4o-mini` 的网关 → 直接 `model_not_found` 报错。

### ✅ 验证通过的能力

1. **OpenAI-compat SSE 流式解析**：`text_delta` / `tool_call_delta` / `tool_call_done` / `completed` 顺序正确
2. **tool_call 完整体重组**：流式 arguments 分片拼接 + JSON 解析无问题
3. **TurnRunner 二轮循环**：第一轮 LLM 决定调工具 → 工具返回 → 第二轮 LLM 出最终答复
4. **JSONL 持久化**：用户消息 / 助手消息 / function_call / function_call_output 全部按序入盘
5. **DispatchPolicy 派发校验**：composite skill 通过白名单 + depth 检查后启动子 TurnRunner
6. **子 turn 独立 LLM 调用**：嵌套调用真实工作，结果回流父 turn
7. **SkillDispatched / SkillReturned 事件**：完整触发并被 ConsoleSink 渲染
8. **PreToolUse Hook 拦截**：每次工具调用前精确触发，可拒绝可放行

### ⚠️ 限制 / 已知约束

- **Cache hit rate = 0%**：本测试网关（lumorax）目前未在响应里返回 `prompt_tokens_details.cached_tokens`，
  所以 `PromptCacheStats` 显示无命中。这是 provider/gateway 限制，不是 taifeng bug。
  Anthropic 原生 / OpenAI 原生 / 自建 vLLM 通常会返回。

- **SKILL.md model 字段**：未声明时 fallback 到 client default 是预期行为。
  如果业务需要不同 skill 用不同模型，必须在 SKILL.md 显式声明 `model:`。

- **大模型可能不调 call_skill**：composite skill 派发依赖 LLM 的 tool-use 训练程度。
  弱模型可能直接自己写答案。SKILL.md 里**强势措辞**（"必须"、"不要自己"）有效；
  必要时配合 PreTurn 钩子做强制路由。

## 性能数据（lumorax gemini-3.1-pro-preview）

| 场景 | iter | 总耗时 | input_tokens | output_tokens |
| --- | --- | --- | --- | --- |
| 单轮 + read_skill | 2 | 10.3s | 1454 | 739 |
| 单轮无 tool | 1 | 9.3s | 957 | 757 |
| 子 skill 单 LLM 调用 | 1 | 8.1s | 466 | 793 |

延迟主要在网关 + LLM 推理；taifeng 引擎本身开销 < 100ms。

## 复现步骤

```bash
cd taifeng

# 确保 api/.env 含三件套
grep '^LLM_BOOTSTRAP_OPENAI_' ../api/.env

PYTHONPATH=src uv run python examples/real_llm/e2e.py
PYTHONPATH=src uv run python examples/real_llm/composite.py
PYTHONPATH=src uv run python examples/real_llm/with_hooks.py
```

## 结论

**Taifeng M1–M5 引擎在真实 LLM 链路上完全可用**。BUG-1 已修复，所有其他设计与实现行为符合预期。
唯一需注意的是**业务侧推荐显式给 entry skill 声明 model 字段**，避免依赖 client default。
