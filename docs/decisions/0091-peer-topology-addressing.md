# ADR 0091：peer 拓扑路径寻址——按对方跑的 skill 指代它

- 状态：Accepted
- 日期：2026-09-30
- 关联：[peer-mailbox-messaging 契约](../architecture/capabilities/peer-mailbox-messaging.md)；
  [detached-spawn](../architecture/capabilities/detached-spawn.md)；ADR 0015 / 0017

## 背景

peer 消息只能按 thread id、句柄 id 或 `parent` 寻址。前两者是运行时才产生的：兄弟专家之间要互发消息，
得由协调者把每个句柄写进派发参数转告，skill 作者无法在提示词里直接写「把结果发给复核专家」。
ADR 0017 曾把拓扑路径寻址判为「挂起等需求」；本轮由项目负责人明确要求实现。

命中 ADR 0017 规则①（内核机制缺口：寻址是投递机制的一部分）。

## 决策

1. **地址写法：`sibling:<skill_id>`、`child:<skill_id>`，可加 `#<n>`**；另加 `root` 作为 `parent` 的同义词。
   skill id 是作者写提示词时就确定的名字。
2. **关系名必须与发送方相符**：root 没有兄弟，child 不寻址「子」。不符时显式拒绝并提示应改用的写法，
   不做自动纠正——自动纠正会让写错的提示词看起来能用，换个发送方就失效。
3. **多个实例时不猜**。同一 skill 被派发多次很常见（并发铺开同一种专家）；未写 `#<n>` 而匹配到多个时
   拒绝，并列出各候选的句柄 id。序号按派发先后，从 1 起。
4. **`cancelled` / `error` 的实例不参与拓扑寻址**。否则同一 skill 先失败后重派时，地址会因为残留的
   失败实例而变得有歧义。它们仍可按句柄 id 直接寻址。`done` 的实例参与——它是空闲的，可以被唤醒。
5. **发送方不算自己的兄弟**。
6. **解析是纯函数，读句柄表当时的状态**。不引入订阅或「地址绑定」：消息投给解析时的那个 thread。
7. **经拓扑地址投递时，返回值与事件带上原始地址**（`address`）。审计时能看出发送方是怎么找到对方的；
   直接寻址不带这个键，既有事件形状不变。

## 替代方案

- **统一成 `skill:<skill_id>`，不区分关系**：少一种写法。但谱系将来若不再是「root + 一层 child」
  （嵌套派发各自成树），不带关系的地址无法表达「只找我这一层的」；关系名现在就定下来，语义不必再改。
- **路径式地址（`/coordinator/reviewer`）**：表达力更强，但当前谱系是平的，所有 child 的 parent 都是 root，
  多级路径没有对应的结构。
- **匹配到多个时投给全部（广播）**：返回值要变成列表，唤醒与降级要逐个表达，且「发给所有复核专家」
  与「发给那个复核专家」是两种意图，不应由实例个数隐式决定。广播若有需要应是单独的写法。
- **匹配到多个时取最近派发的**：隐式规则，并发铺开时几乎必然投错。

## 后果

- 新模块 `loop/peer_address.py`；`send_message` 工具、`SendToPeer` Op、`engine.deliver_peer_message`
  都接受新写法，签名不变。
- 新的拒绝原因：`invalid_peer_address`、`peer_address_not_applicable`、`ambiguous_peer_target`；
  都以 `ValueError` 抛出，`send_message` 工具转成错误结果回给模型（turn 不失败）。
- `peer_message_sent` 事件与投递返回值在拓扑寻址时多一个 `address` 键。
- 未覆盖：广播、跨 engine 寻址、`wait_peer` / `wait_any` 按拓扑地址等待（它们仍只认句柄 id）。
- R1：地址里只有 skill id 与关系名，无业务概念。R2：不触及 prompt 与压缩。R3：事件留痕原始地址。
  R4：解析是同步纯计算。R5：不落新的持久化状态，句柄表可由 store 重建。

## 验证

`tests/loop/test_peer_topology_address.py`：见契约。
