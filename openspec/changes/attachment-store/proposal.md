## Why

图片与文件附件今天以完整 base64 内联在三处：JSONL transcript 的条目、内存 history、以及严格审计下的 Journal 记录。后果有四个：

1. **transcript 随附件线性膨胀**。一张截图是几百 KB 的一行 JSON；带图的长会话冷加载要把全部正文读进内存并解析。同形态的 Claude Code 已有公开的故障记录：单行 4–60 MB、121 MB 会话加载挂起、带图会话恢复失败。
2. **每次构建请求、每次估算 token 都重新解码并校验正文**（`context/budget.py` 的 `_estimate_image_tokens` / `_estimate_file_tokens` 走完整 admission）。压缩检查每轮都估算，附件越多越慢。
3. **同一份内容存多份**。同一张图被多次引用、被 spawn 的 child 复用、被工具读回，每次都是一份新的 base64。
4. **被压缩淘汰的附件要另外落盘才可恢复**（ADR 0147 的做法）。原件本来就该有一个稳定的家，淘汰只需要换掉引用。

对标：DeepSeek Harness 的 attachment seam 与 Google ADK 的 artifact service 都是「字节进独立存储、会话里只留引用与已校验的元数据」。内核立项依据是 ADR 0017 规则①（R5 恢复与上下文机制）与规则③（存储后端走协议、实现在外）。

## What Changes

- 新增 `AttachmentStore` 协议（`put` / `get` / `exists` / `delete`）与本机实现 `LocalAttachmentStore`；`EnginePool.create(attachment_store=...)` 注入，插件可经 `provide_backends` 提供（新增后端种类 `attachment_store`）。
- 新增附件的**引用形态**：`ImageAttachmentRefV1` / `FileAttachmentRefV1`——不含正文，带不透明引用与**已校验元数据**（类型、大小、sha256，图片的宽高与帧数、PDF 的页数）。
- 配置了存储时：用户消息与工具结果的附件在 admission 通过后、durable append 之前写入存储，条目里只落引用（先存后记）。
- 请求构建前新增一个**解析阶段**：把本次请求要用到的引用取回为字节，校验 sha256 与大小；构建函数本身保持同步。
- token / 成本估算对引用形态只读元数据，不再解码正文。
- 取不回或校验不过的历史附件**降级为模型可见的占位文本并发事件**，不让一张坏图永久卡死整个 thread。
- `UserMessage.attachments` 同时接受引用形态：宿主可先 `pool.attachments.put(...)` 上传，再在多条消息 / 多个 thread 里引用。
- 与 ADR 0147 对齐：引用形态的附件被驱逐时不再写工作区，描述里的 `path=` 为 `attachment:<ref>`；`file_read(attach_images=True, attachment_store=...)` 识别该前缀并直接按引用返回图片（不复制字节）。
- 内联形态（V1）继续可读可写：未配置存储时行为逐字节不变；已有 thread 无需迁移。

## Capabilities

### New Capabilities

- `attachment-store`：附件字节的独立存储、引用形态、先存后记、解析阶段、降级与可观测。

### Modified Capabilities

- `llm-image-input` / `llm-file-input`：持久化形态新增引用；估算读元数据。
- `tool-image-attachment`：工具结果附件落引用；`file_read` 识别 `attachment:<ref>`。
- `compaction-multimodal-eviction`：引用形态的驱逐不落盘，描述带 `attachment:<ref>`。
- `plugin-system`：`BackendKind` 增加 `attachment_store`。

## Non-goals

- **严格审计（SessionJournal）下的引用形态**：本变更内 `audit=` 与 `attachment_store=` 同时给出时构造期报错。Journal 的记录形状、hash 链与回放单独立项（`attachment-store-journal`）。
- 不在内核实现任何远端后端（S3 / DB / 对象存储）——宿主实现协议。
- 不做引用计数与自动回收：存储对保留期中立，恢复与分叉的 thread 可共享对象；内核只提供「列出一个 thread 引用了哪些附件」的工具函数。
- 不做按模型路由的图片缩放 / 转码，不做 provider 侧 Files API 上传缓存（`file_id`）。两者都可以后续叠加在解析阶段之上。
- 不改写既有 transcript（R5 追加写）；不提供把旧 thread 的内联附件批量外置的迁移工具。
- 不让工具结果承载文件（PDF）附件。

## R1–R5 影响

- R1：协议与本机实现无业务概念；多租户隔离由宿主按 pool 注入不同的存储实例或包装实现。
- R2：引用与内联在 provider wire 上产生的请求逐字节相同，prompt cache 前缀不受影响；解析阶段不改 history。
- R3：新增 `attachment_stored`、`attachment_unavailable` 事件；不带正文。
- R4：`put` / `get` 接收 turn 的取消信号；解析阶段可取消。
- R5：**直接相关**。transcript 只剩引用后，恢复依赖同一个存储；未注入存储却遇到引用形态时显式报错。先存后记保证不会出现「条目已落、字节不在」。

## Impact

实现：`src/taifeng/conversation/attachment_store.py`（协议 + 本机实现）、`src/taifeng/llm/{image_input,file_input,types}.py`（引用形态）、`src/taifeng/loop/{attachment_parts,attachment_resolve,engine_submit,engine_gate,turn_tooling,prompt,pool,pool_plugins}.py`、`src/taifeng/context/{budget,attachment_offload}.py`、`src/taifeng/context/strategies/multimodal_evict.py`、`src/taifeng/tool/builtins/file_io.py`、`src/taifeng/plugin/backends.py`、`src/taifeng/experimental.py`。

测试：`tests/conversation/test_attachment_store.py`、`tests/loop/test_attachment_refs.py`、`tests/loop/test_attachment_resolve.py`、`tests/context/test_attachment_ref_estimation.py`、既有图片 / 文件 / 驱逐 / 工具附件测试补引用形态用例。

文档：新 ADR、`docs/architecture/capabilities/attachment-store.md`（新契约）、`llm-image-input.md` / `llm-file-input.md` / `tool-image-attachment.md` / `compaction-multimodal-eviction.md` / `plugin-system.md`、`docs/architecture/conversation.md`、`docs/configurable-knobs.md`、`docs/capability-matrix.md`。

触及 `conversation/`、`loop/`、`context/`、`llm/` 四个基础层目录：合入前须全量真实回归并提交台账。前置：`restorable-attachments`（ADR 0147）已合入。
