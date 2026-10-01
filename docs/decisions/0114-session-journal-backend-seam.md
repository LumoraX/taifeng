# ADR 0114：SessionJournal 后端 seam 与一致性检查

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-backend](../architecture/capabilities/session-journal-backend.md)、
  [session-journal-core](../architecture/capabilities/session-journal-core.md)；ADR 0025、0053、0066、0111

## 背景

ADR 0025 把 SessionJournal 定为审计会话的唯一事实源。多实例平台因此真正需要的是可跨机器共享的 Journal
后端与跨机器的写者互斥，而不只是 `MessageStore`。内核这边：

- 唯一的实现 `JsonlSessionJournalCore` 在实验层，只能写本机文件；
- 它其实早就把物理读写与互斥收在了两个可注入的边界里（`SyncFileAdapter`、`WriterLockAdapter`），但两者
  都没有导出，也没有成文的语义；
- `AuditConfig.journal_core` 收的是一个 5 方法的协议，定义在 `loop/` 内部、没有导出；它签名里的全部
  类型与错误类型同样没有导出；
- 没有任何手段让一个外部实现证明自己满足语义。core 的语义（CAS、幂等、lease、接管、epoch 谱系）只存在于
  JSONL 实现及其测试里。

外部包没有合规途径写 Journal 后端（适配包为此专门立了「等内核公开协议，不绕道私有模块」的决策）。

命中 ADR 0017 规则③：内核只定协议，实现走外部。

## 决策

1. **公开两个注入层次**，由实现方按代价选：
   - **换存储**：`SyncFileAdapter` + `WriterLockAdapter` 注入 `JsonlSessionJournalCore`，5 个同步方法，
     其余逻辑沿用内核；
   - **换 core**：实现 `SessionJournalCore`（原 `AuditJournalCore`，定义挪到 `conversation/journal/backend.py`，
     旧名保留）。
2. **给「换 core」配存储无关的构件**（`journal/backend.py`，纯函数）：`seal_batch`、`snapshot_records`、
   `resolve_idempotent_ack`、`verify_envelopes`、`index_envelopes` 等。hash chain 与幂等规则只有一份权威
   实现——构件算出的 envelope 与 JSONL core 落盘的逐字段相同（测试守护）。
3. **一致性检查是协议的一部分**（`taifeng.testing.journal_conformance`）：22 项 core 检查 + 5 项存储检查 +
   2 项锁检查，不依赖测试框架。三种后端形态跑同一套：默认 JSONL core、装了外部适配器的 JSONL core、
   参考实现。检查本身也被验证：四个故意写坏的后端各自在对应的 case 上失败。
4. **`InMemorySessionJournalCore` 作为参考实现随包发布**：只 import 公共 API 里的名字（测试守护），是数据库
   版后端的样板，也让审计模式的测试可以不落文件。
5. **全部放实验层**（`taifeng.experimental` 新增 48 个名字 + `taifeng.testing`）。审计模式整体是 🧪：入口
   `AuditConfig` 在实验层，记录族今天还在增加（ADR 0094–0104）。后端 seam 单独进稳定层没有意义——用它
   必须经过实验层的入口；等审计模式的契约转 ✅ 时一并晋升。

## 不做

- **把 seam 直接定进稳定层**：见决策 5。适配包若坚持只依赖稳定层，需要为审计模式开一个有边界的例外，
  或等它晋升。
- **通用检查覆盖物理损坏与结果未知的提交**：这些取决于存储介质（文件的 torn tail 在数据库里不存在，
  数据库有自己的事务中断形态），写成通用检查只会是某一种介质的检查。
- **优化 JSONL core 每次追加的整读**：那是参考实现「正确优先」的取舍；需要与长度无关的追加正是
  「换 core」存在的理由。
- **让审计模式的对话投影走外部 `MessageStore`**：投影是 Journal 的派生视图，接管从 Journal 重建；共享
  投影不是共享后端的前提。
- **旧 transcript 导入接受任意 core**：导入是一次性迁移动作，先只支持默认后端。

## 影响

- R1：无业务概念。
- R2–R4：无影响。
- R5：审计 Session 可由另一台机器上的实例接管，不再要求共享卷。

### 行为变化

无。`AuditConfig.journal_core` 本来就按协议接受对象；本次只是把协议、类型、构件与检查公开出来。

## 验证

- `tests/testing/test_journal_conformance.py`（89 项）：三种后端形态 × 22 项 core 检查全部通过；两种存储 /
  锁适配器 × 7 项检查通过；四个有缺陷的后端被对应的 case 查出；参考实现只用公共名字。
- `tests/conversation/journal/test_backend_toolkit.py`（15 项）：构件与 JSONL core 逐字段一致、坏批次、
  幂等规则、篡改与 epoch 谱系的校验、索引重建。
- `tests/loop/test_audit_external_journal_core.py`（3 项）：同一段审计会话（挂起 → 释放 → 接管 → `Resume` →
  终结）在参考实现与「JSONL core + 外部存储适配器」上照样成立，链可校验，本机没有 Journal 文件；writer
  存活时第二个 pool 被拒。
