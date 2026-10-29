## ADDED Requirements

### Requirement: offload 与回读共享可注入工作区

OffloadStrategy SHALL 接受 `workspace=` 或 `file_root=`，恰好一个；后者 SHALL 等价于 LocalWorkspaceFS。workspace 不满足 WorkspaceFS SHALL 抛 TypeError，都给或都不给 SHALL 抛 ValueError。

#### Scenario: 非本机工作区写入与分页回读
- **WHEN** 策略和 file_read 注入同一个非本机工作区并压缩超阈值结果
- **THEN** 全部 UTF-8 字节 SHALL 写入该工作区的 `_offload/{thread_id}/{call_id}`，stub 仅在成功写入后生成，file_read SHALL 能按行取回指定中段
- **AND** 本机 SHALL 不产生对应 offload 文件，item 身份、cache anchor 与计数 SHALL 保持既有语义

#### Scenario: 输入边界与路径隔离
- **WHEN** 正阈值下结果为空、未超阈值、已是 stub、带图片或位于 anchor 内
- **THEN** SHALL 保持既有跳过语义，不访问后端
- **WHEN** thread_id / call_id 为空、含路径分隔符、NUL、点目录或目标解析越界
- **THEN** SHALL 不发布 stub、不访问边界外内容，保留原始结果

#### Scenario: 单条写入失败与取消
- **WHEN** 后端写入某条结果抛 OSError
- **THEN** SHALL 记录 warning 并保留该条原文，继续处理其他候选
- **WHEN** 在写入等待期间取消
- **THEN** SHALL 传播取消，原始历史 SHALL 不被原地改写，不得发布未完成写入的 stub

#### Scenario: thread 清理
- **WHEN** cleanup_thread 指定合法 thread
- **THEN** SHALL 通过同一工作区只移除 `_offload/{thread_id}`；缺失 SHALL 幂等，其他后端错误 SHALL 传播
- **WHEN** thread_id 不安全
- **THEN** SHALL 在访问后端前拒绝并 warning，其他 thread 内容保持不变

#### Scenario: 冷恢复与并发
- **WHEN** stub 经 JSONL 后在新 store 重建，宿主重新注入原持久工作区
- **THEN** SHALL 能回读相同字节及指定行区间，重复压缩 stub SHALL 不二次写入
- **WHEN** 不同 thread 或 call_id 同时 offload
- **THEN** SHALL 写入各自独立路径，不相互串写，计数独立

#### Scenario: 调度边界不变
- **WHEN** 在有深度和环检测的子 skill 中使用 offload
- **THEN** SHALL 沿用既有调度准入，工作区注入不得引入新派发路径或改变深度与环限制
