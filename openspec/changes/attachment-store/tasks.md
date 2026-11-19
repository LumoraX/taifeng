# attachment-store 实施计划

> 已实施（ADR 0148，分支 `feat/attachment-store`）。与原计划的差异：`attachment_stored` 不报「是否为已存在对象」（协议的 `put` 只返回引用，多一次 `exists` 不值得）；宿主引用每次提交都取回并 admission（经解析器缓存），不另设「同一 pool 只验一次」的集合；`file_read` 读回的图片以内联形态返回、结算时写回存储（内容寻址，不产生第二个对象）。未勾选项为尚未单列测试或尚未执行的部分（6.5 子 thread 并行解析、9.2 rewind / suspend 等边界、9.3 录制回放）；它们走同一个解析阶段，但没有专门用例证明。

> 前置：`restorable-attachments`（ADR 0147）已合入 main。分支 `feat/attachment-store`，独立 worktree。
> 每个编号是一步可独立提交的工作（≤ 3h）；先红后绿。全程未注入存储时既有测试不得变化。

## 1. 契约与决策
- [x] 1.1 写 ADR（下一个可用编号）：引用形态、先存后记、解析阶段、降级规则、与审计互斥、否决的方案。Acceptance：标注对 ADR 0147 的修订关系（引用形态的驱逐不落盘）与对 ADR 0145 的扩展（新后端种类）。
- [x] 1.2 新建 `docs/architecture/capabilities/attachment-store.md`，把本变更 `specs/attachment-store/spec.md` 落成契约；在 `capabilities/README.md` 登记。Acceptance：R1–R5 影响逐条写明。

## 2. 存储协议与本机实现
- [x] 2.1 `tests/conversation/test_attachment_store.py` 红测试：幂等 `put`、摘要不符抛 `AttachmentIntegrityError`、`get` 不存在抛 `AttachmentNotFoundError`、`delete` 幂等、并发写同一内容、写入中途取消不留残缺对象、引用字符集。Acceptance：观察红。
- [x] 2.2 实现 `conversation/attachment_store.py`：`AttachmentStore` 协议、三个错误类型（归入既有错误分类）、`LocalAttachmentStore`。Acceptance：测试绿；阻塞 IO 全部走工作线程。
- [x] 2.3 提供协议一致性测试套件 `tests/conversation/attachment_store_conformance.py`（参数化的后端工厂），本机实现跑通。Acceptance：宿主实现可直接复用该套件。

## 3. 引用形态
- [x] 3.1 `ImageAttachmentRefV1` / `FileAttachmentRefV1` 及校验测试（拒绝正文字段、拒绝非法 `ref`、冻结）。Acceptance：测试绿；经 `taifeng.experimental` 导出。
- [x] 3.2 `loop/attachment_parts.py`：按 `encoding` 分派的解析入口，内联路径逐行为不变。Acceptance：既有图片 / 文件测试全绿。

## 4. 注入
- [x] 4.1 `EnginePool.create(attachment_store=, attachment_cache_bytes=)` 与 `pool.attachments`；`audit` 同时给出时构造期 `ValueError`。Acceptance：测试覆盖互斥与默认不变；`pool.py` ≤ 800 行。
- [x] 4.2 `plugin/backends.py` 增加 `attachment_store` 后端种类；冲突、冻结、回滚测试。Acceptance：`tests/plugin/test_backend_capabilities.py` 新用例绿。

## 5. 写入路径（先存后记）
- [x] 5.1 红测试 `tests/loop/test_attachment_refs.py`：带图用户消息落引用、transcript 不含 base64、元数据等于实测值、`put` 失败时 submission 被拒且无新条目、未过 admission 不调 `put`。
- [x] 5.2 实现用户消息路径（`engine_submit.py` / `engine_gate.py`）。Acceptance：5.1 绿。
- [x] 5.3 工具结果路径（`turn_tooling.py`）：落引用、`put` 失败判 `tool_attachment_rejected`、`ToolResult.attachments` 接受引用形态、未配置存储时返回引用判错。Acceptance：配对完整性测试绿。
- [x] 5.4 宿主直接传引用：首次进入 pool 重新 admission、伪造元数据不被采信、不存在的引用被拒、同一引用只验一次。Acceptance：测试绿。
- [x] 5.5 `attachment_stored` 事件与 console 渲染；事件不含正文。Acceptance：R3 审计（`capability_matrix.py` 报告 B）无未映射 kind。

## 6. 读取路径（解析阶段）
- [x] 6.1 红测试 `tests/loop/test_attachment_resolve.py`：引用与内联的 provider 请求逐字节相同（OpenAI Responses / Chat、Codex、Anthropic 文件、Gemini 各一例）；已驱逐的不解析；缓存与并发合并；`attachment_cache_bytes=0`；取消。
- [x] 6.2 实现 `loop/attachment_resolve.py`（`AttachmentResolver`：按字节 LRU、单飞、sha256 / size 校验）并接入采样前流程；`loop/prompt.py` 构建函数保持同步，从解析结果取正文。Acceptance：6.1 绿；`prompt.py` ≤ 800 行。
- [x] 6.3 降级：对象丢失 / 摘要不符 / 后端错误 → 占位文本 + `attachment_unavailable`，后续轮次可继续。Acceptance：三种 `reason` 各有测试；摘要不符时字节不出现在请求里。
- [x] 6.4 未注入存储：冷加载发现引用形态 → 第一次采样前 `AttachmentStoreRequiredError`，不发请求。Acceptance：测试绿。
- [ ] 6.5 子 thread（`call_skill` / `spawn_skill`）共用 pool 的解析器；并行子 skill 解析同一引用只读一次后端。Acceptance：测试绿。

## 7. 估算
- [x] 7.1 `tests/context/test_attachment_ref_estimation.py`：引用形态估算不调 `get`、与内联估值相等、`pages=None` 取上限。Acceptance：红 → 绿。
- [x] 7.2 `context/budget.py` 对引用形态读元数据。Acceptance：既有 `test_tool_image_accounting.py` / `test_file_input_accounting.py` 不变。

## 8. 压缩与读回
- [x] 8.1 驱逐与 handoff：引用形态不落盘、描述带 `attachment:<ref>`、混合形态、指针块识别两种路径（`context/attachment_offload.py`、`multimodal_evict.py`、`handoff.py`）。Acceptance：`tests/context/test_attachment_offload.py` 补用例；既有用例不变。
- [x] 8.2 `make_file_read_tool(attachment_store=)`：识别 `attachment:<ref>`、本 thread 归属校验、按引用返回、四种错误（`attachment_not_in_thread` / `attachment_not_image` / `attachment_store_not_configured` / 不存在）。Acceptance：测试绿；`ToolContext` 提供本 thread 引用集的方式写入契约。
- [x] 8.3 `collect_attachment_refs`：覆盖条目上的引用、驱逐描述里的引用、压缩条目指针块里的引用；`cleanup_thread` 不删存储对象。Acceptance：测试绿。

## 9. 恢复与边界
- [x] 9.1 R5：写入 → 关闭 pool → 以同一存储重建 → 续跑，图片可用；新旧形态并存的 thread 冷恢复。Acceptance：测试绿。
- [ ] 9.2 边界：空附件、0 字节拒绝、超长 `ref`、单条消息多附件部分失败（全有或全无）、rewind 跨过带引用的条目、suspend / resume 途中带引用。Acceptance：各有测试。
- [ ] 9.3 Journal 回放客户端（非审计路径的录制回放）在引用形态下的请求摘要与内联一致。Acceptance：`tests/llm/test_journal_replay_*.py` 补一例。

## 10. 文档
- [x] 10.1 更新 `llm-image-input.md`、`llm-file-input.md`、`tool-image-attachment.md`（含更正「strict SessionJournal 不支持」一行）、`compaction-multimodal-eviction.md`、`plugin-system.md`。
- [x] 10.2 更新 `docs/architecture/conversation.md`（附件存储一节，写明 transcript 与附件存储须同备份）、`docs/architecture/context-compression.md`、`docs/configurable-knobs.md`、`docs/README.md` ADR 索引、`docs/capability-matrix.md`。
- [x] 10.3 示例 `examples/attachment_store/demo.py`（SimClient，无需 key）：上传 → 引用 → 驱逐 → 读回，并打印 transcript 大小对比。Acceptance：`scripts/verify_examples.py` 通过。

## 11. 验证
- [x] 11.1 `PYTHONPATH=src uv run pytest tests/ -q`、`scripts/verify_examples.py`、ruff 门禁、mypy strict。Acceptance：全绿；`tests/public_api_snapshot.txt` 仅在稳定层确有新增时更新。
- [x] 11.2 真实场景：在 `examples/real_llm/_restorable_image.py` 旁新增引用形态版本（`*_image_ref_restore`）——图片以引用落盘、被驱逐、模型经 `attachment:<ref>` 读回并答出只在图上的序列号；另核验 transcript 不含 base64。Acceptance：PASS 并入台账。
- [x] 11.3 全量 `examples/real_llm/capability_matrix.py` 并提交 `docs/real-llm-ledger.{json,md}`。Acceptance：通用能力全部 PASS；未过项逐条说明与本变更是否相关。
