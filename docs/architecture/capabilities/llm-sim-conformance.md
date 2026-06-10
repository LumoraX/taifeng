# Capability: llm-sim-conformance

## Purpose

有状态 LLM conformance 模拟器（`src/taifeng/llm/providers/sim/`）——测试基础设施。替代旧开环复读机 mock：像真实服务端一样**先审请求（协议合同）、再记账（token / 前缀 cache）、最后按剧本作答**，使 resume / rewind / call_id 配对 / 重放复读 / 并发时序 / overflow 自愈类 bug 在日常 mock 回归中直接测红。

> 参照 codex `core/tests/common/responses.rs` 的请求侦察断言面；差异：codex 在 wiremock 线缆层记录 HTTP JSON，本实现于 ModelClient 协议层强类型记录 `ApiRequest`。

## 数据契约

| 结构 | 模块 | 要点 |
| --- | --- | --- |
| `SimTurn` | `sim/script.py` | 单 turn 剧本；字段名兼容旧 MockTurn（text / reasoning / tool_calls / usage / delay_seconds / structured / cache_read / cache_creation / request_id；reasoning 非空时在 text 前回放 `reasoning_delta`）+ 新增 `finish` / `expect` / `fault` / `await_signal` / `emit_signal` |
| `SimFault` | `sim/script.py` | 故障注入四变体（工厂构造）：`rate_limit(retry_after)` / `server_error()` / `malformed_arguments()` / `truncate_stream(after_events)` |
| `SimExpect` | `sim/script.py` | 逐 turn 请求断言：`must_contain` / `must_include_output_for` / 消息数上下界 / 自定义谓词 |
| `SimScriptExhausted` | `sim/script.py` | 脚本耗尽异常（普通 Exception；**不入 LLMError**） |
| `SimContractViolation` | `sim/contract.py` | 合同违规异常，带机读 `rule` 标识；**不入 LLMError**（防被 retry 路径消化） |
| `RequestLedger` / `RecordedRequest` | `sim/server.py` | 请求侦察台账：`requests()` / `last_request()` / `single_request()` / `saw_function_call(call_id)` / `function_call_output_text(call_id)` / `message_texts(role)` / `system_texts()` / `tool_names()` / `blob()` / `violations` |
| `SimServerState` | `sim/server.py` | token 记账（chars//4 确定性估算）+ `context_window` 超窗抛 `ContextOverflowError` + 近 32 条环形前缀 cache 账本；`last_cache_read/creation` 测试可观测 |
| `SimClient` / `RoutingSimClient` | `sim/client.py` | ModelClient 实现：顺序回放 / 标记路由回放（每标记独立游标，无命中抛 KeyError） |
| `SimCoordinator` | `sim/client.py` | 信号量协调器：`wait(name)` / `signal(name)`，确定性编排并发完成顺序 |

## 行为契约（要点）

1. **协议合同校验**（每次 `stream(request)`，违规抛 `SimContractViolation` 并记入 `ledger.violations` 双保险）：
   - call_id 恰好配对（不得重复声明 / 重复核销 / 悬空输出）；**采样时刻（messages 末尾）不得有未核销 id**；
   - 未核销期间不得出现 user 消息；messages 非空；
   - 合法结构显式放行：中段 system（compacted / system_injection）、并行 fan-out「多条 assistant 声明在前、输出交错核销在后」；
   - **响应侧反查**：剧本要吐的 tool_call name 必须在请求 `tools` 中（抓「工具没注册进请求」）。
2. **记账先于选剧本**：被拒采样（overflow）不消耗剧本游标（真实服务端拒绝请求时没有产出回答）。
3. **strict 耗尽**：剧本耗尽抛 `SimScriptExhausted`，绝不静默吐空 turn。
4. **前缀 cache 账本**：自动产 `prompt_cache(cache_read=最长公共前缀, cache_creation=余量)`；前缀漂移不是错误、如实反映——R2（resume 重建一致性 / 压缩动 head 的 cache 失效）可量化断言。`SimTurn.cache_read` 显式赋值覆写。
5. **全保真流式**：tool_call arguments 16-char `tool_call_delta` 分片（首片带 name）+ `tool_call_done` 收尾；`chunked_tool_calls=False` 可退化。`finish="content_filter"` 镜像真实 provider 抛 `ContentFilterError`。
6. **故障注入**：rate_limit / server_error 在产出任何事件前抛；truncate_stream 产满 N 个事件后终止且无 completed。
7. **时序编排**：`await_signal` 开吐前等待、`emit_signal` completed 前点亮；确定性零随机。

## 测试接入

- pytest fixture `sim_client`（`tests/conftest.py`）：工厂构造 + teardown 自动断言 `ledger.violations == []`（异常被引擎兜底吞掉时仍能红）。
- 单测：`tests/llm/test_sim_*.py`（script / contract / ledger / server / client / routing / timing）；引擎链路集成：`tests/llm/test_sim_engine_integration.py`。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| R1 | 纯测试基础设施，零业务概念 |
| R2 | 正向强化：前缀账本使 cache 行为首次可在 mock 测试量化断言（曾借此抓出 sliding/handoff 在 anchor=-1 时负索引切片的真实内核 bug） |
| R3 | 事件流与真实 provider 同构（prompt_cache 每 turn 必发），不绕过任何 EventMsg 路径 |
| R4 | 保留 `CancellationToken.raise_if_cancelled()` 检查点（事件循环逐个检查） |
| R5 | 正向强化：前缀单调性 + call_id 配对直接校验 resume / rewind 重建一致性 |

## 能力边界（如实记录）

- token 估算只承诺**相对单调**，不承诺与真实 tokenizer 绝对一致——测试用相对阈值；
- LLM 行为自由度（畸形语义、不按预期派发）模拟器本质测不到，由真实 key 回归兜底（见 capability-matrix「验证状态」）。
