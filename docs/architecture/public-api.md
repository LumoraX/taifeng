# 公共 API 分层与弃用策略

> 决策记录：[ADR 0066](../decisions/0066-public-api-tiers-and-deprecation.md)。本篇是现状，变更时直接更新。

taifeng 是被业务仓库按 PyPI 版本钉住使用的内核，公共 API 的边界必须明确：哪些可以放心依赖、哪些随时可能变、哪些根本不该碰。

## 三层

| 层 | 在哪里 | 兼容承诺 | 如何变更 |
| --- | --- | --- | --- |
| **稳定层** | `taifeng` 顶层 `__all__` | 不做不兼容修改；移除必须经过弃用期 | 增删都要同步改 `tests/public_api_snapshot.txt`（快照测试失败即提醒） |
| **实验层** | `taifeng.experimental.__all__` | 可在任意发布中不兼容地变化，变化记入 ADR | 契约（`docs/architecture/capabilities/`）标 🧪 的能力入口放这里；契约转 ✅ 后晋升顶层，并在本层保留同名导出至少一个发布版本 |
| **内部** | 其余子模块中未经上述两处导出的符号 | 无 | 随时可改；子包自己的 `__all__`（如 `taifeng.tool.builtins`）只表示模块内的组织，不构成稳定承诺，除非同时出现在顶层 |

当前实验层：strict audit Session 与 durable Journal（`AuditConfig`、`AuditCapabilityError`、`JsonlSessionJournalCore`）、
Journal 确定性回放（`JournalReplayClient` 等）、工具崩溃对账回查结果（`ReconcileVerdict`）、工具集动态增删与 MCP HTTP
（`McpHttpClient`、`bind_mcp_tools`、`McpToolBinding`）。

## 类型标记

包内带 `py.typed`（PEP 561），下游 mypy / pyright 直接使用 taifeng 的类型注解，实现 taifeng 协议的适配包可以在 strict 模式下检查。
`tests/test_package_metadata.py` 守护源码中的标记；`scripts/verify_release_artifacts.py` 在干净环境安装 wheel 与 sdist 后再验一次，保证发出去的产物里也有。

## 弃用流程

1. 在 `src/taifeng/_deprecation.py` 的 `DEPRECATED_ALIASES` 登记旧名 → `DeprecatedAlias(target="模块:属性", since, removal_not_before, replacement)`，
   并从顶层 `__all__` 与快照中移除该名。
2. 旧名经 `taifeng.__getattr__` 解析：照常返回新位置的对象，同时发 `DeprecationWarning`，写明起始版本、最早移除日期与替代写法。
3. 弃用期至少跨**两个发布版本**且**不少于 30 天**；期满后删除登记项。
4. 每次弃用 / 移除在对应 ADR 或发布说明中记录。

`tests/test_public_api.py` 守护：快照一致、两层导出都可解析、实验层与稳定层不重叠、弃用别名告警且指向真实对象。
