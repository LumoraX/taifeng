# test-layout Specification

## Purpose
TBD - created by archiving change test-layout-restructure. Update Purpose after archive.
## Requirements
### Requirement: tests 目录按 src 模块对应归类

`tests/` 下 SHALL NOT 存在平铺的 `test_*.py` 文件（仅允许 `conftest.py` 与 `__init__.py`）；
所有测试文件 SHALL 落在 `tests/<module>/` 子目录，其中 `<module>` 与 `src/taifeng/<module>/` 一一对应。

#### Scenario: 根目录平铺测试零容忍
- **WHEN** 执行 `find tests -maxdepth 1 -name "test_*.py"`
- **THEN** 输出 SHALL 为空

#### Scenario: 每个测试文件属于一个 src 模块子目录
- **WHEN** 枚举 `tests/**/test_*.py`
- **THEN** 每个文件 SHALL 位于 `tests/<module>/` 下
- **AND** `<module>` SHALL 是 `src/taifeng/` 的直接子目录名

### Requirement: 子目录是正式 Python 包

每个 `tests/<module>/` SHALL 含空 `__init__.py` 让其成为正式 Python 包，
避免不同子目录同名 helper 模块冲突。

#### Scenario: 子目录 __init__.py 存在
- **WHEN** 枚举 `tests/*/` 下任一子目录
- **THEN** 该目录 SHALL 含 `__init__.py` 文件

### Requirement: pytest 不需要子目录 conftest

`tests/conftest.py` 在根目录 SHALL 自动按层级继承到所有子目录；除非有目录专属 fixture，
否则 SHALL NOT 在子目录复制 conftest。

#### Scenario: 子目录测试可用根 conftest 的 fixture
- **WHEN** 任一 `tests/<module>/test_*.py` 使用 `skills_dir` 等根 conftest fixture
- **THEN** pytest 收集 + 执行 SHALL 成功（自动从 `tests/conftest.py` 继承）

### Requirement: 跨模块测试归到主测目标

测试同时覆盖多个 src 模块时，SHALL 按「主要被测目标」判定子目录归属，
SHALL NOT 拆分单文件。

#### Scenario: permission gate 阻断 call_skill 归 permission
- **WHEN** `test_call_skill_permission.py` 测试 permission policy 对 skill_dispatch 的拒绝
- **THEN** 该文件 SHALL 位于 `tests/permission/`（主测目标 = permission policy 行为）

#### Scenario: skill 子能力测试归 skill
- **WHEN** `test_script_execution.py` / `test_script_loader.py` 测试 SKILL.md 的 scripts 子能力
- **THEN** 这些文件 SHALL 位于 `tests/skill/`（scripts 是 skill 子目录功能）

### Requirement: 迁移用 git mv 保留历史

把测试从平铺位置搬到子目录 SHALL 使用 `git mv` 而非 `cp + rm`，保证 `git log --follow`
与 `git blame` 链路完整。

#### Scenario: 迁移后历史可追溯
- **WHEN** 执行 `git log --follow tests/<module>/test_xxx.py`
- **THEN** 日志 SHALL 含该文件迁移前在 `tests/test_xxx.py` 时的所有 commit

### Requirement: 全量测试数迁移前后一致

迁移完成后，`PYTHONPATH=src uv run pytest tests/` 收集到的测试用例总数 SHALL 不变。

#### Scenario: 测试总数守恒
- **WHEN** 迁移前后分别跑 `pytest --collect-only -q tests/ | tail -1`
- **THEN** 两次输出的 "N tests collected" 中 N SHALL 相等


### Requirement: 墙钟期限区分「守卫」与「被测行为」

测试里的墙钟期限 SHALL 明确属于以下两类之一，且不得混用。

**守卫期限**（等待「本就该发生的事」，只为防止无限挂死）：

- SHALL 使用 `tests/conftest.py` 的 `GUARD_TIMEOUT_SECONDS`（默认 30s，可用环境变量
  `TAIFENG_TEST_GUARD_TIMEOUT` 整体放大），SHALL NOT 按「正常应该多快」估一个 1~5 秒的值。
  条件一满足就立即返回，给足期限不会拖慢通过路径，只在真挂死时才被用到。
- 等待某个状态成立 SHALL 用 `wait_for_condition(predicate)`，SHALL NOT 用
  `await asyncio.sleep(<估计值>)` 充当同步——后者把用例前提押在机器速度上，负载下前提凭空消失，
  症状却表现为下游超时，极难归因。
- 后台 `subscribe_all` 收集器 SHALL 在提交前确认订阅**已登记**（`engine._all_subs` 非空）再发提交；
  `asyncio.create_task` 只是排期，不保证已进入订阅。

**被测行为期限**（期限本身就是断言对象，如「commit 超期必须冻结 writer」）：

- SHALL 显式写值并注明理由，SHALL NOT 复用 `GUARD_TIMEOUT_SECONDS`。
- **人为构造的「慢」SHALL 做成无限**（阻塞到测试放行 / `sleep_forever`），SHALL NOT 用一个比期限大
  几倍的 `sleep`。前者两个方向都确定（必然超期、且必然由阻塞触发），后者把「谁先到」交给机器。
- **期限 SHALL 远大于调度抖动**（经验值 ≥ 100ms），且 SHALL NOT 圈住不属于被测范围的阶段
  （真实 IO、建会话等）——做法是**分段**：非被测阶段给足期限，进入被测阶段前再收紧。
- 有条件时 SHOULD 补一条「超期确由人为慢路径触发」的断言（如 `assert adapter.entered.is_set()`），
  把「超期了」和「因为对的原因超期」分开。

**根因（2026-09 实测，勿再按「正常应该多快」估期限）**：单进程 asyncio 套件里，墙钟时间由
**整个进程的调度延迟**主导，而不是被测对象的耗时。实测 `事件循环 → 线程池 → 回事件循环` 的往返：
空闲时中位 0.1ms，16 并发时中位 3.3ms / p90 4.6ms，**空载即可冒出 13ms 尖刺**——与 10ms 级预算同
数量级。而同一路径上的真实 IO（mkdir + 两次 fsync）中位仅 0.1~0.3ms，无论是否有磁盘争抢。
即：`commit_timeout=0.01` 名义上在测 commit 耗时，实际 98% 在测排队。

同一错误的四种外衣：过紧的守卫期限、罩住调度往返的行为期限、用 `elapsed < X` 证明并发、
用 `sleep(X)` 当同步。后果是 8 个不同用例轮流间歇变红（每次换一个，孤立跑全绿），其中一次把 main
的 CI 跑红。

### Requirement: 并发/顺序断言用结构性判据，不用墙钟

- 「是否并发」SHALL 用**峰值并发度**判定（handler 进出计数，或 `skill_dispatched`/`skill_returned`
  配对计数），SHALL NOT 用 `elapsed < X` —— 后者把「并发」等同于「快」，机器一慢就红，且**抓不到
  真回归**：信号量从 cap=2 坏成 cap=4，只要机器够快 `elapsed` 照样在上界内。
- 「A 早于 B」SHALL 用 `EventMsg.seq`（engine 在 `_emit` 入口同步分配的单调序号）或事件到达顺序，
  SHALL NOT 用两次 `time.monotonic()` 相减。
- 真正的性能回归 SHOULD 走独立 benchmark（多次采样 + 统计量），SHALL NOT 塞进单元测试的超时里：
  一个误报率 36% 的探针没有信噪比可言。
