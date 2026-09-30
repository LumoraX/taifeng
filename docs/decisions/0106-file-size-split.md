# ADR 0106：超长文件按「方法体外置、类里按原名赋值」拆分

- 状态：Accepted
- 日期：2026-09-30
- 关联：[loop-core-module-structure](../architecture/capabilities/loop-core-module-structure.md)；ADR 0038

## 背景

仓库红线是文件 ≤ 800 行。`loop/engine.py` 到本次拆分前已达 2092 行，`loop/event.py` 1213、
`conversation/journal/records.py` 967、`loop/pool.py` 877、`loop/spawn_driver.py` 857。ADR 0038 的
「协作者模块 + 薄委托」手法已经用尽：留在 `engine.py` 里的是公共 API、主循环与上百个 3 行的薄委托，
再抽协作者只会把薄委托换个地方。

同时有一条硬约束：`engine` / `SpawnDriver` 是唯一白盒寻址面——兄弟模块与测试按原名调用、
`monkeypatch.setattr(AgentEngine, ...)` 打桩，方法名与签名一个都不能变。

## 决策

1. **方法体外置，类里按原名赋值**。方法定义为模块级函数（`self` 标注宿主类，宿主类只在
   `TYPE_CHECKING` 下导入），宿主类里 `name = module.name`。Python 里在类体中赋值的函数就是方法，
   绑定语义、属性查找、打桩行为与直接定义完全相同；mypy 按方法检查。
2. **按职责分组**：`engine_public`（公开只读视图）、`engine_submit`（提交 / 订阅 / 关闭）、
   `engine_loop`（主循环与审计 turn）、`engine_facade`（薄委托）；`spawn_settle`（终态收敛）。
   `__init__` 与 `_new_turn_runner` 留在 `engine.py`——后者是 `TurnRunner` 符号的注入面。
3. **事件类按主题分文件**，`event.py` 保留 `Msg` 联合与 `EventMsg`，用 `as` 同名再导出全部事件类：
   既有 `from taifeng.loop.event import X` 一律不变。
4. **Journal 记录按层分文件**：基类 / 枚举 / identity / factory 进 `records_base`，对话项形状与序列化进
   `item_records`，`records.py` 保留领域 DTO 并再导出，既有 import 路径不变。
5. **零行为变更**：只搬代码、不改逻辑；全量测试作为证明。

## 考虑过的其他做法

- **mixin 基类**：`AgentEngine(_PublicMixin, _LoopMixin, ...)`。mixin 里访问 `self._x` 要么给每个 mixin
  声明上百个属性，要么放弃类型检查；赋值法不需要。
- **继续抽协作者对象**：见背景，薄委托本身就是剩余的大头。

## 影响

- 无行为变化；文件全部回到 800 行以内。
- 新增方法的位置：按上表归组，在对应模块定义、在类里赋值。

## 验证

全量 `pytest tests/` 通过（3806 passed, 17 skipped）；ruff 门禁与 `mypy src/` 清零；`verify_examples` 38/38。
