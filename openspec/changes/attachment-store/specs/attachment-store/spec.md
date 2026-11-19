## ADDED Requirements

### Requirement: 附件存储协议
系统 SHALL 提供 `AttachmentStore` 协议，含 `put(data, *, media_type, sha256) -> str`、`get(ref) -> bytes`、`exists(ref) -> bool`、`delete(ref) -> None` 四个可取消的异步方法。`put` SHALL 幂等：相同内容返回相同引用且不重写已有对象；返回即表示对象已 durable。返回的引用 SHALL 只含 `[A-Za-z0-9._:-]`，长度 1–200。

#### Scenario: 重复写入同一内容
- **WHEN** 对同一字节序列调用两次 `put`
- **THEN** 两次 SHALL 返回同一引用，且存储中只有一个对象

#### Scenario: 声明的摘要与内容不符
- **WHEN** `put` 收到的 `sha256` 与 `data` 的实际摘要不一致
- **THEN** SHALL 抛 `AttachmentIntegrityError`，且不写入对象

#### Scenario: 读取不存在的引用
- **WHEN** 对从未写入的引用调用 `get`
- **THEN** SHALL 抛 `AttachmentNotFoundError`

#### Scenario: 删除不存在的引用
- **WHEN** 对不存在的引用调用 `delete`
- **THEN** SHALL 正常返回（noop）

### Requirement: 本机实现
系统 SHALL 提供 `LocalAttachmentStore(root_dir)`，对象路径由内容摘要决定，写入 SHALL 先写临时文件再原子替换；并发写入同一内容 SHALL 不产生损坏或残留的临时文件。

#### Scenario: 并发写入同一内容
- **WHEN** 多个任务同时 `put` 相同字节
- **THEN** 全部 SHALL 成功并返回同一引用，`get` 读回的字节与原件一致

#### Scenario: 写入中途被取消
- **WHEN** `put` 在写入完成前被取消
- **THEN** 目标对象 SHALL 不存在或完整存在，SHALL NOT 留下不完整的对象

### Requirement: 引用形态
系统 SHALL 提供 `ImageAttachmentRefV1` 与 `FileAttachmentRefV1`：冻结、`extra="forbid"`、`encoding="store"`、不含正文，携带 `ref`、`media_type`、`size`、`sha256`，图片另有 `width` / `height` / `detail`，文件另有 `pages`（可为 None）/ `filename`。条目的 `attachments` 列表 SHALL 允许内联形态（`encoding="base64"`）与引用形态并存，判别字段为 `encoding`。

#### Scenario: 引用形态不接受正文
- **WHEN** 以 `encoding="store"` 构造引用并附带 `content` 字段
- **THEN** 校验 SHALL 失败

#### Scenario: 引用含非法字符
- **WHEN** `ref` 含空白、路径分隔符或控制字符
- **THEN** 校验 SHALL 失败

### Requirement: 注入与装配
`EnginePool.create` SHALL 接受 `attachment_store`，并经 `pool.attachments` 暴露给宿主。插件 SHALL 能经 `provide_backends` 提供 `attachment_store`，遵循既有的单一提供者、冲突报错与冻结规则。`audit` 与 `attachment_store` 同时给出时 SHALL 在构造期抛 `ValueError`。

#### Scenario: 未注入
- **WHEN** 未提供 `attachment_store`
- **THEN** 附件的持久化形态、请求内容与全部事件 SHALL 与本变更之前逐字节一致

#### Scenario: 与审计同时配置
- **WHEN** 同时给出 `audit=` 与 `attachment_store=`（显式或经插件）
- **THEN** `create` SHALL 抛 `ValueError`，并说明两者暂不兼容

#### Scenario: 插件与显式参数冲突
- **WHEN** 宿主显式传了 `attachment_store=` 而某插件也提供它
- **THEN** `create` SHALL 抛 `PluginCapabilityError`

### Requirement: 先存后记
配置了存储时，用户消息与工具结果的内联附件 SHALL 在 admission 通过后、条目 durable append 之前写入存储，条目里 SHALL 只落引用形态。写入存储失败 SHALL 使该 submission 被拒绝（用户消息）或该次工具调用判错（工具结果），SHALL NOT 回退为内联形态。

#### Scenario: 带图的用户消息
- **WHEN** 配置了存储，用户消息带一张通过 admission 的内联 PNG
- **THEN** transcript 里该条目的附件 SHALL 是引用形态，transcript SHALL NOT 含该图片的 base64，存储里 SHALL 能按引用取回与原件逐字节一致的内容

#### Scenario: 引用上的元数据来自校验
- **WHEN** 图片通过 admission 并被写入存储
- **THEN** 引用上的 `width` / `height` SHALL 等于从该图片字节读出的值

#### Scenario: 存储写入失败的用户消息
- **WHEN** `put` 抛出错误
- **THEN** submission SHALL 被拒绝，transcript SHALL NOT 新增条目

#### Scenario: 存储写入失败的工具结果
- **WHEN** 工具返回图片附件而 `put` 抛出错误
- **THEN** 该次调用 SHALL 以 `tool_attachment_rejected` 判错，`function_call` 与其 output 的配对 SHALL 保持完整

#### Scenario: 被拒绝的附件不入存储
- **WHEN** 附件未通过 admission（超限、摘要不符、类型不允许）
- **THEN** SHALL NOT 调用 `put`

### Requirement: 宿主直接传引用
`UserMessage.attachments` SHALL 接受引用形态。内核 SHALL 取回字节并做完整 admission，以自己读出的元数据为准（取回经解析器缓存）。

#### Scenario: 先上传后引用
- **WHEN** 宿主先 `pool.attachments.put(...)`，再提交带该引用的用户消息
- **THEN** 消息 SHALL 被接受，模型收到的图片 SHALL 与上传的字节一致

#### Scenario: 引用指向不存在的对象
- **WHEN** 提交的引用在存储中不存在
- **THEN** submission SHALL 被拒绝

#### Scenario: 引用携带伪造的元数据
- **WHEN** 提交的引用声明的 `size` / `sha256` / `width` / `height` 与实际对象不符
- **THEN** submission SHALL 被拒绝，或落盘的引用 SHALL 以实际值为准；SHALL NOT 原样采信声明值

### Requirement: 解析阶段
每次采样构建请求之前，系统 SHALL 把本次请求用到的引用取回为字节，并核对 `sha256` 与 `size`。由引用投影出的 `ImagePart` / `FilePart` SHALL 与同一附件的内联形态逐字段相同。解析 SHALL 可被 turn 的取消信号中断，SHALL NOT 修改 history。

#### Scenario: 引用与内联的请求等价
- **WHEN** 同一张图片分别以内联形态和引用形态出现在两个其余完全相同的 thread 里
- **THEN** 两者发给 provider 的请求 SHALL 逐字节相同

#### Scenario: 已被驱逐的附件不解析
- **WHEN** 某引用所在条目的附件已被压缩策略驱逐
- **THEN** 构建请求 SHALL NOT 对该引用调用 `get`

#### Scenario: 缓存与并发合并
- **WHEN** 同一引用在缓存容量内被连续多次采样使用，或被并发解析
- **THEN** 后端 `get` SHALL 只被调用一次

#### Scenario: 关闭缓存
- **WHEN** `attachment_cache_bytes=0`
- **THEN** 每次采样 SHALL 重新从后端读取

### Requirement: 取不回的历史附件降级
历史条目上的引用在解析时不存在、后端出错或校验不过，系统 SHALL 在该次请求里以文本占位符替代该附件、发出 `attachment_unavailable` 事件，并继续 turn；占位符 SHALL 含类型、大小、摘要前缀、引用与原因，SHALL NOT 含正文。

#### Scenario: 对象丢失
- **WHEN** 历史里某图片引用的对象已从存储中删除
- **THEN** 请求里该位置 SHALL 是 `[attachment unavailable: ...]` 文本，事件 `reason` SHALL 为 `not_found`，turn SHALL 正常完成

#### Scenario: 对象被篡改
- **WHEN** 取回的字节摘要与引用上的 `sha256` 不符
- **THEN** SHALL NOT 把该字节发给 provider，事件 `reason` SHALL 为 `integrity_mismatch`

#### Scenario: 后续轮次不被拖垮
- **WHEN** 一个 thread 里有一张取不回的图片
- **THEN** 该 thread 之后的每一轮 SHALL 仍可正常采样

### Requirement: 未注入存储时遇到引用
pool 未注入存储而 thread 里存在引用形态的附件时，系统 SHALL 在该 thread 冷加载后的第一次采样之前抛 `AttachmentStoreRequiredError`，SHALL NOT 降级为占位符。

#### Scenario: 恢复时漏配存储
- **WHEN** 以未注入存储的 pool 恢复一个含引用形态附件的 thread
- **THEN** 第一个 turn SHALL 以 `AttachmentStoreRequiredError` 失败，且不向 provider 发出请求

### Requirement: 估算只读元数据
对引用形态的附件，token 与成本估算 SHALL 只使用引用上的元数据，SHALL NOT 调用存储或解码正文。`pages` 为 None 的文件 SHALL 取 `unknown_file_token_ceiling`。

#### Scenario: 估算不触发读取
- **WHEN** 对含多个引用形态附件的 history 估算 token
- **THEN** 存储的 `get` SHALL NOT 被调用

#### Scenario: 与内联形态估值一致
- **WHEN** 同一张图片分别以内联与引用形态估算
- **THEN** 两者的估算值 SHALL 相等

### Requirement: 可观测与脱敏
附件写入存储 SHALL 发出 `attachment_stored` 事件（`origin`、`kind`、`media_type`、`size`、摘要前缀、`ref`）；取不回 SHALL 发出 `attachment_unavailable`。两个事件、请求留痕与 telemetry SHALL NOT 含附件正文。

#### Scenario: 事件不含正文
- **WHEN** 带图的 turn 完成
- **THEN** 事件流与 JSONL telemetry 中 SHALL NOT 出现该图片的 base64

### Requirement: 引用枚举
系统 SHALL 提供 `collect_attachment_refs(items)`，返回一段历史里全部引用形态附件的引用集合（含已被驱逐后留在描述与压缩条目指针块里的引用），供宿主回收使用。内核 SHALL NOT 自行删除存储中的对象。

#### Scenario: 驱逐后的引用仍被枚举
- **WHEN** 某引用的附件已被驱逐，只剩描述里的 `attachment:<ref>`
- **THEN** `collect_attachment_refs` 的结果 SHALL 包含该引用

#### Scenario: thread 清理不删对象
- **WHEN** 调用压缩策略的 `cleanup_thread`
- **THEN** 附件存储中的对象 SHALL 保持不变
