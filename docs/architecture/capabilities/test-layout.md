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
- SHALL NOT 让该期限同时圈住**真实 IO**（fsync / 真 journal 写）：期限要小到能被人为构造的慢路径
  确定性触发，就必然小到会被慢盘偶发撞上。做法是**分段**——真实 IO 阶段给足期限，进入被测阶段前
  再收紧。

依据（2026-09 实测）：8 个不同用例曾轮流因守卫期限过紧而间歇变红，每次红的用例都不一样，其中一次
把 main 的 CI 跑红；孤立跑全绿、只在全量跑（2300+ 用例单进程累积负载）复现。典型误用是
`commit_timeout=0.01` 同时圈住了建会话的两次真 fsync。
