# ADR 0011: 空 api_key 省略鉴权头（对齐 OpenAI SDK），授权失败由服务端 401 裁决

- 状态：Accepted
- 日期：2026-06-01
- Related: spec `docs/architecture/capabilities/llm-provider-native.md`（Requirement「空 api_key 时省略鉴权头/参数」）

## 背景

native LLM client（`OpenAICompatClient` / `AnthropicClient` / `GeminiClient` / `DeepSeekClient`）
此前对 `api_key` 一律拼进鉴权头，例如 OpenAI-compat 走 `Authorization: Bearer {api_key}`。

**实际事故**：业务侧用 web_ui demo 在**未配置 key** 的情况下连默认 OpenAI 端点，httpx 在请求
构造阶段直接抛 `LocalProtocolError: Illegal header value b'Bearer '` —— 因为空 key 拼出的
`"Bearer "`（带尾空格）是非法 header 值。该异常不属于任何 `LLMError` 子类，被
`classify_failure` 兜底成 `failure_class=unknown`：**报错隐晦、不可重试、且把"没配 key"
误导成"未知内核错误"**（声明式编排 demo 因子 skill 全挂而级联 `OrchestrationConditionError`，
更掩盖了真正的根因）。

同时存在一类**合法的无鉴权端点**：本地 Ollama / LM Studio / vLLM 等 OpenAI 兼容服务**不需要
key**。旧实现强行拼 `Bearer ` 反而让这些端点无法直连。

调研官方 **OpenAI Python SDK（2.24.0）**的 `auth_headers`：

```python
api_key = self.api_key
if not api_key:
    # if the api key is an empty string, encoding the header will fail
    return {}
return {"Authorization": f"Bearer {api_key}"}
```

SDK **早已踩过同一个坑并修复**：空 key 直接**省略整个 Authorization 头**（注释明写"空字符串
会导致 header 编码失败"）。即 taifeng 旧实现比官方 SDK 还脆。

## 决策

native client 统一规范如下：

1. **接受空/空白 api_key**：当 `api_key.strip()` 为空时，**省略**对应鉴权头/参数，而不是发出
   语义为空的凭据。各 provider 的省略点：
   - `OpenAICompatClient` / `DeepSeekClient`：省略 `Authorization` 头
   - `AnthropicClient`：省略 `x-api-key` 头
   - `GeminiClient`（`auth_via="header"`）：省略 `x-goog-api-key` 头
   - `GeminiClient`（`auth_via="query"`）：URL 不挂 `&key=`
2. **授权由服务端裁决，不在 client 侧预判**：client 不做"key 是否有效"的本地判断。
   - 无鉴权端点（Ollama 等）：没有鉴权头 → 正常工作。
   - 需鉴权端点：缺鉴权头 → 服务端返回 **401** → `classify_http_error` 归为
     `AuthenticationError`（`failure_class=provider_auth`，`suggested_action="检查 API key"`）。
3. **`extra_headers` 在鉴权头之后合并**：即便 `api_key` 为空，业务侧仍可经 `extra_headers`
   注入网关自定义鉴权头（不被空 key 逻辑覆盖）。

这与 OpenAI SDK `auth_headers` 同语义 —— **不是自创兜底，而是对齐已被验证的成熟做法**。

## 后果

- ✅ 本地无鉴权端点（Ollama / LM Studio / vLLM）可**不配 key 直连**；也兼容 Ollama 官方
  推荐的 dummy key（`api_key="ollama"` → 照常发 `Bearer ollama`，服务端忽略）。
- ✅ 真实服务端无 key 时，从隐晦的 `LocalProtocolError → unknown` 升级为清晰的
  **401 → AuthenticationError**，处置建议明确、telemetry 分类准确（R3 可观测受益）。
- ✅ 与官方 SDK 行为一致，降低业务侧迁移/排查心智负担。
- ⚠️ **不再有"client 侧空 key 早失败"**：空 key + 需鉴权端点要到**发出请求**才暴露 401，
  而非构造期。这是刻意取舍 —— client 无从得知目标端点是否需鉴权（同一份代码既连 Ollama
  又连 OpenAI），授权裁决权属于服务端。业务侧若想"开跑前就提示没配 key"，应在**业务层**
  （如 web_ui 启动横幅）做，不下沉到 infra（R1）。

## R1–R5 影响

- **R1（业务零侵入）**：无影响。空 key 判断是纯传输层逻辑，不含任何业务概念。
- **R2 / R4 / R5**：无影响（不触及压缩 / cache / 取消 / resume）。
- **R3（可观测）**：正向改善 —— 把 `unknown` 失败收敛为 `provider_auth`，failure_class 更准。
