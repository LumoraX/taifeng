# Capability: attachment-store（附件存储，实验层）

> 状态：🧪 Experimental（ADR 0148）。默认不启用；暂不可与严格审计（`audit=`）同用。
> 实现：`src/taifeng/conversation/attachment_store.py`（协议、本机实现、引用枚举）、`src/taifeng/llm/attachment_refs.py`
> （引用形态）、`src/taifeng/loop/attachment_resolve.py`（先存后记、解析阶段、降级）、`src/taifeng/loop/pool_attachments.py`
> （装配）。经 `taifeng.experimental` 导出。

## Purpose

附件字节放进独立存储，会话条目、transcript 与内存 history 里只留引用和已校验的元数据。transcript 不再随附件膨胀，
估算不再解码正文，同一内容只存一份，被压缩淘汰的附件天然可取回。

## 数据契约

### `AttachmentStore`（协议，`runtime_checkable`）

| 方法 | 语义 |
| --- | --- |
| `put(data, *, media_type, sha256) -> str` | 写入并返回引用；**幂等**（同内容同引用、不重写）；返回即 durable；`sha256` 与内容不符抛 `AttachmentIntegrityError` 且不写 |
| `get(ref) -> bytes` | 读取；不存在抛 `AttachmentNotFoundError` |
| `exists(ref) -> bool` | 是否存在 |
| `delete(ref) -> None` | 删除；不存在为 noop。只供宿主回收，内核从不调用 |

引用只含 `[A-Za-z0-9._:-]`、长度 1–200；**不透明**——消费方不得解析或据此推导路径。四个方法都必须可被 anyio 取消。
完整性校验不在协议里：解析器统一核对。宿主实现可用 `tests/conversation/attachment_store_conformance.py` 自检。

`LocalAttachmentStore(root_dir)`：对象在 `<root>/v1/<摘要前 2 位>/<摘要>`，引用 `sha256-<64 hex>`；写临时文件 + `fsync` +
原子替换，取消不留残缺对象；阻塞 IO 在工作线程。

### 引用形态

| 模型 | 字段 |
| --- | --- |
| `ImageAttachmentRefV1` | `kind="image"`、`encoding="store"`、`ref`、`media_type`、`size`、`sha256`、`width`、`height`、`detail` |
| `FileAttachmentRefV1` | `kind="file"`、`encoding="store"`、`ref`、`media_type="application/pdf"`、`size`、`sha256`、`pages`（可为 None）、`filename` |

冻结、`extra="forbid"`、不接受 `content`。判别字段是 `encoding`：`"base64"` = 内联（既有 V1），`"store"` = 引用；同一条目的
`attachments` 里两种形态可以并存。`width` / `height` / `pages` 只由 admission 写入，之后被信任。

### 装配

| 入口 | 说明 |
| --- | --- |
| `EnginePool.create(attachment_store=, attachment_cache_bytes=64 MiB)` | 注入；`0` 关闭解析器缓存；同时给 `audit=` → `ValueError` |
| `PluginBackends.attachment_store(factory)` | 插件提供（ADR 0145 规则：单一提供者、与显式参数冲突报错、提供后冻结、须实现协议否则 `TypeError`） |
| `pool.attachments` | 注入的存储（宿主先上传再引用、做回收）；未注入为 None。pool 不关闭它 |
| `make_file_read_tool(attach_images=True, attachment_store=)` | 读 `attachment:<ref>` |
| `collect_attachment_refs(items) -> set[str]` | 一段历史引用到的全部附件（条目上的、驱逐描述里的、压缩条目指针块里的） |

### 事件（R3，均不含正文）

- `attachment_stored`：`{origin: "user_message"|"tool_result", kind, media_type, size, sha256（前 8 位）, ref}`
- `attachment_unavailable`：`{ref, kind, media_type, item_id, reason: "not_found"|"integrity_mismatch"|"backend_error"}`

### 错误

| 错误 | 何时 |
| --- | --- |
| `AttachmentUnavailableError`（`InvalidRequestError`） | 本次提交 / 本次工具结果自带的附件取不回或写不进存储 |
| `AttachmentStoreRequiredError`（`InvalidHistoryError`） | 有引用形态而未注入存储 |
| `AttachmentStoreError` / `AttachmentNotFoundError` / `AttachmentIntegrityError` | 存储层，供后端实现与解析器使用 |

## Requirements

### Requirement: 未注入时行为不变
未注入存储且没有引用形态时，持久化形态、请求内容与事件 SHALL 与引入本能力之前逐字节一致。

### Requirement: 先存后记
配置了存储时，用户消息与工具结果的附件 SHALL 在 admission 通过后、durable append 之前写入存储，条目里只落引用；
未过 admission 的附件 SHALL NOT 写入；写入失败 SHALL 拒绝该提交（`AttachmentUnavailableError`，transcript 无新条目）
或使该次工具调用判 `tool_attachment_rejected`（配对完整），SHALL NOT 回退为内联。

### Requirement: 宿主传入的引用
`UserMessage.attachments` 与 `ToolResult.attachments` SHALL 接受引用形态。内核 SHALL 取回字节并完整 admission，以读出的
元数据为准；对象不存在或摘要不符 SHALL 拒绝。未注入存储时：用户消息抛 `AttachmentStoreRequiredError`，工具结果判错。

### Requirement: 解析阶段
每次采样构建请求前 SHALL 把本次用到的引用取回并核对 `sha256` / `size`，换回与内联逐键相同的形态；SHALL NOT 修改
history；同一张图以引用与内联两种形态出现时，provider 收到的消息 SHALL 逐字段相同。已被驱逐的附件不在条目上，
SHALL NOT 被读取。缓存容量内同一引用的连续与并发读取 SHALL 只调用一次后端。

### Requirement: 降级
历史条目上的引用取不回或校验不过时，该次请求 SHALL 以 `[attachment unavailable: <类型> <大小> sha256=<前 8 位> ref=<ref>: <原因>]`
代替该附件、发 `attachment_unavailable`，turn SHALL 继续；校验不过的字节 SHALL NOT 发给 provider；之后的轮次 SHALL 正常。

### Requirement: 漏配存储
history 有引用形态而未注入存储时，turn SHALL 在发出任何请求之前以 `AttachmentStoreRequiredError` 失败（`turn_failed.kind`
为类名、`failure_class="invalid_request"`）。

### Requirement: 估算
引用形态的 token / 成本估算 SHALL 只用 `width` / `height` / `pages`，SHALL NOT 调用存储；与同一附件内联形态的估值相等；
`pages=None` 取 `unknown_file_token_ceiling`。

### Requirement: 压缩与读回
引用形态被驱逐或被 handoff 摘要时，指针 SHALL 为 `path=attachment:<ref>`（与是否配置工作区无关），SHALL NOT 写工作区、
SHALL NOT 读存储；驱逐的 `detail.offloaded_attachments` 只计工作区落盘。`file_read(attach_images=True, attachment_store=)`
对 `attachment:<ref>`：引用 SHALL 出现在调用方 thread 的 history 里（`ToolContext.extras["attachment_refs"]`，否则
`attachment_not_in_thread`）；取回后按文件头识别为图片则作为附件返回（`output` 只写调用方给的路径）；不是图片
`attachment_not_image`；不存在 `not_found`；未给存储 `attachment_store_not_configured`。读回的图片在结算时写回存储，
内容寻址，不产生第二个对象。

### Requirement: 回收由宿主负责
内核 SHALL NOT 删除存储对象（含压缩策略的 `cleanup_thread`）；宿主据 `collect_attachment_refs` 做标记-清除。

## 边界（如实记录）

- 严格审计下不可用；工具在审计模式返回引用判 `tool_attachment_rejected`。见 openspec `attachment-store-journal`。
- 启用后含引用形态的 thread 不能被旧版本恢复。
- transcript 与附件存储须同备份；只恢复前者会让历史附件全部降级为占位文本。
- PDF 只留指针，不能经 `file_read` 作为附件返回（工具结果只承载图片）。
- 不做图片规范化、按路由缩放、provider Files API 上传缓存；可叠加在解析阶段之上。
- 工作区里名为 `attachment:xxx` 的真实文件在 `attach_images=True` 时读不到（前缀判断先于路径解析）。

## R1–R5

- R1：无业务概念；后端经协议注入。
- R2：请求逐字节不变；解析阶段不改 history；读回的图片进 tail。
- R3：两个事件；不带正文。
- R4：存取与解析可取消；本机写入原子替换。
- R5：先存后记；transcript 仍 append-only；冷恢复须注入同一存储。

## 验证

见 ADR 0148「验证」。
