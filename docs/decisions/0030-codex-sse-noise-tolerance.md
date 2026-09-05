# ADR 0030：Codex SSE 对非协议帧改为容忍（Amends #0026）

- 状态：Accepted
- 日期：2026-09-05
- 修订：[ADR 0026](0026-independent-codex-provider.md) 的 SSE 严格性一条；wire 与验收细则见
  [Codex Responses Provider 能力契约](../architecture/capabilities/llm-codex-provider.md) §5.3。

## 背景

Codex accumulator 原本对**顶层 SSE `type` 未登记**的帧一律 `InvalidResponseError`，行解析层对空
`data:` 帧、非 JSON data、非 object data 同样硬失败。设计意图是 fail-closed：不靠跳过未知事件掩盖
provider 协议漂移。

2026-09-05 一台中转网关开始在 `/responses` 流里注入自造心跳帧 `data: {"type":"keepalive", …}`。后果
是链式的，且每一环都"按设计工作"：

1. 心跳发在流最长的空窗——模型 reasoning、首个 output item 之前；
2. 累加器判 `unsupported Codex SSE event: keepalive`，整条流终止；
3. `InvalidResponseError` 继承 `InvalidRequestError`，`retryable=False`、
   `failure_class=invalid_request`，`ConservativeFailurePolicy` 判 TERMINAL——不重试、不挂起；
4. 编排器根 turn 每一轮都在同一位置崩，**一次工具都没调**，整条链路对上层表现为完全停摆。

即：一个与语义无关的链路噪声，被逐级放大成不可恢复故障。fail-closed 守错了边界——它防的是
**provider 的协议漂移**，而心跳根本不在 Codex Responses 协议里，是中间人塞进传输层的东西。

同一份白名单还漏登记了若干**上游确实存在**的 Responses 事件
（`response.reasoning_summary_part.added/.done`、`response.output_text.annotation.added`、
`response.queued`）。中转站哪天把上游透传放宽，同一姿势会再咬一次。

## 决策

**未登记 = 非协议 = 跳过，不阻断**。具体：

- 顶层 `type` 不在已登记全集 `_KNOWN_EVENTS` 内（含缺 `type`、`type` 非字符串）→ 记账后跳过。
  该判定**排在 completed 闸门之前**：心跳同样会落在 `response.completed` 与 EOF 之间的空窗。
- 行解析层的空 `data:`、非 JSON、非 object → 同样记账后跳过（这三种恰是网关心跳的常见形状）。
- **不静默**：`NoiseLedger` 按 label 计数，同一 label 每个 attempt `logger.warning` 一次
  （心跳可达上百帧，不能刷屏）。计数对调用方与测试可读。

**仍然 fail-closed 的是协议内的违规**，本次一寸不放：显式失败终态
（`response.failed` / `response.incomplete` / `error`）、身份漂移、配对缺失、索引不连续、
delta 与 done 不逐字节一致、completed 后再来**协议**事件、以及 `finalize()` 的全部终态校验。

判据是：**终态保证由 `finalize()` 守，不由逐行严格性守**。输出事实源仍是 done items + completed
完成门；噪声若真吞掉了内容，`finalize()` 必然失败。逐行拒绝是冗余的第二道闸，而它的代价是把第三方
链路噪声升格成不可恢复故障——这笔交换不划算。

## 后果

### 正向

- 中转网关注入心跳 / 计费标记 / 路由探针不再让 turn 停摆，链路噪声不升格为故障。
- 顺带免疫上游 Responses 事件集扩张（新增事件被跳过，而非崩流）。
- 噪声可观测：日志点名 label，运维能看出"我的中转站在往流里塞东西"。

### 代价

- provider 若真的换用新事件承载**输出正文**，本层不再第一时间报错，改由 `finalize()` 在终态校验时
  暴露——错误位置更靠后、信息更弱（表现为"缺 done item"而不是"出现未知事件 X"）。噪声日志是找回
  这段信息的入口。
- `_parse_codex_sse_line` 签名多一个 `NoiseLedger` 参数，两层共用一本账。

## 被否决方案

1. **只放行心跳词表** `{ping, keepalive, heartbeat}`：下一台网关换个词就再崩一次，把运维问题写死进内核。
2. **只放行非 `response.` 前缀**：能挡住本次事故，但挡不住白名单漏登记的标准 `response.*` 事件，
   两类未知需要两套规则，边界更难解释。
3. **补全 Responses 事件全集**：治不了自造帧（`keepalive` 永远不在任何官方全集里），且要求内核追着
   上游事件表跑。
4. **要求中转站停发心跳 / 换直连**：把内核的健壮性押在第三方配置上；且心跳本身是合理的反代行为
   （防中间设备掐长连接），不该由它让步。

## 验证

- `tests/llm/test_codex_sse_noise.py`：心跳插在流中 6 个位置（含 completed 之后）均不阻断；
  未登记 / 畸形帧 9 种形状全部记账跳过；协议内违规 5 类仍硬失败；行解析层 12 行分档断言。
- 真实回归：`docs/real-llm-ledger.md`（基础层 `src/taifeng/llm/` 变更红线）。
