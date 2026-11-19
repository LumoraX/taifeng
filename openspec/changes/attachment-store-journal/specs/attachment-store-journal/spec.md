## ADDED Requirements

### Requirement: Journal 附件引用记录
严格审计且配置了附件存储时，`submission_accepted` 与 `function_call_output` 对话项中的附件 SHALL 以引用记录落账：含 `ref`、`media_type`、`size`、`sha256` 与已校验元数据，SHALL NOT 含正文。未配置存储时 SHALL 沿用内联记录，行为不变。

#### Scenario: 带图提交落引用记录
- **WHEN** 审计会话配置了存储，用户提交一张通过 admission 的图片
- **THEN** Journal 中 SHALL NOT 出现该图片的 base64，对应记录 SHALL 含其 `sha256` 与引用

#### Scenario: 未配置存储
- **WHEN** 审计会话未配置存储
- **THEN** 记录形状与 hash SHALL 与本变更之前逐字节一致

### Requirement: hash 链承诺附件内容
引用记录的 `sha256` SHALL 在 `payload_hash` 的 preimage 之内。验证一条记录的 hash SHALL NOT 需要读取附件存储。

#### Scenario: 篡改记录里的摘要
- **WHEN** 引用记录的 `sha256` 被修改
- **THEN** hash 链校验 SHALL 失败

#### Scenario: 离线验链
- **WHEN** 在没有附件存储的环境里校验一个 session 的 hash 链
- **THEN** 校验 SHALL 能完成

### Requirement: 被拒绝的提交不留痕
未通过 admission 的带附件提交 SHALL 以 `submission_rejected` 落账且不保存正文，SHALL NOT 向附件存储写入对象。

#### Scenario: 超限附件
- **WHEN** 附件超过 `AuditConfig` 的字节上限
- **THEN** 存储的 `put` SHALL NOT 被调用，Journal 只有拒绝记录

### Requirement: 回放一致
回放 SHALL 经附件存储取回字节并核对 `sha256` 与 `size`；`canonical_attempt_digest` 的 preimage SHALL 与内联形态相同，使同一对话的内联录制与引用录制摘要相等。

#### Scenario: 引用录制的回放
- **WHEN** 回放一个以引用形态录制的带图 session，附件存储完好
- **THEN** 每次请求 SHALL 匹配录制摘要，回放 SHALL 无分叉

#### Scenario: 回放时对象缺失
- **WHEN** 回放时某引用的对象已不在存储中
- **THEN** SHALL 报 `ReplayDivergenceError`，SHALL NOT 以占位文本继续

#### Scenario: 回放时对象被篡改
- **WHEN** 取回的字节摘要与记录不符
- **THEN** SHALL 报 `ReplayDivergenceError`

### Requirement: 在线运行时的降级留痕
非回放的审计 turn 中，历史附件取不回时 SHALL 沿用占位文本降级，并 SHALL 在 Journal 中记录该事实（引用、原因），使之后的回放能够重现同一请求。

#### Scenario: 降级后的回放
- **WHEN** 某审计 turn 因对象缺失而降级，之后回放该 session
- **THEN** 回放 SHALL 以同样的占位文本构建该次请求并匹配录制摘要

### Requirement: 版本信号
含引用记录的 session SHALL 带有读取方可识别的版本或能力信号；不支持该信号的读取方 SHALL 明确拒绝该 session，SHALL NOT 把引用记录当作损坏记录跳过。

#### Scenario: 旧读取方
- **WHEN** 不支持引用记录的读取方打开含引用记录的 session
- **THEN** SHALL 以「格式不支持」类错误拒绝，且该错误与数据损坏可区分
