# 资源治理与真实回归观测设计

## 目标

为 Taifeng 的短命 Responses 存储、真实模型回归台账和执行树用量观测补齐可执行的资源与状态契约，保持现有公共 API 和发布门禁兼容。

## 方案

### 短命存储

`EphemeralAtomicBatchMessageStore` 增加冻结的 `EphemeralStoreLimits` 配置，默认限制线程数、单线程消息数、实例总消息数和实例总字节数。存储维护线程级与实例级计数，在任何写入实际改变内存前完成容量检查；超限抛出带 `resource`、`limit`、`observed` 和 `requested` 的 `EphemeralStoreLimitError`。`close()` 同时清零计数。

消息大小按 canonical `model_dump_json()` 的 UTF-8 字节数计算，限制只作为内存保护口径，不改变消息内容。atomic batch 在提交前一次性校验，冲突和超限都不产生部分可见结果。默认上限足够覆盖普通短命执行，宿主可通过构造参数显式收紧。

### 真实回归台账

保留现有 `verdict=PASS|PART|FAIL`，新增可选 `status` 字段。写入时根据终态、缺事件和错误文本分类：`PASS`、`PARTIAL`、`TRANSIENT_FAILURE`、`CODE_FAILURE`。未执行验证继续写入 `not_executed`，状态为 `NOT_EXECUTED`。网络限流、服务过载、连接失败和超时等只归为瞬态失败；断言缺失、协议错误和内核异常归为代码失败。旧台账没有 `status` 时由 `verdict` 推导，确保可读和可重写。

### 执行用量完整性

`usage_complete` 继续采用严格语义：每个采样尝试都必须有有效 provider input/output usage 才为 true。执行更新和终态事件新增 `usage_attempts_started`、`usage_attempts_metered`、`usage_attempts_missing`，使 provider 重试、取消和缺 usage 能被区分。执行树投影仍只使用布尔完整度决定是否完整，不把估算值当作实测值。

## 错误与兼容

- 短命存储超限是显式异常，不静默驱逐、不丢旧消息。
- `ScenarioRecord` 的旧构造方式仍有效；`status` 缺失时按旧 `verdict` 推导。
- `usage_complete` 不放宽；新增字段只提供诊断事实，不改变 token 计费和预算。
- 不把真实 provider 的暂时过载写成代码失败，也不把未执行场景写成通过。

## 验证

- 存储测试覆盖线程、消息、字节三类上限，atomic batch 全有或全无，close 后计数归零。
- 台账测试覆盖旧记录兼容、瞬态错误、代码错误和未执行状态的渲染与 JSON。
- 执行观测测试覆盖正常采样、缺 usage、provider retry 和取消终态的计数事实。
- 通过全量 pytest、examples、ruff、mypy 和真实 Codex 回归后再合并。
