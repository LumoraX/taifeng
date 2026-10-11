# ADR 0121：中断采样的用量完整性

状态：Accepted

## 决策

终态提供 usage_complete，实际用量保持原语义。没有 provider usage 的采样标记未知。估算与配额策略不属于本次微内核修复，宿主保留策略裁决。

## 契约

见 [提交生命周期](../architecture/capabilities/submission-lifecycle.md)。

## 有界采样例外

沿用 ADR 0038 的采样热路径拆分；本次在流分派段增加 started/metred 计数三行，
`TurnSample.sample_once` 按源码起止行核算上限 259 行，文件仍上限 800 行。
计数存放在现有采样协作者，不扩展模型协议或配额状态，不再为此切出共享流状态载体。
