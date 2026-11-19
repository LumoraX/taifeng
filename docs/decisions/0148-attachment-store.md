# ADR 0148：附件字节进独立存储，会话只留引用

- 状态：Accepted（实验层 🧪）
- 日期：2026-11-18
- 修订：[ADR 0147](0147-restorable-attachments.md)——引用形态的附件被驱逐时不再写工作区，指针为 `attachment:<ref>`；[ADR 0145](0145-plugin-backend-providers.md)——新增后端种类 `attachment_store`
- 关联：[capabilities/attachment-store.md](../architecture/capabilities/attachment-store.md)、openspec `attachment-store` / `attachment-store-journal`、ADR 0017（规则①③）

## 背景

图片与文件附件以完整 base64 内联在 JSONL transcript、内存 history 与严格审计 Journal 里：transcript 随附件线性膨胀；
每次构建请求、每次估算 token 都重新解码并校验正文；同一份内容被多次引用时存多份；被压缩淘汰的附件要另外落盘才
可恢复（ADR 0147）。同形态的 Claude Code 有公开的故障记录：单行 4–60 MB、121 MB 会话加载挂起、带图会话恢复失败。

DeepSeek Harness 的 attachment seam 与 Google ADK 的 artifact service 都是「字节进独立存储、会话里只留不透明引用与
已校验元数据」。

## 决策

1. **`AttachmentStore` 协议**（`put` / `get` / `exists` / `delete`，全部可取消）与本机实现 `LocalAttachmentStore`。
   `put` 幂等、返回即 durable、核对调用方给的 `sha256`。经 `EnginePool.create(attachment_store=)` 或插件
   `provide_backends` 注入；`pool.attachments` 暴露给宿主。
2. **引用形态** `ImageAttachmentRefV1` / `FileAttachmentRefV1`：`encoding="store"`，不含正文，带 admission 时从字节读出
   的宽高 / PDF 页数。判别用 `encoding` 而不是新的 `kind`——`kind` 已被十余处用来区分图片与文件。
3. **先存后记**：admission 通过后、durable append 之前写入存储，条目里只落引用。写不进存储即拒绝（用户消息）或判错
   （工具结果），不回退为内联。
4. **宿主传入的引用不被采信**：取回字节、完整 admission，以读出的元数据为准。
5. **解析阶段**：每次采样构建请求之前，把本次要用的引用取回并核对 `sha256` / `size`，换回与内联逐键相同的形态交给
   **同步**的构建函数——构建函数不改签名，provider 收到的请求逐字节相同。每个 pool 一个解析器，带按字节计的 LRU
   缓存（默认 64 MiB）与同引用并发合并。
6. **完整性校验在解析器，不在协议**：后端作者只管存取，不会因漏做校验留下静默损坏。
7. **历史附件取不回即降级**：占位文本 `[attachment unavailable: …]` + `attachment_unavailable` 事件，turn 继续。
   本次提交自带的附件取不回则拒绝提交；history 有引用而未注入存储则 `AttachmentStoreRequiredError`（装配错误，不降级）。
8. **估算只读元数据**，不调用存储、不解码。
9. **压缩**：引用形态被驱逐 / 摘要时指针为 `path=attachment:<ref>`，不写工作区、不读存储；`file_read(attach_images=True,
   attachment_store=)` 识别该前缀，只放行调用方 thread 引用过的附件，取回字节作为图片返回（结算时写回存储，内容
   寻址所以不产生第二个对象）。对模型只有一条规则：描述里给了 `path=`，就用 `file_read` 读它。
10. **默认不启用、实验层发布**；与严格审计（`audit=`）互斥，Journal 的引用记录单列为 `attachment-store-journal`。
11. **不做引用计数**：存储对保留期中立；提供 `collect_attachment_refs(items)` 供宿主做标记-清除，内核从不删除对象。

### 否决的方案

- **把工作区当附件存储**（沿用 ADR 0147 的 `_offload/`）：工作区模型可写，原件必须不可变；工作区可能按 thread 隔离，
  做不到跨 thread 共享。
- **provider Files API 作主存储**：thread 被锁在一个 provider 上，换模型即失效。可作为解析阶段之上的上传缓存。
- **在 `MessageStore` 上加 blob 方法**：已有宿主实现全部要改；附件的生命周期与消息不同。
- **构建函数改异步**：波及 admission 复核、压缩视图、回放等全部调用点，风险与收益不成比例。
- **引用上不放元数据、估算时按需取字节**：把解码成本换成后端读取成本，远端后端下更糟。
- **启用时改写既有 transcript 外置内联附件**：违反 R5 追加写；双读已让旧 thread 可用。

## 后果

- 恢复依赖 transcript 与附件存储两者一致，须同备份。漏配存储时在首次采样前失败、不发请求。
- 启用后含引用形态的 thread 不能被旧版本恢复（旧版本不认识 `encoding="store"`）。
- `ToolResult.attachments` 接受引用形态；严格审计下工具返回引用判 `tool_attachment_rejected`。
- 修正 `tool-image-attachment.md` 中「strict SessionJournal 不支持」的陈旧描述（代码早已支持工具图片落账）。

## R1–R5

- R1：协议与本机实现无业务概念；多租户由宿主按 pool 注入不同实例。
- R2：请求逐字节不变；解析阶段不改 history。
- R3：`attachment_stored` / `attachment_unavailable`，不带正文。
- R4：存取可取消；本机实现的写入用临时文件 + 原子替换，取消不留残缺对象。
- R5：先存后记；transcript 仍 append-only。

## 验证

- Sim：`tests/conversation/test_attachment_store.py`（含可复用的 `attachment_store_conformance.py`）、
  `tests/loop/test_attachment_refs.py`（先存后记、请求等价、写入失败、伪造元数据、降级两种原因、漏配存储的恢复、
  估算不读存储、缓存与并发合并、工具结果、驱逐与读回全链路）、`tests/plugin/test_backend_capabilities.py`。
  降级与完整性校验各做过一次变异检查（去掉即变红）。
- 示例：`examples/attachment_store/demo.py`（一张 43 KB 的图：transcript 从约 58.5 KB 降到约 0.8 KB）。
- 真实模型：`openai_responses_image_ref_restore` / `codex_image_ref_restore`——图片以引用落盘、被驱逐，模型自行经
  `attachment:<ref>` 读回并答出只在图上的序列号，且 transcript 不含图片正文；结果见台账。
