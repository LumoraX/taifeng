## ADDED Requirements

### Requirement: 引用形态的驱逐不落盘
被驱逐的附件是引用形态时，`MultimodalEvictionStrategy` 与 `HandoffCompactionStrategy` SHALL NOT 向工作区写入任何文件，SHALL NOT 读取附件存储；描述里的指针 SHALL 为 `path=attachment:<ref>`。内联形态的行为（ADR 0147）不变。

#### Scenario: 驱逐引用形态的图片
- **WHEN** 一条带引用形态图片的用户消息被驱逐，策略配置了工作区
- **THEN** 工作区 SHALL 无新增文件，描述 SHALL 含 `path=attachment:<ref>`，`detail.offloaded_attachments` SHALL 不计入该附件

#### Scenario: 未配置工作区也可恢复
- **WHEN** 策略未配置 `file_root` / `workspace`，被驱逐的附件是引用形态
- **THEN** 描述 SHALL 仍含 `path=attachment:<ref>`

#### Scenario: 混合形态
- **WHEN** 同一条目上一个内联附件与一个引用附件同时被驱逐，策略配置了工作区
- **THEN** 内联附件 SHALL 落盘并得到 `_offload/...` 路径，引用附件 SHALL 得到 `attachment:<ref>`，两条描述在同一行内按原顺序排列

### Requirement: 指针收集识别两种路径
`collect_attachment_pointers` 与 handoff 的 `<offloaded_attachments>` 块 SHALL 同时保留 `_offload/...` 与 `attachment:<ref>` 两种指针，去重键为指针本身。

#### Scenario: 指针跨多轮摘要保留
- **WHEN** 含 `attachment:<ref>` 指针的压缩条目再次被 handoff 摘要
- **THEN** 新压缩条目的指针块 SHALL 仍含该指针
