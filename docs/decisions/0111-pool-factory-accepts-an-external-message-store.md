# ADR 0111：`EnginePool.create` 接受外部 `MessageStore`

- 状态：Accepted
- 日期：2026-09-30
- 关联：[conversation](../architecture/conversation.md)、[jsonl-transcript](../architecture/capabilities/jsonl-transcript.md)；ADR 0110

## 背景

`MessageStore` 是稳定层的协议，文档说「业务侧落 DB 自行实现 `MessageStore` 协议」。但生产入口
`EnginePool.create` 自己 `JsonlMessageStore(storage_dir)`，没有注入口：想换主存只能绕开工厂、手工
`EnginePool(skill_registry=, store=, tool_registry=, ...)`，把工厂替调用方做的事（加载 skill、默认重试
包装、注册内置工具、组装压缩器、hook 转发、skill 热更）全部自己重做一遍。

多实例部署因此只能把会话主存留在节点本地磁盘，跨节点接管要靠共享卷。

命中 ADR 0017 规则③（内核只定协议，实现走外部）——协议早就有，缺的是入口。

## 决策

1. **`EnginePool.create(message_store=)`**：注入后不再建 `JsonlMessageStore`，不碰本机目录。与
   `storage_dir` / `threads_dir` 互斥（注入的 store 自己决定数据落在哪里）；两者都不给仍是错误。
2. **`index_hook` 照常生效**。注入的 store 同样包在 hook 转发层里；`thread_directory` 不给时用
   `NullThreadDirectory`（hook 的元数据入参由转发层合成）。
3. **所有权随构造成功转移**。池建成后拥有这个 store，`close()` 时关闭它，与直接构造
   `EnginePool(store=)` 的行为一致。构造失败（坏配置、能力门禁拒绝）时池不存在，不替调用方关。
4. **坏配置在拉起任何资源之前拒绝**：
   - 对象不满足 `MessageStore` → `TypeError`；
   - 模型客户端走 Responses 协议而 store 没有实现 `AtomicBatchMessageStore` →
     `UnsupportedPersistenceCapabilityError`。此前这项检查只看包装后的 store，而 hook 转发层自己有
     `append_atomic_batch` 方法，检查恒真，缺口要到第一次采样的终态提交才暴露。
5. **审计模式不变**：对话投影只支持默认 JSONL store，注入外部 store 仍被能力门禁以
   `audit_custom_store_unsupported` 拒绝（共享 Journal 后端是另一条线，见 ADR 0114）。

## 不做

- **让调用方保留所有权（池不关注入的 store）**：同一个对象经工厂与经构造函数两种注入方式行为不同，
  是更糟的惊喜；要复用连接池的实现把 `close()` 写成不释放共享资源即可。
- **注入 store 时自动配一个默认 `ThreadDirectory`**：默认的 SQLite 索引落本机目录，与「不碰本机」
  相悖；需要线程目录的宿主显式给。

## 影响

- R1：无业务概念。
- R2–R4：无影响。
- R5：resume 走 `MessageStore.load_thread`，对外部 store 的要求不变——按写入顺序、完整吐回。

### 行为变化

- `EnginePool.create` 多一个可选参数；不给 `storage_dir` / `threads_dir` 的错误文本多提一句
  `message_store`。

## 验证

`tests/loop/test_pool_external_store.py`（10 项，只用稳定层名字写成）：对话落进外部 store、本机不产生
任何文件、池关闭时关 store 且只关一次；从外部 store 恢复一个 thread 续聊；`index_hook` 与
`thread_directory` 照常工作；与 `storage_dir` 互斥、两者都不给、对象不是 `MessageStore` 三种坏配置；
Responses 客户端配非原子 store 在构造时拒绝且不关 store，配原子 store 时终态输出经
`append_atomic_batch` 落库；审计模式拒绝外部 store 且不关它。
