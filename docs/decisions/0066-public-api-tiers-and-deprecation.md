# ADR 0066：公共 API 分稳定 / 实验 / 内部三层 + 弃用策略

- 状态：Accepted
- 日期：2026-09-29
- 关联：[architecture/public-api.md](../architecture/public-api.md)

## 背景

`taifeng` 顶层导出约 160 个符号，没有任何分层：成熟接口与契约仍标 🧪 的能力（strict audit Journal、回放、MCP HTTP 等）混在一起，
也没有弃用机制——代码里零 `DeprecationWarning`。下游按 PyPI 版本钉住使用内核，任何顶层符号的改名或删除都会让下游在升级时直接炸掉，
而内核自己也无从知道哪些符号是承诺过的。

## 决策

1. 三层：顶层 `__all__` = 稳定层；新增 `taifeng.experimental` = 实验层（契约 🧪 的入口）；其余子模块符号 = 内部。
2. 稳定层快照：`tests/public_api_snapshot.txt` 记录顶层 `__all__`，增删都要同步修改快照，让 API 变化成为评审可见的有意决定。
3. 弃用机制：`_deprecation.DEPRECATED_ALIASES` + 顶层 `__getattr__`，旧名照常可用并发 `DeprecationWarning`；弃用期 ≥ 两个发布版本且 ≥ 30 天。
4. 本分支内新增、尚未发布且契约为 🧪 的 `ReconcileVerdict` 从顶层移到实验层（未发布，无需弃用期）。其余已发布的顶层符号不动。
5. 实验层符号不能同时出现在顶层；晋升时移到顶层并在实验层保留同名导出至少一个发布版本。

## 否决的方案

- **把已发布的顶层符号按成熟度挪进实验层**：对下游是破坏性变化，收益只是分类整洁；已发布即视为稳定，今后新增的 🧪 入口先进实验层。
- **按子包 `__all__` 定义稳定性**：子包 `__all__` 目前承担模块组织职责，很多内部符号也在其中，混用会让「稳定」失去意义。
- **用 `typing_extensions.deprecated` 装饰器**：只作用于函数 / 类定义本身，无法表达「旧位置 → 新位置」的迁移，且对导入路径改变无效。

## 验证

`tests/test_public_api.py`（7 例）：快照一致、稳定层与实验层全部符号可解析、两层不重叠、弃用别名告警并返回新对象、
未知属性仍抛 `AttributeError`、弃用登记指向真实对象。
