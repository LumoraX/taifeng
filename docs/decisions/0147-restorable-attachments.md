# ADR 0147：被压缩移出上下文的附件可恢复 —— 落盘、留路径、用读取工具读回

- 状态：Accepted
- 日期：2026-11-17
- 修订：[ADR 0082](0082-multimodal-payload-eviction.md)（驱逐只留描述）——新增可选的落盘与路径；tool-image-attachment 契约「不提供任何取图工具的实现」——`file_read` 新增可选的图片返回
- 关联：[compaction-multimodal-eviction](../architecture/capabilities/compaction-multimodal-eviction.md)、[compaction-offload-strategy](../architecture/capabilities/compaction-offload-strategy.md)、[tool-image-attachment](../architecture/capabilities/tool-image-attachment.md)、ADR 0017（规则①③）、ADR 0059（压缩后衔接）

## 背景

图片 / 文件被压缩移出上下文后，模型能看到的只有 `[evicted: 1 attachment(s): image/png 812KB sha256=1a2b3c4d]`。
原件仍在 JSONL transcript 里，但模型没有任何可执行的线索：摘要前缀不是地址，内核也没有按摘要取回的工具。
用户再问「看一下我之前发的那张图」，模型只能凭自己当时写下的文字作答，或者承认看不到。handoff 摘要之后更糟：
连摘要前缀也只剩 `[含 N 张图片]` 的计数。

同一仓库里，超大**文本**工具输出已经是可恢复的：`OffloadStrategy` 落盘 + stub 里写路径 + `file_read` 分页回读。
图片被排除在外，理由是「落盘后 `file_read` 回来是文本，模型看不见」。缺的只是读取工具返回图片的能力。

调研结论（外部做法）：Manus 把「可恢复」定为压缩的硬要求——内容可以离开上下文，只要留下能取回它的路径或 URL；
DeepSeek Harness 的图片 offload 在占位文本里写明附件与只读路径，恢复靠读取工具而不是自动回填。

## 决策

1. **落盘**。`MultimodalEvictionStrategy` 与 `HandoffCompactionStrategy` 新增可选的 `file_root` / `workspace`。
   配置后，附件原件在离开上下文之前写进工作区 `_offload/{thread_id}/att-<sha256 前 16 位><扩展名>`。
   按内容寻址：同一附件再次淘汰（如冷加载后）得到同一路径、不重复写。目录与 `OffloadStrategy` 共用，
   thread 级联清理是同一个入口。
2. **留路径**。驱逐描述在落盘成功时带 `path=<相对路径>`，并另起一行写明用 `file_read` 读取。
3. **读回**。`make_file_read_tool(attach_images=True)`：读到图片文件（按文件头识别 PNG / JPEG / GIF / WebP）
   时作为 `ToolResult.attachments` 返回，走既有的工具图片附件准入与协议分档。默认 `False`。
4. **指针不经 LLM 转述**。handoff 把被压缩区间里全部带路径的描述（含更早一轮压缩条目里的）原样放进压缩条目的
   `<offloaded_attachments>` 块，上限 `max_attachment_pointers`（默认 32，保留最近的）。
5. **不自动回填**。内核不替模型决定何时再看一张图；读回的图片进入 tail，不改写已缓存前缀（R2）。

### 否决的方案

- **按摘要取回的专用工具（`recall_attachment(sha256)`）**：多一个工具、多一套寻址，且只对附件有效。
  路径 + 既有读取工具对文本落盘与附件落盘是同一机制，子 skill 与冷恢复后也可用（共享工作区）。
- **从 transcript 里取回**：要求工具能读对话存储，把 `MessageStore` 暴露给工具层；业务把存储换成 DB 后还要另行适配。
  工作区已经是模型可读的持久面。
- **附件一进来就落盘（admission 期）**：要改 `loop/` 的准入路径，且绝大多数附件从不被淘汰。
  淘汰时刻附件正文仍在内存 history 里，此时落盘足够。
- **放开 `OffloadStrategy` 接手带图的工具结果**：它按 `call_id` 命名、一条输出一个文件；附件是多份二进制，
  按内容寻址更合适。两者共用目录，各管各的。
- **压缩后自动重新附上最近的若干文件**（Claude Code 的做法，社区资料）：是策略而非机制，且会重新吃掉刚腾出的
  空间。指针留在上下文里，业务要这样做可在 `post_turn` / `InjectSystemMessage` 上自行实现。

## 边界

- **PDF 等文件附件**同样落盘并留路径，但 `file_read` 不能把它作为文件附件返回（工具结果只承载图片）。
  路径仍可供脚本或宿主工具处理。
- **未配置工作区**：行为与本 ADR 之前逐字节一致（描述无路径、`detail` 无新键）。
- **落盘失败**（正文不可解码、`thread_id` 不是安全路径段、写入错误）：附件照常淘汰，只是不带路径；每种情形有
  warning，`detail.offloaded_attachments` 如实计数。
- **审计模式**：落盘是工作区里的旁路文件，不进 Journal；压缩条目里的指针块是摘要正文的一部分，随压缩条目落账。

## R1–R5

- R1：无业务概念；后端经 `WorkspaceFS` 协议注入。
- R2：驱逐的 cache 语义不变；读回的图片追加在 tail。handoff 的指针块在压缩条目内，不产生额外条目。
- R3：`compaction_completed.detail.offloaded_attachments`；读回是普通的 `tool_call_*`。
- R4：落盘是可取消的 `await`；驱逐与 handoff 的协作检查点不变。
- R5：transcript 仍 append-only；工作区文件按内容寻址，冷恢复后重新驱逐指向同一文件。

## 验证

- Sim：`tests/context/test_attachment_offload.py`（落盘、路径穿越、幂等、指针跨多轮摘要保留、handoff 变异检查、
  驱逐 → `file_read` 读回逐字节一致、未 opt-in 时的显式错误）。
- 真实模型：`examples/real_llm/_restorable_image.py`（`openai_responses_image_restore` / `codex_image_restore`）——
  图片被驱逐后模型自行 `file_read` 落盘路径并答出只在图上的序列号；结果见台账。
