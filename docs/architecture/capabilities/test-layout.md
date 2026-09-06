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
- SHALL NOT 用 `for _ in range(N): await asyncio.sleep(P)` 做轮询——固定圈数就是把守卫期限硬编码成
  `N×P`，读代码的人看不出这是期限，调参的人也不知道该调什么，机器一慢就把「慢」误判成「没发生」。
  单纯等条件成立用 `wait_for_condition`；**边轮询边处理**（每圈扫缓冲区、按需回包）用
  `tests/conftest.py` 的 `guard_ticks(poll_seconds)` 节拍器，它按 `GUARD_TIMEOUT_SECONDS` 收口。

**被测行为期限**（期限本身就是断言对象，如「commit 超期必须冻结 writer」）：

- SHALL 显式写值并注明理由，SHALL NOT 复用 `GUARD_TIMEOUT_SECONDS`。
- **人为构造的「慢」SHALL 做成无限**（阻塞到测试放行 / `sleep_forever`），SHALL NOT 用一个比期限大
  几倍的 `sleep`。前者两个方向都确定（必然超期、且必然由阻塞触发），后者把「谁先到」交给机器。
- **期限 SHALL 远大于调度抖动**（经验值 ≥ 100ms），且 SHALL NOT 圈住不属于被测范围的阶段
  （真实 IO、建会话等）——做法是**分段**：非被测阶段给足期限，进入被测阶段前再收紧。
- 有条件时 SHOULD 补一条「超期确由人为慢路径触发」的断言（如 `assert adapter.entered.is_set()`），
  把「超期了」和「因为对的原因超期」分开。
- **上界与下界的风险不对称**：`elapsed < X` 会被调度抖动撑破（误报红）；`elapsed >= X` 只可能被
  「返回得太早」违反，机器越慢越安全。收紧存量时 SHALL 优先处理上界。
- 少数墙钟断言**不可约**，此时 SHALL 保留并注明「为什么不能换成结构判据」，且余量 SHOULD ≥ 10x
  实测值。判据是：被测量除了耗时**没有别的观测量**。例：permission prompter 超时——`reason` 里的
  秒数取自**配置值**而非实际生效值（变异验证：`fail_after` 写死 3.0 时 `reason` 照样输出 `0.1s`），
  所以「配置有没有真生效」只能由耗时暴露。

**根因（2026-09 实测，勿再按「正常应该多快」估期限）**：单进程 asyncio 套件里，墙钟时间由
**整个进程的调度延迟**主导，而不是被测对象的耗时。实测 `事件循环 → 线程池 → 回事件循环` 的往返：
空闲时中位 0.1ms，16 并发时中位 3.3ms / p90 4.6ms，**空载即可冒出 13ms 尖刺**——与 10ms 级预算同
数量级。而同一路径上的真实 IO（mkdir + 两次 fsync）中位仅 0.1~0.3ms，无论是否有磁盘争抢。
即：`commit_timeout=0.01` 名义上在测 commit 耗时，实际 98% 在测排队。

同一错误的五种外衣：过紧的守卫期限、罩住调度往返的行为期限、用 `elapsed < X` 证明并发、
用 `sleep(X)` 当同步、用固定圈数轮询（`range(N)` 即期限 `N×P`）。后果是 11 个不同用例轮流间歇
变红（每次换一个，孤立跑全绿），其中一次把 main 的 CI 跑红。

IO + CPU 风暴下交错复跑全量（两侧同机器、同依赖、同用例数）：**main 累计红 7 / 25 轮，
修复分支 0 / 25 轮**。最后一次 main 变红的原文是 `assert 1.934 < 0.5`——被测代码只花 ~0.3s，
另外 1.6s 全是调度延迟。**判读规则**：同一轮里 main 也绿的轮次不构成证据，只说明该轮没撞上
那个窗口；只有「main 红 + 修复分支绿」的轮次才有区分力。

### Requirement: 并发/顺序断言用结构性判据，不用墙钟

- 「是否并发」SHALL 用**峰值并发度**判定，统一走 `tests/conftest.py` 的 `OverlapProbe`
  （handler 里 `enter()`/`exit()`，或按 `skill_dispatched`/`skill_returned` 事件配对计数），
  SHALL NOT 用 `elapsed < X` —— 后者把「并发」等同于「快」，机器一慢就红，且**抓不到
  真回归**：信号量从 cap=2 坏成 cap=4，只要机器够快 `elapsed` 照样在上界内。
- 「A 早于 B」SHALL 用 `EventMsg.seq`（engine 在 `_emit` 入口同步分配的单调序号）或事件到达顺序，
  SHALL NOT 用两次 `time.monotonic()` 相减。
- 「已终态/已就绪时立即返回，不空转一个轮询周期」SHALL 用零等待预算（`timeout_seconds=0.0` 仍须
  返回终态结果，证明轮询圈是「先收、再判超时」）与 sleep 记账（替换**被测模块的模块级 asyncio
  名字**，不动全局，避免把并发协程的 sleep 混进来）联合判定，SHALL NOT 用 `elapsed < 轮询粒度`
  ——断言量与轮询粒度同数量级时，它和调度抖动完全重叠。
- 真正的性能回归 SHOULD 走独立 benchmark（多次采样 + 统计量），SHALL NOT 塞进单元测试的超时里：
  一个误报率 36% 的探针没有信噪比可言。
- 把墙钟断言改判为结构断言后 SHALL 做**变异验证**（人为把被测行为改坏，确认新断言必红）——
  否则容易换出一条恒真的空断言，或悄悄丢掉原断言覆盖的某类回归。
