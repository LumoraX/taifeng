## Context

用户已确认终端执行树第一版。基线 main=6c51223，独立 feat/execution-observability worktree；基线测试4141 passed/17 skipped。

## Goals / Non-Goals

目标为单engine事件流可消费的执行树、状态、当前工具、耗时、用量；不引入额外调度/存储体系。

## Decisions

1. 复用EventMsg而非另一条总线；execution为可选冻结结构，旧事件缺省None。身份不进入LLM请求。
2. 节点execution_id采用thread_id以自然跨Resume更新，attempt_id在每次run新建；同步与detached子线程本就隔离，同Skill并发不共用节点。
3. 使用按session隔离的ContextVar传播父身份。run装饰器建立/清理作用域；TurnRunner._emit显式使用owner身份，EngineEvents补齐处于作用域内的协调事件。无需扩展spawn工具的参数或Journal。
4. 仅在采样开始和usage入账后发execution_updated，前者标估算上下文，后者仅在provider明确返回有效input_tokens时标实测，缺失时保持估算。工具与挂起/终态由原事件驱动，既有调度路径不改。
5. spawn协调事件按handle映射更新目标子节点（含尚未起跑/挂起后被取消）；状态投影与终端显示置telemetry，单流seq去重/跳号检测、节点容量上限、不可变快照，终端防控制字符并有界遍历。projection是观察结果，不成为内核真相。
6. 不采用逐条解析Console文本；不强依赖Rich/Textual，TTY原地重绘，非TTY追加输出，write/flush在线程执行并允许取消等待。宿主可以直接取snapshot复用到SSE/UI。
7. turn.py=794行、turn_sample.py=791行已接近上限：新增观测函数放独立模块，旧_emit下沉释放空间；不扩大既有超长采样函数。

## Risks / Trade-offs

- 作用域传播错误导致跨会话串线：session key过滤、finally恢复、并发测试。
- Resume新attempt重复计量：按attempt累计绝对值，不用subtree_usage；同attempt终态不可被迟到事件复活。
- 晚订阅/丢事件/容量截断：明确标记incomplete，不猜完整树；宿主应在提交前attach。
- ContextVar只保留运行中观测：冷恢复父级未知可接受，已有投影保留已知父级；不修改持久恢复机制。
- 终端输出阻塞：TextIO.write/flush放在线程并允许取消等待，actor仍非阻塞广播；底层已开始的写入可能稍后结束，渲染有行数/字符限制。

## Migration Plan

原订阅者继续读取原字段；新宿主用experimental.attach_terminal_tree_sink(engine)，也可直接维护ExecutionProjection。回滚删除新增sink接线即可，不涉及数据迁移。
