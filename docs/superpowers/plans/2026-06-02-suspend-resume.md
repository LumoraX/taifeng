# 通用挂起 / Resume 原语 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 HITL 从阻塞 await 改造成通用挂起原语——一个 turn 可在任意点产出 `TurnSuspended([pending...])` 作为正常结局并释放实例,业务侧之后凭 `thread_id` + `request_id` 提交 `Resume` Op 续跑。

**Architecture:** 挂起点抛 `SuspendSignal`(内部控制流异常,不进 `LLMError`)→ `dispatch_batch` 收集整批信号(不 fail-fast)→ `run_turn` 退栈为 `TurnSuspended` 结局,把待批 tool call 作为无 output 的 `function_call` 落盘 + 写一条 `suspension` ResponseItem(可序列化断点)→ 释放。`Resume` Op 经 `SuspensionResolver` 把 `{request_id: payload}` 配回 pending:permission→执行 tool 填 output,form/data→payload 直接成 output,system_retry→重跑 sample。复用既有 JSONL store 与 `resume-by-thread-id`。

**Tech Stack:** Python 3.12+,`anyio`,`pydantic` v2,`@dataclass(frozen=True)`,`pytest`(`asyncio_mode=auto`,全程 `MockClient`,`PYTHONPATH=src`)。

**设计依据:** `docs/superpowers/specs/2026-06-02-suspend-resume-design.md`。参照 openclaw 重入模型 + codex 协议形状(详见 spec §2)。

**通用前提(每个 `pytest` 步骤都适用):** 命令在 worktree 根目录 `.claude/worktrees/suspend-resume/` 执行;Python import 走 `PYTHONPATH=src`。

---

## 文件结构(decomposition)

| 文件 | 职责 | 动作 |
| --- | --- | --- |
| `src/taifeng/suspend/__init__.py` | 包导出 | Create |
| `src/taifeng/suspend/reason.py` | `SuspendReason` enum + `PendingRequest` | Create |
| `src/taifeng/suspend/signal.py` | `SuspendSignal` 控制流异常 | Create |
| `src/taifeng/suspend/record.py` | `SuspensionRecord` + ↔ `ResponseItem` 序列化 | Create |
| `src/taifeng/suspend/resolver.py` | `SuspensionResolver` 配对 resolution | Create |
| `src/taifeng/conversation/models.py` | 新增 `suspension` ItemKind + 构造器 | Modify |
| `src/taifeng/permission/types.py` | 新增 `SuspendingPrompter` | Modify |
| `src/taifeng/loop/tool_batch.py` | `dispatch_batch` 收集 `SuspendSignal` | Modify |
| `src/taifeng/loop/turn.py` | suspend 结局 + system_retry 转挂起 | Modify |
| `src/taifeng/loop/event.py` | 3 个新 EventMsg | Modify |
| `src/taifeng/loop/submission.py` | `Resume` Op | Modify |
| `src/taifeng/loop/engine.py` | `_handle_resume` + suspended 态 | Modify |
| `src/taifeng/__init__.py` | 顶层导出新公共类型 | Modify |
| `tests/test_suspend.py` | 单元 + 集成测试 | Create |
| `tests/test_engine_e2e.py` | e2e resume 用例 | Modify |

执行顺序自底向上:Phase 1(纯原语)→ 2(持久化)→ 3(prompter)→ 4(batch)→ 5(turn)→ **5b(request_user_input 采集工具,Task 15)**→ 6(event+Op)→ 7(resolver)→ 8(engine)→ 9(e2e+docs)。

> **与已有 openspec change 的调和(2026-06-02 决策)**:仓库主树有一份**未提交**的 openspec change `hitl-user-input-suspend-resume`(只做采集型、permission 仍阻塞、单挂起点)。决策为**本通用方案超集化并吸收它**:① 新增 Task 15 创建其定义的 `request_user_input` 内置工具(并入我们的 reason=form/data 挂起);② Task 14 把该 openspec change 改写/归档为覆盖四类挂起的超集形状。差异详见 spec 与对话记录。

---

## Phase 1 — suspend 原语(纯数据,无依赖)

### Task 1: `SuspendReason` 枚举 + `PendingRequest`

**Files:**
- Create: `src/taifeng/suspend/__init__.py`
- Create: `src/taifeng/suspend/reason.py`
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py
"""通用挂起 / resume 原语测试。"""
from __future__ import annotations

from taifeng.suspend.reason import PendingRequest, SuspendReason


def test_suspend_reason_values():
    # 四类挂起原因,值用于 JSON 序列化稳定性
    assert SuspendReason.PERMISSION.value == "permission"
    assert SuspendReason.FORM.value == "form"
    assert SuspendReason.DATA.value == "data"
    assert SuspendReason.SYSTEM_RETRY.value == "system_retry"


def test_pending_request_frozen_and_fields():
    req = PendingRequest(
        request_id="req_1",
        reason=SuspendReason.PERMISSION,
        payload_schema={"type": "object"},
        related_call_id="call_abc",
        detail={"scope": "tool_use", "target": "shell_exec"},
    )
    assert req.request_id == "req_1"
    assert req.reason is SuspendReason.PERMISSION
    assert req.related_call_id == "call_abc"
    # frozen:不可变
    import dataclasses
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.request_id = "x"  # type: ignore[misc]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'taifeng.suspend'`

- [ ] **Step 3: 写最小实现**

```python
# src/taifeng/suspend/__init__.py
"""通用挂起 / resume 原语(业务无关)。

参照:openclaw 重入模型 + codex 协议形状(见
docs/superpowers/specs/2026-06-02-suspend-resume-design.md §2)。
差异:taifeng 用 function_call 无 output 的 history-gap 表示挂起点,
不重跑 tool;额外落 SuspensionRecord 标记 turn 中途断点。
"""
from __future__ import annotations

from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.signal import SuspendSignal

__all__ = ["PendingRequest", "SuspendReason", "SuspendSignal"]
```

```python
# src/taifeng/suspend/reason.py
"""挂起原因分类 + 单个待办请求的数据契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SuspendReason(str, Enum):
    """挂起原因 —— 决定 resume 时的续跑语义(见 spec §5)。"""

    PERMISSION = "permission"      # 等权限审批 → decision 回填 gate 结果
    FORM = "form"                  # 等用户填表 → payload 成 tool output
    DATA = "data"                  # 等外部数据 → payload 成 tool output
    SYSTEM_RETRY = "system_retry"  # 限流/余额/key/LLM 错 → resume 即重试同次 sample


@dataclass(frozen=True)
class PendingRequest:
    """一个挂起点的待办请求。

    Attributes:
        request_id: 关联 id(对标 codex call_id);Resume.resolutions 的 key。
        reason: 挂起原因分类。
        payload_schema: JSON Schema —— 业务/前端据此渲染表单或审批 UI。
        related_call_id: 关联的 function_call call_id;人类输入类必有,系统态为 None。
        detail: 不透明上下文(scope/target/command/failure_class 等);taifeng 不解析(R1)。
    """

    request_id: str
    reason: SuspendReason
    payload_schema: dict[str, Any] = field(default_factory=dict)
    related_call_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v`
Expected: PASS(2 passed)。注:`signal.py` 由 Task 2 创建;本步 `__init__.py` 已 import 它会失败 → **本 task 先把 `__init__.py` 的 `signal` import 行删掉**,Task 2 完成后再加回。改 `__init__.py`:暂时 `from taifeng.suspend.reason import PendingRequest, SuspendReason` + `__all__ = ["PendingRequest", "SuspendReason"]`。

- [ ] **Step 5: commit**

```bash
git add src/taifeng/suspend/__init__.py src/taifeng/suspend/reason.py tests/test_suspend.py
git commit -m "feat(suspend): SuspendReason 枚举 + PendingRequest 数据契约"
```

---

### Task 2: `SuspendSignal` 控制流异常

**Files:**
- Create: `src/taifeng/suspend/signal.py`
- Modify: `src/taifeng/suspend/__init__.py`(加回 signal 导出)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
from taifeng.suspend.signal import SuspendSignal


def test_suspend_signal_carries_pending():
    req = PendingRequest(request_id="r1", reason=SuspendReason.FORM)
    sig = SuspendSignal(req)
    assert sig.pending is req
    # 是 Exception 子类(控制流),但不是 LLMError 家族
    from taifeng.llm.errors import LLMError
    assert isinstance(sig, Exception)
    assert not isinstance(sig, LLMError)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_suspend_signal_carries_pending -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'taifeng.suspend.signal'`

- [ ] **Step 3: 写最小实现**

```python
# src/taifeng/suspend/signal.py
"""SuspendSignal —— 挂起的内部控制流异常。

不属于 LLMError 体系:挂起不是错误,是 turn 的正常中断信号。深处挂起点
(permission ask / 表单 tool / LLM 可恢复错误耗尽 retry 后)抛出,由
dispatch_batch / run_turn 捕获后聚合为 TurnSuspended 结局(见 spec §3)。
"""
from __future__ import annotations

from taifeng.suspend.reason import PendingRequest


class SuspendSignal(Exception):
    """携带单个 PendingRequest 的挂起信号。"""

    def __init__(self, pending: PendingRequest) -> None:
        self.pending = pending
        super().__init__(f"suspend: {pending.reason.value} request_id={pending.request_id}")
```

然后把 `__init__.py` 恢复成完整导出(Task 1 Step 4 临时删掉的 signal 行加回):

```python
# src/taifeng/suspend/__init__.py —— 恢复完整 __all__
from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.signal import SuspendSignal

__all__ = ["PendingRequest", "SuspendReason", "SuspendSignal"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: commit**

```bash
git add src/taifeng/suspend/signal.py src/taifeng/suspend/__init__.py tests/test_suspend.py
git commit -m "feat(suspend): SuspendSignal 控制流异常(不属 LLMError)"
```

---

## Phase 2 — 持久化(SuspensionRecord ↔ ResponseItem)

### Task 3: `conversation/models.py` 新增 `suspension` ItemKind + 构造器

**Files:**
- Modify: `src/taifeng/conversation/models.py`(`ItemKind` Literal + `ResponseItem` docstring + 新构造器)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
from taifeng.conversation.models import suspension_item


def test_suspension_item_constructor():
    item = suspension_item(
        record_id="sr_1",
        submission_id="sub_1",
        turn_index=2,
        pending=[{"request_id": "r1", "reason": "permission", "payload_schema": {},
                  "related_call_id": "call_a", "detail": {}}],
        created_at=1000,
        thread_id="th_1",
    )
    assert item.kind == "suspension"
    assert item.thread_id == "th_1"
    assert item.payload["record_id"] == "sr_1"
    assert item.payload["turn_index"] == 2
    assert item.payload["pending"][0]["request_id"] == "r1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_suspension_item_constructor -v`
Expected: FAIL — `ImportError: cannot import name 'suspension_item'`

- [ ] **Step 3: 写实现**

在 `src/taifeng/conversation/models.py` 把 `ItemKind` Literal 末尾加 `"suspension"`:

```python
ItemKind = Literal[
    "user_message",
    "assistant_message",
    "function_call",
    "function_call_output",
    "reasoning",
    "compacted",
    "system_injection",
    "suspension",   # 新增:turn 中途挂起断点标记(payload = 序列化 SuspensionRecord)
]
```

在 `ResponseItem` docstring 的 payload schema 列表末尾加一行:

```python
    - suspension:             {"record_id": str, "submission_id": str, "turn_index": int,
                               "pending": list[dict], "created_at": int, "resolved": bool}
```

在文件「强类型构造器」区(`function_call_output` 之后)新增:

```python
def suspension_item(
    *,
    record_id: str,
    submission_id: str,
    turn_index: int,
    pending: list[dict[str, Any]],
    created_at: int,
    thread_id: str,
) -> ResponseItem:
    """构造 suspension 断点 item(落 JSONL,使 mid-turn 挂起可跨进程 resume)。

    Args:
        pending: 已序列化的 PendingRequest dict 列表。
        created_at: 业务侧传入的时间戳(R1:src 内不取系统时钟)。
    """
    return ResponseItem(
        kind="suspension",
        thread_id=thread_id,
        payload={
            "record_id": record_id,
            "submission_id": submission_id,
            "turn_index": turn_index,
            "pending": pending,
            "created_at": created_at,
            "resolved": False,
        },
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v`
Expected: PASS(4 passed)

- [ ] **Step 5: commit**

```bash
git add src/taifeng/conversation/models.py tests/test_suspend.py
git commit -m "feat(conversation): 新增 suspension ItemKind + 构造器"
```

---

### Task 4: `SuspensionRecord` + ↔ ResponseItem 序列化

**Files:**
- Create: `src/taifeng/suspend/record.py`
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
from taifeng.suspend.record import SuspensionRecord


def test_record_roundtrip_via_item():
    rec = SuspensionRecord(
        record_id="sr_1",
        thread_id="th_1",
        submission_id="sub_1",
        turn_index=1,
        pending=(
            PendingRequest(request_id="r1", reason=SuspendReason.PERMISSION,
                           related_call_id="call_a", detail={"scope": "tool_use"}),
            PendingRequest(request_id="r2", reason=SuspendReason.FORM,
                           related_call_id="call_b"),
        ),
        created_at=1234,
    )
    item = rec.to_item()
    assert item.kind == "suspension"
    # 往返还原等价
    back = SuspensionRecord.from_item(item)
    assert back == rec
    assert back.pending[0].reason is SuspendReason.PERMISSION
    assert back.pending[1].related_call_id == "call_b"


def test_record_request_ids():
    rec = SuspensionRecord(
        record_id="sr", thread_id="t", submission_id="s", turn_index=1,
        pending=(PendingRequest(request_id="a", reason=SuspendReason.DATA),
                 PendingRequest(request_id="b", reason=SuspendReason.DATA)),
        created_at=1,
    )
    assert rec.request_ids() == {"a", "b"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -k record -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'taifeng.suspend.record'`

- [ ] **Step 3: 写实现**

```python
# src/taifeng/suspend/record.py
"""SuspensionRecord —— 落 store 的可序列化挂起断点标记。

一条 record 对应 turn 的一次挂起,可含多个 PendingRequest(多挂起点并存)。
通过 to_item/from_item 与 conversation.ResponseItem(kind="suspension")互转,
复用既有 JSONL 追加写,实现跨进程 resume(R5)。
"""
from __future__ import annotations

from dataclasses import dataclass

from taifeng.conversation.models import ResponseItem, suspension_item
from taifeng.suspend.reason import PendingRequest, SuspendReason


@dataclass(frozen=True)
class SuspensionRecord:
    """turn 一次挂起的完整断点。

    Attributes:
        record_id: 幂等键(重复 resume 检测)。
        pending: 本次挂起的全部待办请求。
        created_at: 业务侧时间戳(R1:src 内不取系统时钟)。
    """

    record_id: str
    thread_id: str
    submission_id: str
    turn_index: int
    pending: tuple[PendingRequest, ...]
    created_at: int

    def request_ids(self) -> set[str]:
        """本 record 全部 pending 的 request_id 集合(resume 校验用)。"""
        return {p.request_id for p in self.pending}

    def to_item(self) -> ResponseItem:
        """序列化为 suspension ResponseItem(落 JSONL)。"""
        return suspension_item(
            record_id=self.record_id,
            submission_id=self.submission_id,
            turn_index=self.turn_index,
            pending=[
                {
                    "request_id": p.request_id,
                    "reason": p.reason.value,
                    "payload_schema": p.payload_schema,
                    "related_call_id": p.related_call_id,
                    "detail": p.detail,
                }
                for p in self.pending
            ],
            created_at=self.created_at,
            thread_id=self.thread_id,
        )

    @classmethod
    def from_item(cls, item: ResponseItem) -> SuspensionRecord:
        """从 suspension ResponseItem 还原。

        Raises:
            ValueError: item.kind 不是 'suspension'。
        """
        if item.kind != "suspension":
            raise ValueError(f"not a suspension item: kind={item.kind}")
        p = item.payload
        return cls(
            record_id=p["record_id"],
            thread_id=item.thread_id,
            submission_id=p["submission_id"],
            turn_index=p["turn_index"],
            pending=tuple(
                PendingRequest(
                    request_id=d["request_id"],
                    reason=SuspendReason(d["reason"]),
                    payload_schema=d.get("payload_schema") or {},
                    related_call_id=d.get("related_call_id"),
                    detail=d.get("detail") or {},
                )
                for d in p["pending"]
            ),
            created_at=p["created_at"],
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v`
Expected: PASS(6 passed)

- [ ] **Step 5: commit**

```bash
git add src/taifeng/suspend/record.py tests/test_suspend.py
git commit -m "feat(suspend): SuspensionRecord + ResponseItem 往返序列化"
```

---

## Phase 3 — SuspendingPrompter

### Task 5: `permission/types.py` 新增 `SuspendingPrompter`

**Files:**
- Modify: `src/taifeng/permission/types.py`(文件尾部「内置 prompter 实现」区追加)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
import pytest

from taifeng.permission.types import PermissionRequest, SuspendingPrompter
from taifeng.suspend.reason import SuspendReason
from taifeng.suspend.signal import SuspendSignal


async def test_suspending_prompter_raises_signal():
    """ask 模式不阻塞,而是抛 SuspendSignal(reason=PERMISSION)。"""
    prompter = SuspendingPrompter()
    req = PermissionRequest.for_tool_call(
        "shell_exec", {"cmd": "rm -rf /tmp/x"},
        thread_id="th", submission_id="sub", entry_skill_id="root",
        turn_index=1, call_chain=("root",),
    )
    with pytest.raises(SuspendSignal) as ei:
        await prompter.prompt(req)
    pending = ei.value.pending
    assert pending.reason is SuspendReason.PERMISSION
    # permission 挂起必带关联(scope/target 进 detail),供前端渲染审批 UI
    assert pending.detail["scope"] == "tool_use"
    assert pending.detail["target"] == "shell_exec"
    assert pending.request_id  # 非空
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_suspending_prompter_raises_signal -v`
Expected: FAIL — `ImportError: cannot import name 'SuspendingPrompter'`

- [ ] **Step 3: 写实现**

在 `src/taifeng/permission/types.py` 末尾(`CallbackPrompter` 之后)追加。注意顶部已 `import secrets`?没有 → 在文件顶部 import 区加 `import secrets`(若已存在则跳过)。

```python
class SuspendingPrompter(PermissionPrompter):
    """挂起式 prompter —— ask 模式不阻塞,抛 SuspendSignal 让 turn 退栈挂起。

    参照 openclaw「tool 早返回 approval-pending」:差异是 taifeng 不返回状态码,
    而是抛控制流异常,由 run_turn 聚合为 TurnSuspended(见 spec §3)。

    request_id 生成策略:优先用 PermissionRequest 关联的工具调用上下文,
    回退到随机 id。payload_schema 给出审批决定的 JSON Schema(allow/deny)。
    """

    def __init__(self, *, request_id_factory: Callable[[], str] | None = None) -> None:
        # 注入式 id 工厂(测试可固定);默认随机
        self._gen_id = request_id_factory or (lambda: f"req_{secrets.token_hex(6)}")

    # 审批决定的 JSON Schema —— 业务/前端据此渲染审批 UI
    _DECISION_SCHEMA = {
        "type": "object",
        "properties": {
            "granted": {"type": "boolean"},
            "reason": {"type": "string"},
            "remember_until": {"enum": ["once", "session", "always"]},
        },
        "required": ["granted"],
    }

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        """不返回 —— 总是抛 SuspendSignal(reason=PERMISSION)。"""
        from taifeng.suspend.reason import PendingRequest, SuspendReason
        from taifeng.suspend.signal import SuspendSignal

        # detail:把审批上下文透传给前端(R1:taifeng 不解析,仅携带)
        detail = {
            "scope": request.scope,
            "target": request.target,
            "reason": request.reason,
            "call_chain": list(request.call_chain),
            **request.metadata,
        }
        pending = PendingRequest(
            request_id=self._gen_id(),
            reason=SuspendReason.PERMISSION,
            payload_schema=self._DECISION_SCHEMA,
            related_call_id=request.metadata.get("call_id"),
            detail=detail,
        )
        raise SuspendSignal(pending)
```

> 注:`related_call_id` 取自 `request.metadata["call_id"]`。当前 `PermissionRequest.for_tool_call` 未写入 `call_id` —— Task 8 会在 turn 侧补:`extra_metadata={"call_id": req.call_id}`。本 task 测试不依赖该字段(允许 None)。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v`
Expected: PASS(7 passed)

- [ ] **Step 5: commit**

```bash
git add src/taifeng/permission/types.py tests/test_suspend.py
git commit -m "feat(permission): SuspendingPrompter —— ask 抛 SuspendSignal 不阻塞"
```

---

## Phase 4 — dispatch_batch 收集挂起信号

### Task 6: `dispatch_batch` 不 fail-fast,聚合 `SuspendSignal`

**Files:**
- Modify: `src/taifeng/loop/tool_batch.py`(`ToolCallOutcome` 加 `suspend` 字段;`_dispatch_one` 捕获 `SuspendSignal`)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
from taifeng.loop.tool_batch import ToolCallOutcome


def test_outcome_has_optional_suspend_field():
    # ToolCallOutcome 新增 suspend 字段,默认 None(正常完成的 outcome)
    import dataclasses
    fields = {f.name for f in dataclasses.fields(ToolCallOutcome)}
    assert "suspend" in fields
```

> `_dispatch_one` 捕获 `SuspendSignal` 的行为属集成行为,放 Phase 5 的 turn e2e 验证;此处只锁结构。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_outcome_has_optional_suspend_field -v`
Expected: FAIL — `assert 'suspend' in {...}`

- [ ] **Step 3: 写实现**

`ToolCallOutcome` 加字段(`result` 在挂起时无意义,但保留非 Optional 以最小化改动 → 挂起 outcome 用一个哨兵 `ToolResult`)。改 `tool_batch.py`:

```python
from taifeng.suspend.signal import SuspendSignal   # 顶部 import 区追加
```

```python
@dataclass(frozen=True)
class ToolCallOutcome:
    """单条 tool call 的执行结果。

    suspend 非 None 时表示该 tool call 命中挂起点(此时 result 为占位错误,
    调用方据 suspend 改走挂起落盘路径,不回填 function_call_output)。
    """

    index: int
    call_id: str
    name: str
    result: ToolResult
    duration_ms: int
    suspend: Any = None   # PendingRequest | None;Any 避免顶层 import suspend 包
```

在 `_dispatch_one` 内,把对 `runtime.dispatch` 的调用用 try 包住捕获 `SuspendSignal`。`_dispatch_one` 末尾原本 `return ToolCallOutcome(...)`;改为:在调用链抛 `SuspendSignal` 时返回带 `suspend=sig.pending` 的 outcome。最小改法——在 `_dispatch_one` 函数体最外层包:

```python
    ctx = ctx_for(req.call_id)
    start = time.monotonic()
    try:
        # ... 原有 hook + dispatch + PostToolUse + emit 逻辑保持不变 ...
        # (原函数体整体移入 try)
        return ToolCallOutcome(index=req.index, call_id=req.call_id, name=req.name,
                               result=result, duration_ms=duration_ms)
    except SuspendSignal as sig:
        # 挂起:不 emit Completed(turn 侧据 suspend 落 suspension);返回占位 result
        from taifeng.tool.spec import ToolResult
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolCallOutcome(
            index=req.index, call_id=req.call_id, name=req.name,
            result=ToolResult(output="<suspended>", is_error=False),
            duration_ms=duration_ms, suspend=sig.pending,
        )
```

> 实现注意:`SuspendSignal` 可能从 PreToolUse hook 或 runtime.dispatch 任一处抛出 —— try 必须包住二者。确认 `ToolResult` 构造签名:`ToolResult(output=..., is_error=...)`(见 `src/taifeng/tool/spec.py`)。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_outcome_has_optional_suspend_field tests/test_dispatch.py -v`
Expected: PASS(新测试 + dispatch 回归全绿)

- [ ] **Step 5: commit**

```bash
git add src/taifeng/loop/tool_batch.py tests/test_suspend.py
git commit -m "feat(loop): dispatch_batch 捕获 SuspendSignal 不 fail-fast"
```

---

## Phase 5 — turn.py 挂起结局 + system_retry

### Task 7: `TurnOutcome` 加挂起结局 + `run_turn` 落 suspension

**Files:**
- Modify: `src/taifeng/loop/turn.py`(`TurnOutcome` 加 `suspension` 字段;`_dispatch_tools` 配对回填区识别 `outcome.suspend`;`run_turn` 结局)
- Test: `tests/test_suspend.py`(集成,走 MockClient + SuspendingPrompter)

- [ ] **Step 1: 写失败测试(集成)**

```python
# tests/test_suspend.py 追加 —— 用项目既有的 e2e 搭建套路
# 参考 tests/test_engine_e2e.py 里 MockClient + TurnRunner 的构造方式,
# 构造一个 entry skill,其 system prompt 诱导 MockClient 产出一个需要审批的
# shell_exec tool call;permission policy default_mode='ask' + SuspendingPrompter。
#
# 断言:run_turn 返回 outcome.end_reason == "suspended" 且 outcome.suspension
# 含一个 PendingRequest(reason=PERMISSION);store 里出现 kind=="suspension" 的 item,
# 且对应 call_id 的 function_call 已落盘但无 function_call_output。
#
# 具体 fixture 构造照抄 test_engine_e2e.py 既有 helper(MockClient 脚本化响应)。
```

> 实现者:打开 `tests/test_engine_e2e.py`,复制其 MockClient + skill + store 搭建 helper 到本测试;MockClient 脚本设为「第一轮返回一个 shell_exec tool call」。本步先写断言骨架并跑,确认因 `end_reason != "suspended"` 失败。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -k suspend_turn -v`
Expected: FAIL — `end_reason == "completed"`(或 KeyError:`suspension` 字段不存在)

- [ ] **Step 3: 写实现**

(a) `TurnOutcome` 加字段(`turn.py:106` 区):

```python
@dataclass(frozen=True)
class TurnOutcome:
    success: bool
    iterations: int
    duration_ms: int
    usage: TokenUsage
    final_text: str
    end_reason: str
    error: str | None = None
    suspension: Any = None   # SuspensionRecord | None;end_reason=="suspended" 时非空
```

(b) `_dispatch_tools` 配对回填区(`turn.py:653` 附近的 `for req, outcome in zip(...)` 循环)——挂起的 outcome **不回填 output**,而是只落 `function_call` 并收集 pending:

```python
        suspended_pending: list[Any] = []   # PendingRequest 列表
        for req, outcome in zip(requests, outcomes, strict=True):
            fc_item = function_call(
                call_id=req.call_id, name=req.name,
                arguments=req.arguments_raw, thread_id=self.thread_id,
            )
            self.history_buffer.append(fc_item)
            await self.store.append(fc_item)
            if outcome.suspend is not None:
                # 挂起点:留下无 output 的 function_call(history-gap),收集 pending
                suspended_pending.append(outcome.suspend)
                continue
            fco_item = function_call_output(
                call_id=req.call_id, output=outcome.result.output,
                thread_id=self.thread_id, is_error=outcome.result.is_error,
            )
            self.history_buffer.append(fco_item)
            await self.store.append(fco_item)

        if suspended_pending:
            # 抛 SuspendSignal 给 run_turn 聚合;多挂起点合成一条 record
            raise _BatchSuspend(tuple(suspended_pending))
        return assistant_text, True
```

其中新增一个内部聚合异常(turn.py 顶部,`TurnRunner` 之前):

```python
class _BatchSuspend(Exception):
    """内部:_dispatch_tools 把整批挂起 pending 上抛给 run_turn。"""
    def __init__(self, pending: tuple[Any, ...]) -> None:
        self.pending = pending
        super().__init__(f"batch suspend: {len(pending)} pending")
```

(c) `run_turn` 主循环外层 try 捕获 `_BatchSuspend`(放在 `except asyncio.CancelledError` **之前**,因为它不是错误):

```python
        except _BatchSuspend as bs:
            end_reason = "suspended"
            suspension = await self._persist_suspension(bs.pending)
```

并在 `try` 上方初始化 `suspension = None`,在构造 `TurnOutcome` 时传入 `suspension=suspension`。

(d) 新增 `_persist_suspension` 方法(TurnRunner 内),落 suspension item:

```python
    async def _persist_suspension(self, pending: tuple[Any, ...]) -> Any:
        """把本次挂起落 store 并返回 SuspensionRecord。

        record_id / created_at 由注入的工厂提供(R1:不取系统时钟);
        默认工厂在 __init__ 已设(见 Task 7 step3-e)。
        """
        from taifeng.suspend.record import SuspensionRecord

        record = SuspensionRecord(
            record_id=self._suspend_id_factory(),
            thread_id=self.thread_id,
            submission_id=self.submission_id,
            turn_index=self._current_iteration,
            pending=pending,
            created_at=self._now_factory(),
        )
        item = record.to_item()
        self.history_buffer.append(item)
        await self.store.append(item)
        return record
```

(e) `TurnRunner` 需要两个可注入工厂 + 记录当前 iteration。`TurnRunner` 是 `@dataclass`,加字段:

```python
    # 挂起 id / 时间戳工厂(R1:src 内不取系统时钟/随机;业务侧注入,测试可固定)
    suspend_id_factory: Any = None
    now_factory: Any = None
```

在 `__post_init__`(若无则加一个)里兜底默认值,并加 `self._current_iteration = 0`:

```python
    def __post_init__(self) -> None:
        import secrets
        import time as _time
        self._suspend_id_factory = self.suspend_id_factory or (lambda: f"sr_{secrets.token_hex(6)}")
        self._now_factory = self.now_factory or (lambda: int(_time.time()))
        self._current_iteration = 0
```

> 注:若 `TurnRunner` 已有 `__post_init__`,把上述三行并入,不要重复定义。`_current_iteration` 在 `run_turn` 主循环 `iterations += 1` 后同步赋值:`self._current_iteration = iterations`。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py tests/test_engine_e2e.py -v`
Expected: PASS(挂起集成测试 + e2e 回归全绿)

- [ ] **Step 5: commit**

```bash
git add src/taifeng/loop/turn.py tests/test_suspend.py
git commit -m "feat(loop): run_turn 退栈为 suspended 结局 + 落 suspension 断点"
```

---

### Task 8: permission check 透传 `call_id` + system_retry 转挂起

**Files:**
- Modify: `src/taifeng/loop/turn.py`(`_sample_once` 的 LLMError 处理 → 可恢复类转 `SuspendSignal`;permission 调用点补 `call_id`)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
def test_should_suspend_classifies_recoverable():
    from taifeng.loop.turn import _should_suspend_on_error
    from taifeng.llm.errors import (
        RateLimitError, AuthenticationError, ContentFilterError,
        ContextOverflowError, InvalidRequestError,
    )
    # 可恢复 / 等外部条件 → 挂起
    assert _should_suspend_on_error(RateLimitError("rl")) is True
    assert _should_suspend_on_error(AuthenticationError("bad key")) is True
    # 确定性失败 → 不挂起(硬失败)
    assert _should_suspend_on_error(ContentFilterError("blocked")) is False
    assert _should_suspend_on_error(ContextOverflowError("too long")) is False
    assert _should_suspend_on_error(InvalidRequestError("bad req")) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_should_suspend_classifies_recoverable -v`
Expected: FAIL — `ImportError: cannot import name '_should_suspend_on_error'`

- [ ] **Step 3: 写实现**

(a) `turn.py` 顶部(模块级函数)加判据:

```python
def _should_suspend_on_error(err: Exception) -> bool:
    """LLMError 是否转 SYSTEM_RETRY 挂起(等外部条件清除后重跑同次 sample 可过)。

    判据(见 spec §5.3):retryable 为真,或 failure_class 属"等外部介入"类
    (provider_auth / provider_quota / provider_balance)。ContentFilter /
    ContextOverflow / InvalidRequest 这类确定性失败不挂起,照旧硬失败。
    """
    from taifeng.llm.errors import LLMError

    if not isinstance(err, LLMError):
        return False
    if getattr(err, "retryable", False):
        return True
    return getattr(err, "failure_class", None) in (
        "provider_auth", "provider_quota", "provider_balance",
    )
```

(b) `_sample_once` 内对 `ModelClientSession.stream` 的调用:retry 由既有 `retry_async` 负责(≤3 次)。retry 耗尽抛出的 `LLMError` —— 在 `_sample_once` 捕获,若 `_should_suspend_on_error` 为真则转 `SuspendSignal(reason=SYSTEM_RETRY)`:

```python
        # _sample_once 内,包住 stream 调用的 try(retry_async 已在 stream 内部):
        from taifeng.llm.errors import LLMError
        from taifeng.suspend.reason import PendingRequest, SuspendReason
        from taifeng.suspend.signal import SuspendSignal
        try:
            # ... 既有 ModelClientSession.stream 消费循环 ...
            pass
        except LLMError as e:
            if _should_suspend_on_error(e):
                raise SuspendSignal(PendingRequest(
                    request_id=self._suspend_id_factory(),
                    reason=SuspendReason.SYSTEM_RETRY,
                    payload_schema={"type": "object",
                                    "properties": {"action": {"enum": ["retry", "abort"]}}},
                    related_call_id=None,
                    detail={"failure_class": getattr(e, "failure_class", None),
                            "retry_after_seconds": getattr(e, "retry_after_seconds", None),
                            "kind": type(e).__name__},
                )) from e
            raise   # 确定性失败:照旧上抛硬失败
```

> system_retry 的 `SuspendSignal` 没有 tool call 配对 —— 它在 sample 阶段抛出。`run_turn` 的 `_BatchSuspend` 只覆盖 tool 阶段;system_retry 走另一路:`run_turn` 主循环外层 try **也要捕获 `SuspendSignal`**(单个),转挂起:

```python
        except SuspendSignal as sig:
            end_reason = "suspended"
            suspension = await self._persist_suspension((sig.pending,))
```

放在 `except _BatchSuspend` 之后、`except asyncio.CancelledError` 之前。

(c) permission 调用点补 `call_id`:在 `_build_tool_context` 或调用 `policy.check` 处,给 `PermissionRequest.for_tool_call` 传 `extra_metadata={"call_id": call_id}`。定位:`src/taifeng/tool/builtins/` 各 builtin 调 `policy.check` 处——但 builtin 不持有 turn 的 call_id。**最小改法**:在 `ToolContext` 已带 `call_id`(确认 `tool/spec.py::ToolContext` 字段);builtin 用 `ctx.call_id` 填 `extra_metadata`。逐个 builtin(`file_io.py` / `apply_patch.py` / `shell.py` / `background.py` / `call_skill.py`)的 `PermissionRequest.for_*` 调用补 `extra_metadata={"call_id": ctx.call_id, ...}`。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py tests/test_engine_e2e.py tests/test_dispatch.py -v`
Expected: PASS 全绿

- [ ] **Step 5: commit**

```bash
git add src/taifeng/loop/turn.py src/taifeng/tool/builtins/ tests/test_suspend.py
git commit -m "feat(loop): system_retry 转挂起 + permission 透传 call_id"
```

---

## Phase 6 — Events + Resume Op

### Task 9: 3 个新 EventMsg

**Files:**
- Modify: `src/taifeng/loop/event.py`(`MsgKind` Literal + 3 个 `_Msg` 子类 + `EventMsg` Union + `__all__`)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
def test_new_event_msgs():
    from taifeng.loop.event import (
        TurnSuspended, SuspensionResolved, SuspensionResolveRejected,
    )
    e1 = TurnSuspended(submission_id="s", data={"thread_id": "t", "record_id": "sr",
                                                 "pending": [], "cache_invalidated": True})
    assert e1.kind == "turn_suspended"
    e2 = SuspensionResolved(submission_id="s", data={"record_id": "sr", "request_ids": ["r1"]})
    assert e2.kind == "suspension_resolved"
    e3 = SuspensionResolveRejected(submission_id="s", data={"reason": "unknown_request_id"})
    assert e3.kind == "suspension_resolve_rejected"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_new_event_msgs -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 写实现**

`event.py` 的 `MsgKind` Literal 加三个值:`"turn_suspended"`、`"suspension_resolved"`、`"suspension_resolve_rejected"`。

在 `ThreadResumed` 附近追加三个类:

```python
class TurnSuspended(_Msg):
    """turn 在中途挂起,实例可释放;业务凭 thread_id + request_id 提交 Resume 续跑。

    data = {"thread_id": str, "record_id": str,
            "pending": list[dict],         # 每项 {request_id, reason, payload_schema, related_call_id, detail}
            "cache_invalidated": bool}     # tier-2 跨进程 resume 必 True
    """

    kind: Literal["turn_suspended"] = "turn_suspended"


class SuspensionResolved(_Msg):
    """Resume 成功配对,turn 续跑。

    data = {"record_id": str, "request_ids": list[str]}
    """

    kind: Literal["suspension_resolved"] = "suspension_resolved"


class SuspensionResolveRejected(_Msg):
    """Resume 被拒(resolution 不全/多余、record 已消费、payload 不符 schema 等)。

    data = {"reason": str, "record_id": str | None, "detail": dict}
    """

    kind: Literal["suspension_resolve_rejected"] = "suspension_resolve_rejected"
```

把三个类名加入文件底部 `EventMsg = Union[...]` 与 `__all__`(若有)。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_new_event_msgs -v`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add src/taifeng/loop/event.py tests/test_suspend.py
git commit -m "feat(loop): 新增 turn_suspended/suspension_resolved/rejected EventMsg"
```

---

### Task 10: `Resume` Op

**Files:**
- Modify: `src/taifeng/loop/submission.py`(新增 `Resume` + 加进 `Op` Union)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
def test_resume_op_in_union():
    from taifeng.loop.submission import Resume, Submission
    op = Resume(thread_id="th_1", resolutions={"r1": {"granted": True}})
    assert op.kind == "resume"
    sub = Submission(op=op)
    assert sub.op.thread_id == "th_1"
    assert sub.op.resolutions["r1"]["granted"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_resume_op_in_union -v`
Expected: FAIL — `ImportError: cannot import name 'Resume'`

- [ ] **Step 3: 写实现**

`submission.py` 在 `Shutdown` 之前新增:

```python
class Resume(BaseModel):
    """续跑一个挂起的 thread(见 spec §3)。

    Attributes:
        thread_id: 要续跑的 thread。
        resolutions: {request_id: payload};必须一次补齐该挂起 record 的全部 pending
            (不允许部分 resume,见 spec §6)。payload 形状由对应 PendingRequest.reason 决定:
            - permission: {"granted": bool, "reason"?: str, "remember_until"?: str}
            - form / data: 任意 JSON(直接成 function_call_output)
            - system_retry: {"action": "retry" | "abort"}
    """

    kind: Literal["resume"] = "resume"
    thread_id: str
    resolutions: dict[str, Any]
```

加进 `Op` Union:

```python
Op = Union[
    UserMessage,
    CancelTurn,
    CompactNow,
    InjectSystemMessage,
    ThreadRollback,
    UpdateBudget,
    RefreshSnapshot,
    UpdateInstructions,
    Resume,        # 新增
    Shutdown,
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py::test_resume_op_in_union -v`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add src/taifeng/loop/submission.py tests/test_suspend.py
git commit -m "feat(loop): 新增 Resume Op"
```

---

## Phase 7 — SuspensionResolver

### Task 11: `SuspensionResolver` 配对 + 校验

**Files:**
- Create: `src/taifeng/suspend/resolver.py`
- Modify: `src/taifeng/suspend/__init__.py`(导出 `SuspensionResolver` + 结果类型)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_suspend.py 追加
def _rec(*reqs):
    from taifeng.suspend.record import SuspensionRecord
    return SuspensionRecord(record_id="sr", thread_id="t", submission_id="s",
                            turn_index=1, pending=tuple(reqs), created_at=1)


def test_resolver_rejects_incomplete():
    from taifeng.suspend.resolver import SuspensionResolver, ResolveError
    import pytest
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.FORM),
        PendingRequest(request_id="r2", reason=SuspendReason.FORM),
    )
    resolver = SuspensionResolver()
    with pytest.raises(ResolveError) as ei:
        resolver.validate(rec, {"r1": {"x": 1}})   # 缺 r2
    assert "incomplete" in str(ei.value).lower() or "r2" in str(ei.value)


def test_resolver_rejects_unknown():
    from taifeng.suspend.resolver import SuspensionResolver, ResolveError
    import pytest
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.FORM))
    with pytest.raises(ResolveError):
        SuspensionResolver().validate(rec, {"r1": {}, "rX": {}})   # 多余 rX


def test_resolver_classifies_outputs():
    from taifeng.suspend.resolver import SuspensionResolver
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.PERMISSION, related_call_id="ca"),
        PendingRequest(request_id="r2", reason=SuspendReason.FORM, related_call_id="cb"),
        PendingRequest(request_id="r3", reason=SuspendReason.SYSTEM_RETRY),
    )
    plan = SuspensionResolver().plan(rec, {
        "r1": {"granted": True},
        "r2": {"answer": "hello"},
        "r3": {"action": "retry"},
    })
    # permission allow → 需执行 tool ca
    assert plan.execute_tool_call_ids == ["ca"]
    # form → 直接成 output(call cb)
    assert plan.direct_outputs["cb"] == {"answer": "hello"}
    # system_retry → 重跑 sample
    assert plan.resample is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -k resolver -v`
Expected: FAIL — `ModuleNotFoundError: taifeng.suspend.resolver`

- [ ] **Step 3: 写实现**

```python
# src/taifeng/suspend/resolver.py
"""SuspensionResolver —— 把 Resume.resolutions 配回 SuspensionRecord.pending。

产出 ResolvePlan 告诉 turn 续跑时该做什么:执行哪些 tool call(permission allow)、
哪些 call_id 直接回填 output(form/data + permission deny)、是否重跑 sample(system_retry)。
不允许部分 resume(见 spec §6):resolutions 必须与 record.request_ids() 精确相等。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from taifeng.suspend.reason import SuspendReason
from taifeng.suspend.record import SuspensionRecord


class ResolveError(Exception):
    """resolution 不合法(不全/多余/payload 不符 schema)。"""


@dataclass
class ResolvePlan:
    """续跑计划。"""

    execute_tool_call_ids: list[str] = field(default_factory=list)   # permission allow → 执行 tool
    direct_outputs: dict[str, Any] = field(default_factory=dict)      # call_id → output(form/data)
    deny_outputs: dict[str, str] = field(default_factory=dict)        # call_id → deny reason(permission deny)
    resample: bool = False                                            # system_retry → 重跑 sample
    abort: bool = False                                               # system_retry action=abort


class SuspensionResolver:
    """挂起配对器(无状态)。"""

    def validate(self, record: SuspensionRecord, resolutions: dict[str, Any]) -> None:
        """校验 resolutions 与 record 精确匹配;不匹配抛 ResolveError(禁部分 resume)。"""
        want = record.request_ids()
        got = set(resolutions.keys())
        if got != want:
            missing = want - got
            extra = got - want
            raise ResolveError(
                f"incomplete_or_extra_resolutions: missing={sorted(missing)} extra={sorted(extra)}"
            )

    def plan(self, record: SuspensionRecord, resolutions: dict[str, Any]) -> ResolvePlan:
        """校验后产出续跑计划。"""
        self.validate(record, resolutions)
        plan = ResolvePlan()
        for p in record.pending:
            payload = resolutions[p.request_id]
            if p.reason is SuspendReason.PERMISSION:
                granted = bool(payload.get("granted"))
                if granted:
                    if p.related_call_id is None:
                        raise ResolveError(f"permission pending missing related_call_id: {p.request_id}")
                    plan.execute_tool_call_ids.append(p.related_call_id)
                else:
                    plan.deny_outputs[p.related_call_id or p.request_id] = str(
                        payload.get("reason", "denied by user")
                    )
            elif p.reason in (SuspendReason.FORM, SuspendReason.DATA):
                if p.related_call_id is None:
                    raise ResolveError(f"form/data pending missing related_call_id: {p.request_id}")
                plan.direct_outputs[p.related_call_id] = payload
            elif p.reason is SuspendReason.SYSTEM_RETRY:
                action = payload.get("action", "retry")
                if action == "abort":
                    plan.abort = True
                else:
                    plan.resample = True
        return plan
```

更新 `__init__.py` `__all__` 增加 `"SuspensionResolver"`, `"ResolvePlan"`, `"ResolveError"`, `"SuspensionRecord"`(并 import)。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v`
Expected: PASS 全绿

- [ ] **Step 5: commit**

```bash
git add src/taifeng/suspend/resolver.py src/taifeng/suspend/__init__.py tests/test_suspend.py
git commit -m "feat(suspend): SuspensionResolver 配对 + 禁部分 resume 校验"
```

---

## Phase 8 — Engine 集成

### Task 12: Engine `_handle_resume` + suspended 态续跑

**Files:**
- Modify: `src/taifeng/loop/engine.py`(Submission dispatch 加 `Resume` 分支;suspended 态记录;续跑入口)
- Test: `tests/test_engine_e2e.py`

- [ ] **Step 1: 阅读既有 Op 分发**

打开 `src/taifeng/loop/engine.py`,定位 Submission 消费的 dispatch(grep `_handle_update_instructions` / `op.kind` / `isinstance(op,`),照其模式加 `Resume` 分支。确认 `AgentEngine` 如何持有当前 thread 的 `MessageStore`、`emit`、以及 TurnRunner 的构造方式(`engine.py:112` 附近 `initial_history` / resume 注释)。

- [ ] **Step 2: 写失败测试(e2e)**

```python
# tests/test_engine_e2e.py 追加(沿用本文件既有 MockClient/engine fixture)
async def test_resume_after_permission_suspension(...):
    """MockClient 第一轮产出 shell_exec → ask + SuspendingPrompter → TurnSuspended;
    提交 Resume{granted:True} → tool 执行 → MockClient 第二轮无 tool call → 完成。

    断言:
      1. 第一次 run 后收到 EventMsg.turn_suspended,data.pending 含 1 项 PERMISSION;
      2. store 中 function_call(shell_exec) 已落、无 function_call_output;
      3. 提交 Resume 后收到 suspension_resolved;
      4. 续跑后 store 中该 call_id 的 function_call_output 出现;turn_completed。
    """
    ...
```

> 实现者按本文件既有 e2e 风格补全 fixture;MockClient 脚本两轮:第一轮 tool call,第二轮纯文本完成。

- [ ] **Step 3: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/test_engine_e2e.py -k resume_after_permission -v`
Expected: FAIL(Resume 分支未实现 → 无 suspension_resolved / output 未补)

- [ ] **Step 4: 写实现**

在 engine 的 Submission dispatch 加分支(伪代码,按既有 `_handle_*` 签名落地):

```python
        elif isinstance(op, Resume):
            await self._handle_resume(submission.id, op)
```

`_handle_resume` 要点(全部走既有抽象):

1. **取挂起 record**:从 `self._store.load_thread(op.thread_id)` 读全量,找**最后一条 `kind=="suspension"` 且 `payload["resolved"] is False`** 的 item → `SuspensionRecord.from_item`。找不到 → emit `SuspensionResolveRejected(reason="no_active_suspension")` 返回。
2. **幂等**:若该 record_id 已被消费(payload resolved=True 或内存 set 记录)→ emit rejected(`reason="already_resolved"`)返回。
3. **校验 + plan**:`SuspensionResolver().plan(record, op.resolutions)`;`ResolveError` → emit `SuspensionResolveRejected(reason=str(e))` 返回(禁 silent fallback)。
4. **重建 history**(tier-2):若内存无该 thread 的 TurnRunner/engine(已释放),用既有 `resume-by-thread-id` 路径从 store 重建 history_buffer(`load_thread` 去掉末尾 suspension item)。tier-1 直接复用驻留的 runner。
5. **回填 output / 执行 tool**:
   - `plan.direct_outputs`:对每个 call_id 追加 `function_call_output(call_id, output=json.dumps(payload), is_error=False)` 到 history + store。
   - `plan.deny_outputs`:追加 `function_call_output(call_id, output=reason, is_error=True)`。
   - `plan.execute_tool_call_ids`:对每个 call_id 从 record/history 找回原 `function_call`(name+arguments),经 `tool_runtime.dispatch` 真正执行,把结果追加为 `function_call_output`。
   - `plan.resample`:不动 history(system_retry)。
   - `plan.abort`:追加一条 `function_call_output(..., is_error=True, output="aborted by user")` 或终结 turn(emit turn_failed)。
6. **标记 record 已消费**:追加一条 resolved 标记(最简:append 一个 `system_injection` item `{"source":"suspend","resolved_record":record_id}`,或新增 `suspension` item with `resolved=True`)。
7. **emit `SuspensionResolved(record_id, request_ids)`**。
8. **续跑 turn**:复用 TurnRunner.run_turn 从 sample loop 继续(history 已补齐)。按既有 engine 启动 turn 的方式重新驱动一轮 `run_turn`(entry skill 不变)。

> **关键约束**:第 4、8 步必须复用既有 `resume-by-thread-id`(`engine.py:112/183` 注释、`pool.py:364` 的 `resume_thread_id`)与 `run_turn`,不另起炉灶。第 5 步执行 tool 复用 `tool_runtime.dispatch`,不绕过 RwLock。

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `PYTHONPATH=src uv run pytest tests/test_engine_e2e.py tests/test_suspend.py -v`
Expected: PASS 全绿

- [ ] **Step 6: commit**

```bash
git add src/taifeng/loop/engine.py tests/test_engine_e2e.py
git commit -m "feat(loop): Engine _handle_resume —— 配对回填 + 续跑挂起 turn"
```

---

## Phase 9 — 收尾(顶层导出 + 边界测试 + 文档)

### Task 13: 顶层导出 + 边界/幂等/cancel 测试

**Files:**
- Modify: `src/taifeng/__init__.py`(`__all__` 增 `SuspendReason`/`PendingRequest`/`Resume`/`SuspendingPrompter` 等公共类型)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 写失败/补充测试**

```python
# tests/test_suspend.py 追加
def test_public_exports():
    import taifeng
    for name in ("SuspendReason", "PendingRequest", "Resume", "SuspendingPrompter"):
        assert hasattr(taifeng, name), name


async def test_resume_rejects_partial(...):
    """对 2 个 pending 的挂起,只补 1 个 resolution → SuspensionResolveRejected。"""
    ...

async def test_duplicate_resume_idempotent(...):
    """同一 record 提交两次 Resume → 第二次 already_resolved 被拒。"""
    ...

async def test_cancel_during_suspension(...):
    """挂起态收到 CancelTurn → 清理,turn 终结为 cancelled。"""
    ...

async def test_system_retry_resume_resamples(...):
    """MockClient 前 3 次抛 RateLimitError → 挂起;Resume{action:retry} → 重跑 sample 成功。"""
    ...

async def test_tier2_rebuild_resume(...):
    """释放 engine(新建实例,仅靠 store)后 Resume → 从 JSONL + suspension 重建续跑。"""
    ...
```

> 边界用例(空 resolutions / 超长 payload / 未知 request_id / thread 不存在)各加一条,断言 `SuspensionResolveRejected` 或 `ResolveError`。

- [ ] **Step 2: 跑测试确认失败** — Run: `PYTHONPATH=src uv run pytest tests/test_suspend.py -v` → 部分 FAIL。

- [ ] **Step 3: 写实现** — 在 `src/taifeng/__init__.py` 的 import 区与 `__all__` 增加公共类型;补齐 engine 侧 cancel/幂等分支(若 Task 12 未覆盖)。

- [ ] **Step 4: 全量回归** — Run: `PYTHONPATH=src uv run pytest tests/ -v`,Expected: 全绿。再跑 `uv run ruff check src/ tests/` 与 `uv run mypy src/`,Expected: 无新增告警。

- [ ] **Step 5: commit**

```bash
git add src/taifeng/__init__.py tests/test_suspend.py
git commit -m "feat(suspend): 顶层导出 + 边界/幂等/cancel/tier2 测试"
```

---

### Task 14: 文档义务(架构活文档 + 契约 + ADR)

**Files:**
- Create: `docs/architecture/capabilities/suspend-resume.md`
- Modify: `docs/architecture/capabilities/README.md`(索引表加一行)
- Modify: `docs/architecture/agent-loop.md`(loop/tool 变更:挂起结局 + Resume Op)
- Create: `docs/decisions/NNNN-suspend-resume-primitive.md`(NNNN = `ls docs/decisions` 取下一个编号)
- Modify: `docs/configurable-knobs.md`(SuspendingPrompter、retry-then-suspend 阈值、Resume Op)

- [ ] **Step 1: 写能力契约** `capabilities/suspend-resume.md` —— 数据契约(`SuspendReason`/`PendingRequest`/`SuspensionRecord`/`Resume`/3 EventMsg)+ 行为契约(挂起→释放→resume 时序、禁部分 resume、幂等、cancel、tier1/tier2、R1–R5)。内容取自 spec §3–§7。
- [ ] **Step 2: 更新** `agent-loop.md` 的 turn 数据流(加挂起分支)、`capabilities/README.md` 索引。
- [ ] **Step 3: 写 ADR** —— 记三条决策:① 为什么挂起=turn 结局而非阻塞 channel(对 codex/hermes/claw-code 的差异);② 为什么额外持久化 SuspensionRecord(对 codex `resume_thread_from_rollout` 的差异);③ 为什么用通用四类挂起超集吸收原 `hitl-user-input-suspend-resume`(而非两层并存)。`Supersedes` 留空。
- [ ] **Step 4: 更新** `configurable-knobs.md`。
- [ ] **Step 5: 调和 openspec change** —— 主树有未提交的 openspec change `openspec/changes/hitl-user-input-suspend-resume`(窄方案)。改写它为本超集形状:proposal 的 What/Capabilities 扩为四类挂起(permission/form/data/system_retry)、Op 由 `provide_tool_result(call_id,result)` 收敛为 `Resume(thread_id,{request_id:payload})`(form/data 场景 request_id↔related_call_id 等价)、新增 `request_user_input` 工具归入 form/data 触发器、多挂起点并存取代"单 pending"、`turn_suspended` 对齐本设计的 data 字段;spec.md 的 Requirements 同步改写。**保留**其优秀的"纯 history-gap + tool-result 回灌"叙述作为 form/data 子路径说明。改后 `cd <主树> && openspec validate hitl-user-input-suspend-resume`(若装了 openspec CLI;否则人工核对)。
- [ ] **Step 6: commit**

```bash
git add docs/
git commit -m "docs(suspend): 能力契约 + agent-loop 活文档 + ADR + knobs"
# openspec change 在主树(未提交、不在本 worktree),其调和由集成阶段在主树单独处理
```

> 注:openspec change 目录在**主树**(`/Volumes/Codes/Qiuben/qiuben/taifeng/openspec/`),不在本 worktree。Step 5 的改写在集成阶段于主树执行(或由控制方单独安排),不混入本分支的 `docs/` commit。

> **硬约束(CLAUDE.md)**:architecture / 契约未同步 → PR 不合并。Task 14 是合并前置条件,不可省。

---

## Phase 5b — 采集型触发工具(吸收 openspec change)

### Task 15: `request_user_input` 内置工具

> **执行时机**:在 Task 8 之后、Task 12(engine e2e)之前。它是 reason=form/data 挂起的**用户面触发器**,吸收自原 openspec change `hitl-user-input-suspend-resume`。机制完全复用 Task 6/7:工具被调用时抛 `SuspendSignal(reason=DATA)`,`dispatch_batch` 捕获 → turn 留 `function_call` gap + 落 `SuspensionRecord` → 挂起。resume 时该 `request_id` 的 payload 经 resolver 直接回填成 `function_call_output`(等价原方案的 `provide_tool_result`)。

**Files:**
- Create: `src/taifeng/tool/builtins/request_user_input.py`
- Modify: 内置工具注册处(读 `src/taifeng/tool/builtins/__init__.py` 看 `file_read`/`shell_exec` 等如何注册,照葫芦画瓢)
- Test: `tests/test_suspend.py`

- [ ] **Step 1: 先读现有 builtin** —— 打开 `src/taifeng/tool/builtins/file_io.py` 或 `shell.py` 看 `ToolSpec` 构造(name/description/parameters JSON Schema/`parallel_safe`)、handler 签名(`async def handler(args, ctx) -> ToolResult`,`ctx: ToolContext` 含 `call_id`),以及 `builtins/__init__.py` 的注册列表。务必匹配既有风格。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_suspend.py 追加
async def test_request_user_input_raises_form_suspend():
    """request_user_input 被调用 → 抛 SuspendSignal(reason=DATA),
    related_call_id == 本次 call_id,prompt/response_schema 进 detail(不透明透传)。"""
    import pytest
    from taifeng.suspend.reason import SuspendReason
    from taifeng.suspend.signal import SuspendSignal
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
    spec = make_request_user_input_tool()
    assert spec.name == "request_user_input"
    assert spec.parallel_safe is False
    # 构造一个最小 ToolContext(参考既有 builtin 测试的 ctx 搭建)
    ctx = _make_tool_ctx(call_id="call_xyz")   # helper:见既有 builtins 测试
    with pytest.raises(SuspendSignal) as ei:
        await spec.handler({"prompt": "你的年龄?", "response_schema": {"type": "integer"}}, ctx)
    p = ei.value.pending
    assert p.reason is SuspendReason.DATA
    assert p.related_call_id == "call_xyz"
    assert p.detail["prompt"] == "你的年龄?"
    assert p.detail["response_schema"] == {"type": "integer"}


async def test_request_user_input_empty_prompt_rejected():
    """空 prompt → typed error(禁静默占位)。"""
    import pytest
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
    spec = make_request_user_input_tool()
    ctx = _make_tool_ctx(call_id="c1")
    with pytest.raises(ValueError):
        await spec.handler({"prompt": "", "response_schema": {}}, ctx)
```

> `_make_tool_ctx` helper:参考既有 builtin 测试(grep `ToolContext(` in tests/)构造最小 ctx;若无现成 helper,在本测试文件内写一个返回 `ToolContext(call_id=..., ...)` 的小工厂,字段按 `tool/spec.py::ToolContext` 必填项填默认。

- [ ] **Step 3: 跑测试确认失败** — `PYTHONPATH=src uv run pytest tests/test_suspend.py -k request_user_input -v` → FAIL(模块不存在)。

- [ ] **Step 4: 实现**

```python
# src/taifeng/tool/builtins/request_user_input.py
"""request_user_input —— 采集型 HITL 触发工具。

参照 openspec change hitl-user-input-suspend-resume 的工具定义。差异:不引入独立
provide_tool_result Op,而是统一走 SuspendSignal(reason=DATA)→ TurnSuspended →
Resume({request_id: payload}),payload 经 resolver 回填成本次 call 的 function_call_output。

LLM 调用本工具向人类发问;response_schema 为不透明 passthrough(R1,内核不解析)。
本工具 parallel_safe=False。被调用时不同步返回 result,而是抛 SuspendSignal 让 turn 挂起。
"""
from __future__ import annotations

from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.signal import SuspendSignal
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec   # 按实际 spec.py 导出名调整


def make_request_user_input_tool() -> ToolSpec:
    """构造 request_user_input ToolSpec(注册进内置工具表)。"""

    async def handler(args: dict, ctx: ToolContext) -> ToolResult:
        # 边界校验:prompt 必填非空(系统边界,禁静默占位)
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("request_user_input_empty_prompt: prompt 必须为非空字符串")
        response_schema = args.get("response_schema") or {}
        # 不同步返回 → 抛挂起信号;related_call_id 锚定本次 call,resume 时回填其 output
        raise SuspendSignal(PendingRequest(
            request_id=ctx.call_id,                 # form/data 场景 request_id 直接用 call_id
            reason=SuspendReason.DATA,
            payload_schema=response_schema,          # 不透明透传
            related_call_id=ctx.call_id,
            detail={"prompt": prompt, "response_schema": response_schema},
        ))

    return ToolSpec(
        name="request_user_input",
        description=(
            "向人类发起一个结构化问询并等待回答。调用本工具会挂起当前 turn 直到收到回答。"
            "应作为该 step 唯一的工具调用发起。参数:prompt(问询文本)、response_schema(回答的 JSON Schema)。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "向人类展示的问询文本"},
                "response_schema": {"type": "object", "description": "期望回答的 JSON Schema(不透明)"},
            },
            "required": ["prompt"],
        },
        handler=handler,
        parallel_safe=False,
    )
```

> 注:`ToolSpec` 字段名(`handler`/`parameters`/`parallel_safe`)与 `ToolResult`/`ToolContext` 的确切签名以 `src/taifeng/tool/spec.py` 为准——Step 1 已要求先读。若注册是通过一个 `BUILTIN_TOOLS` 列表 / `register_builtins()`,把 `make_request_user_input_tool()` 加进去。

- [ ] **Step 5: 注册 + 跑测试 + 回归**
- 在 `builtins/__init__.py`(或既有注册入口)注册本工具。
- `PYTHONPATH=src uv run pytest tests/test_suspend.py -k request_user_input -v`(expect pass)
- `PYTHONPATH=src uv run pytest tests/ -k "tool or builtin" -v`(回归:工具注册/schema 不破)
- `uv run ruff check src/taifeng/tool/builtins/request_user_input.py tests/test_suspend.py`

- [ ] **Step 6: commit**

```bash
git add src/taifeng/tool/builtins/request_user_input.py src/taifeng/tool/builtins/__init__.py tests/test_suspend.py
git commit -m "feat(tool): request_user_input 采集型挂起触发工具(吸收 openspec hitl-user-input)"
```

---

## 收尾(全部 task 完成后,人工集成)

1. 全量验证:`PYTHONPATH=src uv run pytest tests/ -v` + `uv run ruff check src/ tests/` + `uv run mypy src/` 全绿。
2. 复述实际命令 + 关键输出(DoD)。
3. 合并:回主树 `git merge feat/suspend-resume`,跑全量测试。
4. 清理:`git worktree remove .claude/worktrees/suspend-resume` + `git branch -d feat/suspend-resume`。

---

## Self-Review(plan ↔ spec 覆盖核对)

- spec §3 三动作 → Task 5/6/7(挂起点抛信号)、Task 7(退栈落盘)、Task 10/12(Resume 续跑)。✅
- spec §4 数据契约 → Task 1(reason/PendingRequest)、Task 4(SuspensionRecord)、Task 10(Resume)。✅
- spec §5.1 人类输入流 → Task 7/12。✅ §5.2 系统态流 → Task 8(转挂起)、Task 12(resample)。✅ §5.3 判据 → Task 8 `_should_suspend_on_error`。✅
- spec §6 边界(不全/多余/不存在/重复/cancel/schema)→ Task 11(validate)、Task 12(engine 拒绝分支)、Task 13(边界测试)。✅
- spec §7 R1–R5 → tail-append(Task 7)、cache_invalidated(Task 9 event)、3 EventMsg(Task 9)、cancel(Task 13)、JSONL resume(Task 4/12)。✅
- spec §8 测试 → Task 7/12/13 覆盖全部列举用例。✅
- spec §9 文档义务 → Task 14。✅
- 类型一致性核对:`SuspendReason`/`PendingRequest`/`SuspensionRecord`/`ResolvePlan`/`Resume`/`TurnOutcome.suspension`/`ToolCallOutcome.suspend` 在各 task 间命名一致。✅
- 占位符扫描:无 TBD;唯二「按既有 fixture 补全」在 Task 7/12/13 的测试搭建步(因 e2e fixture 依赖本仓 `test_engine_e2e.py` 既有 helper,指明了来源文件与 MockClient 脚本要求,非逻辑占位)。
