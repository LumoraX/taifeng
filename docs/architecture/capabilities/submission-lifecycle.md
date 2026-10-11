# 提交生命周期

## 准入与取消

legacy UserMessage 在 `submit()` 返回之前进入 pending 登记，排队也可取消。
执行入口保留排队期间收到的取消原因，已取消操作不得启动 turn。
审计接受与取消仍由 durable coordinator 管理，不扩展审计的挂起丢弃语义。
准入登记只表示已接受，不表示正在执行；运行中输入注入不能落到尚未派发的提交。
排队取消以既有 `turn_failed` 事件收尾，`data.kind=cancelled`。

## 挂起交接

runner 发出 `turn_suspended` 后，pending 所有权保持到 history 回写完成。
窗口内的 CancelTurn 由该所有者在回写后处理，避免旧 buffer 覆盖核销 marker。
回写期间排入的残余输入再次持久化；回写后关闭输入队列，新输入直接进入共享历史。
取消登记保持到这一收尾过程完成，随后释放，不丢用户或系统注入。
legacy 挂起取消成功发出 `suspension_cancelled`，归属于 CancelTurn 提交，包含
`record_id`、`thread_id`、`target_submission_id`、`reason=cancelled`；撤销对应 TTL。

## Resume 的提交终态

`suspension_partially_resolved` 与 `suspension_resolve_rejected` 是本次 Resume 的终态。
根 Resume 或直接 spawn Resume 中止时，`suspension_resolved.resumed=false` 为终态；
续跑时为 true，等待后续 turn 事件。嵌套子链的核销仍是中间事件，不提前结束父链。
终态供在线订阅自动结束与晚到订阅重放共同使用，不虚构 turn。

## 核销幂等

先验证 resolutions 为当前活跃 record 的非空已知请求集合，再过滤已核销请求。
混合新旧请求只执行未核销部分；全部已核销拒绝为 `already_resolved`。
空集合与未知 ID 仍拒绝。已完整结束的 record 不扫描历史来改变 `no_active_suspension`。

## 实际用量与完整性

runner 终态增加 `usage_complete: bool`：每次开始的采样都有 provider usage 才为 true。
采样中取消、失败或重试中的 attempt 没有 usage 时为 false；此前实际 usage 保留。尚未采样即取消
不产生未知模型用量；工具执行中取消不改变已收到的采样用量。
字段仅说明当前 turn 本段的实际用量是否完整，不代表 subtree 或账单估算。
会话计量和 token 上限继续只用实际 usage，不加入估算值。

实现边界：`TurnSample.sample_once` 保留 ADR 0038 的采样热路径例外，
本次源码起止行上限 259 行（请求/流分派/工具派发仍分离），`turn_sample.py` 上限 800 行；
计数只用于事实标记，详见 ADR 0121。
