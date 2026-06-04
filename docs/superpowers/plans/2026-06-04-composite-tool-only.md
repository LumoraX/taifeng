# Composite Tool-Only 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 放松 composite 校验，允许 "只有 tool_names、没有 child_skills" 的 tool-only composite 叶子合法，让需要 `request_user_input` 等工具的子 skill 无需捏 dummy 子 skill。

**Architecture:** 改 `SkillDefinition.validate()` composite 分支唯一一处：把"必须非空 child_skills"换成"child_skills 与 tool_names 不可同时为空"（变体 A）。其余文件零改动（空 child_skills 在迭代/成员判断处天然安全）。配套 loader 级测试、e2e 挂起续跑测试、ADR + 活文档同步。

**Tech Stack:** Python 3.12 / pydantic dataclass / pytest（`asyncio_mode=auto`）/ MockClient。所有测试 `PYTHONPATH=src uv run pytest ...`，不调真实 API。

**分支：** 已在 `feat/composite-tool-only`（spec 已 commit）。

**关联 spec：** `docs/superpowers/specs/2026-06-04-composite-tool-only-design.md`

---

## File Structure

| 文件 | 职责 | 动作 |
| --- | --- | --- |
| `src/taifeng/skill/definition.py` | `SkillDefinition.validate()` 校验规则 | Modify（composite 分支 + 类 docstring 注释） |
| `tests/skill/test_skill.py` | loader/validate 单测 | Modify（新增 tool-only 通过用例） |
| `tests/test_tool_only_composite_resume.py` | tool-only composite 端到端挂起续跑 | Create |
| `docs/decisions/0013-composite-tool-only.md` | ADR 决策记录 | Create |
| `docs/architecture/capabilities/skill-dispatch.md` | 能力契约活文档 | Modify（:233 一行） |
| `docs/architecture/skill-system.md` | skill 模块活文档 | Modify（validate 片段补 composite 分支） |

---

## Task 1: 放松 composite 校验（核心代码改动）

**Files:**
- Modify: `src/taifeng/skill/definition.py`（composite 分支，当前约 124-132 行；类 docstring 约 62-69 行）
- Test: `tests/skill/test_skill.py`

- [ ] **Step 1: 写失败测试 —— tool-only composite 应被接受**

在 `tests/skill/test_skill.py` 末尾追加（紧邻既有 `test_composite_missing_child_skills_rejected`，与其形成"接受/拒绝"对照）：

```python
def test_tool_only_composite_accepted(tmp_path: Path) -> None:
    """composite 仅声明 tool_names（无 child_skills）应通过校验 —— 变体 A：有 agency 即合法。"""
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "SKILL.md").write_text(
        """---
name: ok
description: x
type: composite
tool_names: [request_user_input]
---
body
""",
        encoding="utf-8",
    )
    skills = load_skills_from_dir(tmp_path)
    # load_skills_from_dir 返回 {id: SkillDefinition}；tool-only composite 正常载入
    assert skills["ok"].type == "composite"
    assert "request_user_input" in skills["ok"].tool_names
    assert not skills["ok"].child_skills
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `PYTHONPATH=src uv run pytest tests/skill/test_skill.py::test_tool_only_composite_accepted -v`
Expected: FAIL —— `SkillValidationError`（当前 composite 强制 child_skills 非空）。

- [ ] **Step 3: 改 `validate()` composite 分支**

在 `src/taifeng/skill/definition.py` 把 composite 分支：

```python
        elif self.type == "composite":
            if not self.child_skills:
                raise SkillValidationError(
                    f"composite skill {self.id!r} 必须声明 child_skills"
                )
            if self.max_call_depth < 1:
                raise SkillValidationError(
                    f"composite skill {self.id!r} max_call_depth 必须 >= 1"
                )
```

改为：

```python
        elif self.type == "composite":
            # composite = 有 agency 的 skill：可调子 skill、可调工具，二者至少其一。
            # 两者皆空 = 戴帽子的 atomic（无意义空壳）→ fail-fast 拒绝。
            # （参照 ADR 0013：放松"必须有 child_skills"为"二者至少其一"。）
            if not self.child_skills and not self.tool_names:
                raise SkillValidationError(
                    f"composite skill {self.id!r} 必须至少声明 child_skills 或 tool_names 之一"
                )
            if self.max_call_depth < 1:
                raise SkillValidationError(
                    f"composite skill {self.id!r} max_call_depth 必须 >= 1"
                )
```

- [ ] **Step 4: 同步类 docstring**

把类 docstring 里描述 composite 字段约束的句子（约 62-69 行）更新，使其反映新规则。将：

```python
    """统一 skill 描述符。

    Atomic 与 composite 通过 ``type`` 字段区分。Composite 独有字段
    （``child_skills`` / ``tool_names`` / ``max_call_depth`` / ``model``）
    在 atomic 上必须留空，由 ``validate()`` 强制。
    """
```

改为：

```python
    """统一 skill 描述符。

    Atomic 与 composite 通过 ``type`` 字段区分。Composite 独有字段
    （``child_skills`` / ``tool_names`` / ``max_call_depth`` / ``model``）
    在 atomic 上必须留空；composite 则需 ``child_skills`` 或 ``tool_names``
    至少其一非空（tool-only composite 合法，见 ADR 0013），均由 ``validate()`` 强制。
    """
```

- [ ] **Step 5: 跑测试确认通过 + 旧拒绝用例仍绿**

Run: `PYTHONPATH=src uv run pytest tests/skill/test_skill.py -v`
Expected: PASS —— `test_tool_only_composite_accepted` 通过；`test_composite_missing_child_skills_rejected`（两者皆空）仍 PASS（变体 A 仍拒绝空壳）；`test_atomic_with_child_skills_rejected` 等其余不受影响。

- [ ] **Step 6: 跑全量 skill 测试确认无回归**

Run: `PYTHONPATH=src uv run pytest tests/skill/ -q`
Expected: 全绿。若有红，先排查是否与本改动相关，禁止以 pre-existing 跳过。

- [ ] **Step 7: lint + 类型检查**

Run: `uv run ruff check src/taifeng/skill/definition.py tests/skill/test_skill.py && uv run mypy src/taifeng/skill/definition.py`
Expected: 无报错。

- [ ] **Step 8: Commit**

```bash
git add src/taifeng/skill/definition.py tests/skill/test_skill.py
git commit -m "feat(skill): 放松 composite 校验为 child_skills 或 tool_names 至少其一

允许 tool-only composite（只有工具、无子 skill 的叶子），无需捏 dummy 子 skill。
两者皆空仍 fail-fast 拒绝。回归 ADR 0006 本意，详见 ADR 0013。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 文档落档（ADR + 活文档）

> 项目红线：实现完成但文档未同步 → 不合并。本任务与 Task 1 紧邻，可同一会话连做。

**Files:**
- Create: `docs/decisions/0013-composite-tool-only.md`
- Modify: `docs/architecture/capabilities/skill-dispatch.md:233`
- Modify: `docs/architecture/skill-system.md`（validate 代码片段，约 160-169 行）

- [ ] **Step 1: 写 ADR 0013**

Create `docs/decisions/0013-composite-tool-only.md`：

```markdown
# ADR 0013: 放松 composite 校验 —— 允许 tool-only composite

- 状态：Accepted
- 日期：2026-06-04
- 关系：澄清 ADR 0006 本意（非推翻）

## 背景

ADR 0006 把 skill 分为 atomic / composite，atomic 禁止声明 `tool_names`，
故"只想调工具的叶子"必须升为 composite。而 `definition.py::validate()` 又
强制 composite 的 `child_skills` 非空，导致这类工具型叶子被迫凭空捏一个 dummy
子 skill 才能过校验（见 `tests/test_child_suspend_resume.py` 的 leaf-noop 占位）。

关键：ADR 0006 的数据结构只把 `child_skills` / `tool_names` 标为"composite 特有
字段（atomic 留空）"，**从未要求 composite 必须有非空 child_skills**。该约束是
`validate()` 后加的实现细节，并非决策本身。

## 决策

composite 的合法条件由"必须有 child_skills"放松为
**"`child_skills` 或 `tool_names` 至少其一非空"**。两者皆空 = 戴帽子的 atomic
（无意义空壳）→ 仍 fail-fast 拒绝。

`request_user_input` 维持普通工具语义，经 `tool_names` 显式授予，不引入任何内置
原语（保持"无配置即纯 LLM 调工具"范式）。

排除备选：
- 完全去掉非空要求（放进无子无工具空壳）—— 违背 fail-fast 调性。
- 改为允许 atomic 声明 tool_names —— 与"atomic = 纯内容、无 agency"定位相悖。

## 后果

- composite 语义从"有子 skill"修正为"有 agency（工具和/或子 skill）"，更贴合
  ADR 0006 本意。
- 工具型叶子（如分析 + `request_user_input` 采集）可写成 tool-only composite，
  不再需要 dummy 子 skill。
- atomic 约束不变（仍禁工具）。

## 相关

- ADR 0006（统一 skill 模型）
- 架构：`docs/architecture/skill-system.md`、`docs/architecture/capabilities/skill-dispatch.md`
```

- [ ] **Step 2: 改 skill-dispatch.md:233**

把该行：

```
- composite: 必须声明 child_skills（>=1）；可作为 entry（``entry: true``）
```

改为：

```
- composite: 必须声明 child_skills 或 tool_names 至少一个（tool-only composite 合法，见 ADR 0013）；可作为 entry（``entry: true``）
```

- [ ] **Step 3: 改 skill-system.md 的 validate 片段**

`docs/architecture/skill-system.md` 约 160-169 行的 `validate()` 示例只渲染了 atomic
分支。在 atomic 分支后补上 composite 分支，体现新规则（注意这是文档示例片段，缩进/风格
与该文件现有片段一致）：

```python
        if self.type == "atomic":
            # atomic 不可声明 child_skills / tool_names，也不可作为 entry（无豁免位）
            if self.child_skills:
                raise SkillValidationError(f"atomic skill {self.id!r} 不能声明 child_skills")
            if self.tool_names:
                raise SkillValidationError(f"atomic skill {self.id!r} 不能声明 tool_names")
            if self.entry:
                raise SkillValidationError(f"atomic skill {self.id!r} 默认不可作为 entry")
        elif self.type == "composite":
            # composite = 有 agency：child_skills 或 tool_names 至少其一非空（ADR 0013）
            if not self.child_skills and not self.tool_names:
                raise SkillValidationError(
                    f"composite skill {self.id!r} 必须至少声明 child_skills 或 tool_names 之一")
```

> 注意：若该文件附近还有叙述性文字声称"composite 必须有 child_skills"，一并改成新规则。

- [ ] **Step 4: Commit**

```bash
git add docs/decisions/0013-composite-tool-only.md docs/architecture/capabilities/skill-dispatch.md docs/architecture/skill-system.md
git commit -m "docs(skill): ADR 0013 + 活文档同步 composite tool-only 规则

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: e2e —— tool-only composite 子 skill 挂起续跑

> 独立 commit 切片（spec §5 提示）。faithfully 复刻用户真实场景：父 LLM 驱动派子，
> 子是 tool-only composite，子内 `request_user_input` 挂起 → Resume → 续跑回传父 → 根完成。
> 结构参照 `tests/test_child_suspend_resume.py`，但子 skill **无 dummy child**（本特性的核心证据）。

**Files:**
- Create: `tests/test_tool_only_composite_resume.py`

**前置事实（实现时已知，勿再查）：**
- `request_user_input` **不是**默认注册工具（`pool.py` 仅注册 read_skill/call_skill/run_script），
  须经 `EnginePool.create(..., extra_tools=[make_request_user_input_tool()])` 注入。
- `request_user_input` handler 抛 `SuspendSignal(PendingRequest(reason=DATA,
  request_id=ctx.call_id, related_call_id=ctx.call_id, ...))`；Resume 用
  `resolutions={request_id: <payload>}` 回填该 call 的 function_call_output。
- 子挂起落子 thread；`turn_suspended.data["thread_id"]` = 子 thread；`Resume(thread_id=子)` 续跑。
- `threads_dir` 是既有 fixture（见 `tests/test_child_suspend_resume.py` 用法）。

- [ ] **Step 1: 写 e2e 测试**

Create `tests/test_tool_only_composite_resume.py`：

```python
"""tool-only composite 子 skill 内 request_user_input 挂起 → Resume 续跑回传父 → 根完成。

本特性（ADR 0013）的端到端证据：子 skill 是 tool-only composite —— 只声明
tool_names: [request_user_input]、无 child_skills（对比 test_child_suspend_resume.py
里为过校验而捏的 leaf-noop dummy 子 skill，本测试证明那已不再需要）。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng.suspend.record import SuspensionRecord

if TYPE_CHECKING:
    from pathlib import Path

# 父 entry：LLM 驱动，派发子 skill，自身不挂工具
_PARENT_SKILL = """---
name: parent-flow
description: 父流程
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [intake-analyzer]
tool_names: []
max_call_depth: 3
---
# 父流程 PARENT_MARK
派发子 skill 完成分析。
"""

# 子 skill：tool-only composite —— 仅 tool_names、无 child_skills（本特性核心形态）
_CHILD_SKILL = """---
name: intake-analyzer
description: 采集分析子单元
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 采集分析 CHILD_MARK
缺数据时调 request_user_input 向用户采集，补齐后给结论。
"""


def _build_skills(tmp_path: Path) -> Path:
    """内联写出 parent-flow(entry) + intake-analyzer 两个 skill（无 dummy 叶子）。"""
    skills = tmp_path / "tool_only_skills"
    for sub, body in (
        ("parent-flow", _PARENT_SKILL),
        ("intake-analyzer", _CHILD_SKILL),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


def _routing_client():
    """父首轮 call_skill 派子；子首轮 request_user_input（挂起）；各自第 2 轮纯文本完成。"""
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient

    return RoutingMockClient(routes={
        "PARENT_MARK": [
            MockTurn(text="派发子 skill", tool_calls=[
                {"id": "c_call", "name": "call_skill",
                 "arguments": '{"skill_id": "intake-analyzer", "reason": "analyze"}'},
            ]),
            MockTurn(text="父流程完成。"),
        ],
        "CHILD_MARK": [
            MockTurn(text="子向用户采集", tool_calls=[
                {"id": "call_rui1", "name": "request_user_input",
                 "arguments": '{"prompt": "请补充近三月体检报告"}'},
            ]),
            MockTurn(text="子分析完成 CHILD_DONE_MARK"),
        ],
    })


class _AllEventsRecorder:
    """后台 subscribe_all 收集器 —— submit 前启动，按 (submission_id, 终结判据) 等目标终结。"""

    def __init__(self, engine) -> None:
        self._events: list = []
        self._task = asyncio.create_task(self._run(engine))

    async def _run(self, engine) -> None:
        async for ev in engine.subscribe_all():
            self._events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    async def wait_terminal(self, sub_id: str, *, timeout_s: float = 8.0) -> list:
        async def _poll() -> list:
            while True:
                got = [e for e in self._events if e.submission_id == sub_id]
                for e in got:
                    k = e.msg.kind
                    if k == "turn_suspended":
                        return got
                    if k in ("turn_completed", "turn_failed") and e.msg.data.get("is_root"):
                        return got
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(_poll(), timeout=timeout_s)


@pytest.mark.asyncio
async def test_tool_only_composite_child_suspend_resume(tmp_path: Path, threads_dir):
    """tool-only composite 子 skill request_user_input 挂起 → Resume(子 thread) → 根完成。"""
    import taifeng
    from taifeng.loop.submission import Resume
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    skills = _build_skills(tmp_path)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills,
        threads_dir=threads_dir,
        model_client=_routing_client(),
        compressors=[],
        extra_tools=[make_request_user_input_tool()],  # request_user_input 非默认注册
    )
    engine = await pool.get_or_create(
        session_id="tool-only-e2e", entry_skill_id="parent-flow",
    )
    root_thread_id = engine.thread_id

    recorder = _AllEventsRecorder(engine)
    await asyncio.sleep(0)  # 让 subscribe_all 注册队列

    # === 第一阶段：父派子，子内 request_user_input 挂起 ===
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await recorder.wait_terminal(sub_id)
    assert events1[-1].msg.kind == "turn_suspended", \
        f"应以挂起收尾，实得 {[e.msg.kind for e in events1]}"

    suspend_ev = next(ev for ev in events1 if ev.msg.kind == "turn_suspended")
    child_thread_id = suspend_ev.msg.data["thread_id"]
    assert child_thread_id != root_thread_id, "子挂起的 thread_id 必须是子 thread"

    # 从子 thread suspension record 取 request_id（DATA 挂起：request_id == related_call_id == call_rui1）
    child_items = [it async for it in await pool.store.load_thread(child_thread_id)]
    suspension_items = [it for it in child_items if it.kind == "suspension"]
    assert len(suspension_items) == 1, "子 thread 应落且仅落一条 suspension"
    rec = SuspensionRecord.from_item(suspension_items[0])
    req_id = rec.pending[0].request_id
    assert rec.pending[0].related_call_id == "call_rui1"

    # === 第二阶段：Resume(子 thread) 回填表单答案 → 续跑 ===
    resume_sub = await engine.submit(Resume(
        thread_id=child_thread_id,
        resolutions={req_id: {"report": "已上传"}},
    ))
    events2 = await recorder.wait_terminal(resume_sub)
    kinds2 = [ev.msg.kind for ev in events2]

    await pool.close()

    # (a) 子挂起被定位并 resolve
    assert "suspension_resolved" in kinds2, f"未见 suspension_resolved，实得 {kinds2}"
    # (b) 续跑回传父 → 整个 submission 以根 turn_completed 收尾
    root_completed = [
        ev for ev in events2
        if ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root")
    ]
    assert root_completed, f"续跑应回传父并以根 turn_completed 收尾，实得 {kinds2}"

    # (c) 子 thread 续跑输出落盘 + 被挂起 call 补回 function_call_output
    child_items2 = [it async for it in await pool.store.load_thread(child_thread_id)]
    blob = " ".join(str(it.payload) for it in child_items2)
    assert "CHILD_DONE_MARK" in blob, "子 thread 续跑后的输出必须落盘"
    fco_ids = {
        it.payload.get("call_id") for it in child_items2
        if it.kind == "function_call_output"
    }
    assert "call_rui1" in fco_ids, "被 resolve 的挂起 call 必须补回 function_call_output"
```

- [ ] **Step 2: 跑 e2e 确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_tool_only_composite_resume.py -v`
Expected: PASS。
若 FAIL，按现象排查：
  - `SkillValidationError`（子 skill 加载失败）→ 说明 Task 1 未生效或子 skill frontmatter 写错。
  - `no_active_suspension` / 无 `suspension_resolved` → 检查 `child_thread_id` 是否取自 `turn_suspended.data["thread_id"]`。
  - 工具未找到 → 确认 `extra_tools=[make_request_user_input_tool()]` 已传。

- [ ] **Step 3: lint**

Run: `uv run ruff check tests/test_tool_only_composite_resume.py`
Expected: 无报错。

- [ ] **Step 4: Commit**

```bash
git add tests/test_tool_only_composite_resume.py
git commit -m "test(skill): tool-only composite 子 skill request_user_input 挂起续跑 e2e

证明 tool-only composite（无 dummy 子 skill）在 LLM 驱动父下可挂起 + Resume 续跑回传父。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 全量验证

- [ ] **Step 1: 跑全量测试**

Run: `PYTHONPATH=src uv run pytest tests/ -q`
Expected: 全绿。复述实际命令 + 关键输出（通过数 / 失败数）。红测试禁止以 pre-existing 跳过——先排查是否与本改动相关。

- [ ] **Step 2: lint + 类型全量**

Run: `uv run ruff check src/ tests/ && uv run mypy src/`
Expected: 无报错。

- [ ] **Step 3: 收尾确认**

确认四象限文档已落位：ADR 0013 创建、skill-dispatch.md / skill-system.md 已同步、spec 与 plan 在 `docs/superpowers/`。分支 `feat/composite-tool-only` 上共应有：spec commit（已存在）+ Task1（feat）+ Task2（docs）+ Task3（test）。

---

## Self-Review（计划自检）

- **Spec 覆盖**：§2 决策 → Task 1；§3.1 代码 → Task 1 Step 3；§5 测试三项 → Task 1 Step 1（accepted）+ 既有 rejected 用例（Task 1 Step 5 验证保持绿）+ Task 3（e2e）；§6 文档四项 → Task 2；§7 R1–R5 → 无代码路径变化，Task 4 全量回归兜底。全覆盖。
- **Placeholder 扫描**：无 TODO/TBD；每个代码步给出完整代码。
- **类型/命名一致**：测试中 `request_user_input` call_id 全程为 `call_rui1`；`SuspensionRecord.from_item`、`pool.store.load_thread`、`Resume(thread_id=, resolutions=)` 与既有 `test_child_suspend_resume.py` 完全一致；`make_request_user_input_tool` 名称取自 `tool/builtins/__init__.py` 的 `__all__`。
