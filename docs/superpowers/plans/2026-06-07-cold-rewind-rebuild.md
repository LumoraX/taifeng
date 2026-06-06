# Turn-Rewind 冷场景重建 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让冷 worker / 跨进程加载一条 thread 后,能为其任意历史 turn 重建 rewind 可寻址节点表,使 `re_reason` / `retry_tool` 冷热行为一致(含压缩过 / 历史 rewind 过的 thread)。

**Architecture:** 新增 `reconstruct_logical_history`(把 append-only transcript 顺序重放成与热内存等价的逻辑 history,消费 `compacted.replaced_range` + 折叠 salvage note + 按 rewind/rollback marker 的 `cut_index` 截断),冷加载让 `engine._history` = 逻辑 history;再用纯函数 `derive_rewind_log` 在其上现算节点表。`derive_rewind_log` 成为冷加载 / 热 turn 结束 / CompactNow 三处唯一产出方。node_id 统一 turn 限定 `t{k}:it{n}` / `t{k}:disp{m}`。

**Tech Stack:** Python 3.12+,anyio,pytest(asyncio_mode=auto),src-layout(测试需 `PYTHONPATH=src`),MockClient(禁真实 LLM)。

**设计来源:** [`docs/superpowers/specs/2026-06-07-cold-rewind-rebuild-design.md`](../specs/2026-06-07-cold-rewind-rebuild-design.md)(经 5 轮评审定稿)。

**运行测试统一前缀:** `cd <worktree>; PYTHONPATH=src uv run pytest <file> -v`

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `src/taifeng/conversation/models.py` | `system_injection()` 支持 additive `extra` payload | 改 |
| `src/taifeng/conversation/reconstruct.py` | `reconstruct_logical_history(raw)` —— transcript → 逻辑 history | 新建 |
| `src/taifeng/loop/rewind.py` | `count_turns` helper、`RewindCheckpoint.turn_index`、turn 限定 node_id、`derive_rewind_log` | 改 |
| `src/taifeng/loop/turn.py` | `record_*` 传入 `k=count_turns(history_buffer)` | 改 |
| `src/taifeng/loop/event.py` | 新增 `RewindTableRebuilt` EventMsg | 改 |
| `src/taifeng/loop/engine.py` | 冷加载 reconstruct+derive、热路径 re-derive、marker 补 cut_index、`_handle_rewind` 惰性 resolve | 改 |
| `tests/conversation/test_reconstruct.py` | reconstruct 全用例 | 新建 |
| `tests/loop/test_rewind_cold.py` | derive + 冷 rewind 全用例 | 新建 |
| `tests/loop/test_turn_rewind.py` | 既有用例随 node_id 迁移更新 | 改 |

---

## Task 1: `system_injection()` 支持 additive `extra` payload

**Files:**
- Modify: `src/taifeng/conversation/models.py`(`system_injection` 函数,约 :158)
- Test: `tests/conversation/test_models_system_injection.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/conversation/test_models_system_injection.py
"""system_injection 的 additive extra payload(供 rewind/rollback marker 落 cut_index)。"""
from __future__ import annotations

from taifeng.conversation.models import system_injection


def test_system_injection_without_extra_unchanged():
    """不传 extra 时 payload 仅 text + source(向后兼容)。"""
    item = system_injection("hi", thread_id="t1", source="rewind")
    assert item.payload == {"text": "hi", "source": "rewind"}


def test_system_injection_merges_extra():
    """传 extra 时合并进 payload,不覆盖 text/source。"""
    item = system_injection("hi", thread_id="t1", source="rewind", extra={"cut_index": 7})
    assert item.payload == {"text": "hi", "source": "rewind", "cut_index": 7}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/conversation/test_models_system_injection.py -v`
Expected: FAIL —— `TypeError: system_injection() got an unexpected keyword argument 'extra'`

- [ ] **Step 3: 改实现**

把 `src/taifeng/conversation/models.py` 的 `system_injection` 改为:

```python
def system_injection(
    text: str, *, thread_id: str, source: str, extra: dict[str, Any] | None = None
) -> ResponseItem:
    """系统注入项(marker / 指令 / digest)。

    extra:可选附加 payload(如 rewind/rollback marker 的 cut_index),合并进 payload。
    不传则行为与旧版完全一致(向后兼容)。
    """
    payload: dict[str, Any] = {"text": text, "source": source}
    if extra:
        payload.update(extra)
    return ResponseItem(kind="system_injection", thread_id=thread_id, payload=payload)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/conversation/test_models_system_injection.py -v`
Expected: PASS(2 passed)

- [ ] **Step 5: 回归(既有 system_injection 调用方不受影响)**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_turn_rewind.py -v`
Expected: PASS(既有 marker 调用未传 extra,行为不变)

- [ ] **Step 6: Commit**

```bash
git add src/taifeng/conversation/models.py tests/conversation/test_models_system_injection.py
git commit -m "feat(conversation): system_injection 支持 additive extra payload

为 rewind/rollback marker 落 cut_index 铺路;不传 extra 向后兼容。"
```

---

## Task 2: `count_turns` helper + `turn_index` 字段 + turn 限定 node_id

**Files:**
- Modify: `src/taifeng/loop/rewind.py`(`RewindCheckpoint`、`RewindLog.record_*`,新增 `count_turns`)
- Modify: `src/taifeng/loop/turn.py`(:571-576 与 :820-829 两处 `record_*` 调用,传入 `turn_index`)
- Test: `tests/loop/test_rewind_node_id.py`
- Modify: `tests/loop/test_turn_rewind.py`(node_id 引用迁移)

- [ ] **Step 1: 写失败测试**

```python
# tests/loop/test_rewind_node_id.py
"""turn 限定 node_id + count_turns 结构化 turn 序号。"""
from __future__ import annotations

from taifeng.conversation.models import assistant_message, user_message
from taifeng.loop.rewind import RewindLog, count_turns


def _hist(*kinds: str) -> list:
    """造一串只关心 kind 的 ResponseItem。"""
    out = []
    for k in kinds:
        if k == "user":
            out.append(user_message("u", thread_id="t"))
        else:
            out.append(assistant_message("a", thread_id="t", model="m"))
    return out


def test_count_turns_counts_user_messages():
    """count_turns = 累积 user_message 数(1-based 的最大 k)。"""
    assert count_turns([]) == 0
    assert count_turns(_hist("user", "assistant", "user")) == 2


def test_record_iteration_node_id_is_turn_qualified():
    log = RewindLog()
    cp = log.record_iteration(turn_index=2, iteration_index=3, history_len=5, cache_anchor=-1)
    assert cp.node_id == "t2:it3"
    assert cp.turn_index == 2


def test_record_dispatch_node_id_is_turn_qualified():
    log = RewindLog()
    cp = log.record_dispatch(
        turn_index=1, iteration_index=1, iteration_history_len=4, cache_anchor=-1,
        call_id="c1", target_id="read_skill", inner_history_len=6, args_digest="{}",
    )
    assert cp.node_id == "t1:disp0"
    assert cp.turn_index == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_node_id.py -v`
Expected: FAIL —— `ImportError: cannot import name 'count_turns'` / `record_iteration() got an unexpected keyword argument 'turn_index'`

- [ ] **Step 3: 改 `rewind.py`**

在 `src/taifeng/loop/rewind.py`:

(a) `RewindCheckpoint` 增字段(放在 `node_id` 之后):

```python
    node_id: str
    turn_index: int
    """所属 root turn 序号 k(= 累积 user_message 数,1-based)。"""
    kind: RewindKind
```

(b) `record_iteration` 改签名 + node_id:

```python
    def record_iteration(
        self, *, turn_index: int, iteration_index: int, history_len: int, cache_anchor: int
    ) -> RewindCheckpoint:
        """记一圈 LLM 采样前的 iteration 节点。"""
        cp = RewindCheckpoint(
            node_id=f"t{turn_index}:it{iteration_index}",
            turn_index=turn_index,
            kind="iteration",
            history_len=history_len,
            cache_anchor=cache_anchor,
            iteration_index=iteration_index,
        )
        self.checkpoints.append(cp)
        return cp
```

(c) `record_dispatch` 改签名 + node_id:

```python
    def record_dispatch(
        self, *, turn_index: int, iteration_index: int, iteration_history_len: int,
        cache_anchor: int, call_id: str, target_id: str, inner_history_len: int, args_digest: str,
    ) -> RewindCheckpoint:
        """记一次工具 / call_skill 派发的 dispatch 节点(两个切点)。"""
        cp = RewindCheckpoint(
            node_id=f"t{turn_index}:disp{self._dispatch_seq}",
            turn_index=turn_index,
            kind="dispatch",
            history_len=iteration_history_len,
            cache_anchor=cache_anchor,
            iteration_index=iteration_index,
            call_id=call_id,
            target_id=target_id,
            inner_history_len=inner_history_len,
            args_digest=args_digest,
        )
        self._dispatch_seq += 1
        self.checkpoints.append(cp)
        return cp
```

(d) 文件顶部加 `count_turns`(放在 import 之后、`RewindKind` 之前):

```python
from taifeng.conversation.models import ResponseItem


def count_turns(history: list[ResponseItem]) -> int:
    """累积 user_message 数 —— 结构化 turn 序号 k 的真相来源。

    derive 与 live 记录共用此 helper,保证 node_id 的 t{k} 前缀热冷一致。
    """
    return sum(1 for it in history if it.kind == "user_message")
```

- [ ] **Step 4: 改 `turn.py` 两处 `record_*` 调用传入 `turn_index`**

`src/taifeng/loop/turn.py` 顶部 import 加 `count_turns`:

```python
from taifeng.loop.rewind import RewindLog, count_turns
```

`_sample_once` 内(约 :571,`record_iteration` 调用)改为:

```python
        if self._is_root:
            cp = self.rewind_log.record_iteration(
                turn_index=count_turns(self.history_buffer),
                iteration_index=iteration,
                history_len=iteration_history_len,
                cache_anchor=self.cache_anchor_index,
            )
```

dispatch 记录处(约 :820,`record_dispatch` 调用)改为:

```python
            if self._is_root:
                dcp = self.rewind_log.record_dispatch(
                    turn_index=count_turns(self.history_buffer),
                    iteration_index=iteration,
                    iteration_history_len=iteration_history_len,
                    cache_anchor=self.cache_anchor_index,
                    call_id=req.call_id,
                    target_id=req.name,
                    inner_history_len=len(self.history_buffer),
                    args_digest=req.arguments_raw[:200],
                )
```

> 注:root turn 起跑时 `history_buffer` 已含本 turn 的 `user_message`(由 engine 在建 runner 前追加),故 `count_turns` 在本 turn 任意时刻返回的 k 稳定 = 本 turn 序号。

- [ ] **Step 5: 迁移既有测试 node_id 引用**

`tests/loop/test_turn_rewind.py` 把单 turn 的 `it{n}` / `disp{n}` 引用加 `t1:` 前缀:
- :75 `Rewind(node_id="it3")` → `Rewind(node_id="t1:it3")`
- :84 `Rewind(node_id="disp2", ...)` → `Rewind(node_id="t1:disp2", ...)`
- :92 / :95 `"disp0"` → `"t1:disp0"`
- :250 `Rewind(node_id="it2", mode="re_reason")` → `Rewind(node_id="t1:it2", ...)`;:246-248 选 `it2` 的查找改 `n.node_id == "t1:it2"`

> :102 的 `node_id="x"`、:214 的 `"does-not-exist"` 是无效/未知节点用例,**不改**(本就该被拒)。

- [ ] **Step 6: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_node_id.py tests/loop/test_turn_rewind.py -v`
Expected: PASS(新文件全过 + test_turn_rewind 迁移后全过)

- [ ] **Step 7: Commit**

```bash
git add src/taifeng/loop/rewind.py src/taifeng/loop/turn.py tests/loop/test_rewind_node_id.py tests/loop/test_turn_rewind.py
git commit -m "feat(rewind): node_id 统一 turn 限定 t{k}: + count_turns 结构化 turn 序号

为冷场景多 turn 寻址铺路;k 走 user_message 累积计数,不依赖 _turn_index。
Supersedes #0014 的 node_id 格式段。"
```

---

## Task 3: `reconstruct_logical_history`

**Files:**
- Create: `src/taifeng/conversation/reconstruct.py`
- Test: `tests/conversation/test_reconstruct.py`

- [ ] **Step 1: 写失败测试(恒等 + 压缩 + salvage + 截断 + 缺 cut_index)**

```python
# tests/conversation/test_reconstruct.py
"""reconstruct_logical_history —— transcript 重放成逻辑 history。"""
from __future__ import annotations

import pytest

from taifeng.conversation.models import (
    assistant_message, compacted, function_call, system_injection, user_message,
)
from taifeng.conversation.reconstruct import reconstruct_logical_history

T = "thr"


def _u(n): return user_message(f"u{n}", thread_id=T)
def _a(n): return assistant_message(f"a{n}", thread_id=T, model="m")


def test_clean_thread_is_identity():
    """无压缩/无 rewind marker → 原样返回。"""
    raw = [_u(1), _a(1), _u(2), _a(2)]
    assert reconstruct_logical_history(raw) == raw


def test_single_compaction_folds_replaced_range():
    """[head, mid, tail, PH@end] → [head, PH, tail]。"""
    head, mid, tail = _u(1), _a(1), _u(2)
    # 压缩发生时内存 history = [head, mid, tail](len 3),replaced_range=(1,2) 替换 mid
    ph = compacted("s", thread_id=T, replaced_range=(1, 2), cache_invalidated=True)
    raw = [head, mid, tail, ph]
    assert reconstruct_logical_history(raw) == [head, ph, tail]


def test_compaction_with_salvage_note_placed_after_placeholder():
    """store 尾 [..., note, PH] → 逻辑 [head, PH, note, tail](note 挪到 PH 后)。"""
    head, mid, tail = _u(1), _a(1), _u(2)
    note = system_injection("digest", thread_id=T, source="memory_pre_evict")
    ph = compacted("s", thread_id=T, replaced_range=(1, 2), cache_invalidated=True)
    raw = [head, mid, tail, note, ph]  # 写序:note 在 PH 前
    assert reconstruct_logical_history(raw) == [head, ph, note, tail]


def test_rewind_marker_truncates_to_cut_index():
    """[保留, 废弃尾, rewind_marker(cut_index=1), re-run] → [保留[:1], re-run]。"""
    keep, dead = _u(1), _a(1)
    marker = system_injection("[rewind]", thread_id=T, source="rewind", extra={"cut_index": 1})
    rerun = _a(2)
    raw = [keep, dead, marker, rerun]
    assert reconstruct_logical_history(raw) == [keep, rerun]


def test_rollback_marker_truncates_to_cut_index():
    keep = _u(1)
    dead = _a(1)
    marker = system_injection("[rollback]", thread_id=T, source="rollback", extra={"cut_index": 1})
    raw = [keep, dead, marker]
    assert reconstruct_logical_history(raw) == [keep]


def test_missing_cut_index_raises():
    """rewind/rollback marker 缺 cut_index → 显式报错,不静默猜。"""
    marker = system_injection("[rewind]", thread_id=T, source="rewind")  # 无 extra
    with pytest.raises(ValueError, match="cut_index"):
        reconstruct_logical_history([_u(1), _a(1), marker])


def test_nested_compaction_folds_in_order():
    """两次压缩顺序折叠:第二个 PH 的 range 针对已折叠的 history。"""
    head = _u(1)
    a1, a2 = _a(1), _a(2)
    ph1 = compacted("s1", thread_id=T, replaced_range=(1, 2), cache_invalidated=True)  # 替换 a1
    # 第一次压缩后内存 = [head, ph1, a2](len 3);第二次替换 a2(index 2)
    ph2 = compacted("s2", thread_id=T, replaced_range=(2, 3), cache_invalidated=True)
    raw = [head, a1, a2, ph1, ph2]
    # 重放:[head,a1,a2] → 遇 ph1 → [head,ph1,a2] → 遇 ph2(range 2,3 针对 [head,ph1,a2]) → [head,ph1,ph2]
    assert reconstruct_logical_history(raw) == [head, ph1, ph2]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/conversation/test_reconstruct.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'taifeng.conversation.reconstruct'`

- [ ] **Step 3: 写实现**

```python
# src/taifeng/conversation/reconstruct.py
"""reconstruct_logical_history —— 把 append-only transcript 顺序重放成逻辑 history。

append-only 主存(R5)在两种情况下与热内存 history 结构性发散:
1. 压缩:被替换的中间 item 不删,placeholder append 到末尾(replaced_range 记区间)。
2. 历史 rewind/rollback:被截断的 item 仍物理留存,marker 记 cut_index。

本函数顺序重放 transcript,复现热内存 history:折叠压缩区间、挪 salvage note、
按 cut_index 截断。对未压缩/未 rewind 的干净 thread 是恒等映射。纯 CPU、无 IO。

设计:docs/superpowers/specs/2026-06-07-cold-rewind-rebuild-design.md §4
"""

from __future__ import annotations

from taifeng.conversation.models import ResponseItem

# 触发截断的 marker source(rewind / rollback 在内存截断 history,store 仅留 marker)
_TRUNCATING_SOURCES = frozenset({"rewind", "rollback"})
# 压缩 salvage digest 的 source(store 里在 placeholder 前,内存里在 placeholder 后)
_SALVAGE_SOURCE = "memory_pre_evict"


def reconstruct_logical_history(raw: list[ResponseItem]) -> list[ResponseItem]:
    """顺序重放 transcript → 与热内存等价的逻辑 history。

    参数 raw:`MessageStore.load_thread` 按写入序返回的全部 item(保序 + 完整)。
    抛 ValueError:rewind/rollback marker 缺 cut_index(不静默猜下标)。
    """
    logical: list[ResponseItem] = []
    for item in raw:
        if item.kind == "compacted":
            # 折叠被替换区间;若紧邻前一项是 salvage note,挪到 placeholder 之后
            start, end = item.payload["replaced_range"]
            salvage = None
            if logical and _is_salvage(logical[-1]):
                salvage = logical.pop()
                # 边界断言(扩展边界):自定义压缩策略可能产孤儿 note,
                # 仅当 note 紧贴 placeholder(写序连续)才配对——此处恒成立,
                # 因内置策略 note(:345)紧接 placeholder(:1094),中间无 append。
            tail_extra = [salvage] if salvage is not None else []
            logical = logical[:start] + [item] + tail_extra + logical[end:]
        elif item.kind == "system_injection" and item.payload.get("source") in _TRUNCATING_SOURCES:
            cut = item.payload.get("cut_index")
            if cut is None:
                raise ValueError(
                    f"rewind/rollback marker 缺 cut_index,无法重建逻辑 history:{item.id}"
                )
            logical = logical[:cut]
            # marker 本身不进 logical(热路径只落 store、不进 _history)
        else:
            logical.append(item)
    return logical


def _is_salvage(item: ResponseItem) -> bool:
    """是否压缩 salvage digest(memory_pre_evict note)。"""
    return item.kind == "system_injection" and item.payload.get("source") == _SALVAGE_SOURCE


__all__ = ["reconstruct_logical_history"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/conversation/test_reconstruct.py -v`
Expected: PASS(7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/conversation/reconstruct.py tests/conversation/test_reconstruct.py
git commit -m "feat(conversation): reconstruct_logical_history —— transcript 重放成逻辑 history

消费 compacted.replaced_range + salvage note 挪位 + rewind/rollback cut_index 截断;
干净 thread 恒等。修正冷加载/resume 与热内存 history 的结构性发散。"
```

---

## Task 4: `derive_rewind_log`(在逻辑 history 上推导节点表)

**Files:**
- Modify: `src/taifeng/loop/rewind.py`(新增 `derive_rewind_log`)
- Test: `tests/loop/test_rewind_cold.py`(derive 部分)

- [ ] **Step 1: 写失败测试(结构推导 + 空尾圈 + 同圈多派发)**

```python
# tests/loop/test_rewind_cold.py
"""derive_rewind_log 推导 + 冷场景 rewind。"""
from __future__ import annotations

from taifeng.conversation.models import (
    assistant_message, function_call, function_call_output, user_message,
)
from taifeng.loop.rewind import derive_rewind_log

T = "thr"


def _u(): return user_message("u", thread_id=T)
def _a(): return assistant_message("a", thread_id=T, model="m")
def _fc(cid, name="read_skill", args="{}"): return function_call(cid, name, args, thread_id=T)
def _fco(cid): return function_call_output(cid, output="ok", thread_id=T, is_error=False)


def test_derive_single_turn_iterations_and_dispatch():
    """一 turn:[u, a, fc, fco, a](2 圈,首圈 1 派发)→ 2 iteration + 1 dispatch。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1"), _a()]
    nodes = derive_rewind_log(hist)
    ids = [n.node_id for n in nodes]
    assert ids == ["t1:it1", "t1:disp0", "t1:it2"]
    disp = next(n for n in nodes if n.node_id == "t1:disp0")
    assert disp.history_len == 1          # 所属 it1 的 history_len(assistant_message 下标)
    assert disp.inner_history_len == 3    # fc 下标(2)+1
    assert disp.call_id == "c1"


def test_derive_same_iteration_multiple_dispatch_share_history_len():
    """同圈 2 派发:[u, a, fc1, fco1, fc2, fco2] → 两 dispatch 共享 it1 的 history_len=1。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1"), _fc("c2"), _fco("c2")]
    nodes = derive_rewind_log(hist)
    disps = [n for n in nodes if n.kind == "dispatch"]
    assert [d.node_id for d in disps] == ["t1:disp0", "t1:disp1"]
    assert all(d.history_len == 1 for d in disps)        # 归一到 it1 采样前
    assert [d.inner_history_len for d in disps] == [3, 5]  # 各自 fc 下标+1


def test_derive_empty_trailing_iteration_produces_no_node():
    """末圈空采样(无 assistant_message)不留 item → 不产节点。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1")]  # 首圈派发,末圈空(无尾 assistant)
    nodes = derive_rewind_log(hist)
    assert [n.node_id for n in nodes] == ["t1:it1", "t1:disp0"]


def test_derive_multi_turn_addressable():
    """两 turn → t1 / t2 前缀分别可寻址。"""
    hist = [_u(), _a(), _u(), _a()]
    ids = [n.node_id for n in derive_rewind_log(hist)]
    assert ids == ["t1:it1", "t2:it1"]


def test_derive_ignores_unknown_kinds_for_index():
    """spawn 等未知 kind 计入下标、不产节点,后续下标不偏移。"""
    from taifeng.conversation.models import spawn_item
    sp = spawn_item(handle_id="h", skill_id="s", child_thread_id="c", thread_id=T)
    hist = [_u(), sp, _a(), _fc("c1"), _fco("c1")]
    nodes = derive_rewind_log(hist)
    it1 = next(n for n in nodes if n.node_id == "t1:it1")
    disp = next(n for n in nodes if n.node_id == "t1:disp0")
    assert it1.history_len == 2          # assistant_message 在 spawn 之后,下标 2
    assert disp.inner_history_len == 4   # fc 下标(3)+1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py -v`
Expected: FAIL —— `ImportError: cannot import name 'derive_rewind_log'`

- [ ] **Step 3: 写实现(在 `rewind.py` 末尾,`__all__` 之前)**

```python
def derive_rewind_log(history: list[ResponseItem]) -> list[RewindCheckpoint]:
    """从逻辑 history(reconstruct 后)推导全 turn 可寻址节点表。

    输入须是与热内存等价的逻辑 history(见 reconstruct_logical_history),故下标
    与 engine 后续 _history[:cut] 截断同坐标系、自洽。纯 CPU、无副作用。

    规则(按 ItemKind 扫一遍,见 spec §5.2):
    - user_message: 进入新 turn k(=已见 user_message 数),重置 n/m,清当前圈游标
    - assistant_message: 记 iteration 节点(history_len=本项下标,存入游标)
    - function_call: 记 dispatch 节点(history_len=当前圈游标,inner=fc 下标+1)
    - 其余 kind(含 compacted/system_injection/spawn/...): 计入下标、不产节点(default)
    """
    log = RewindLog()
    k = 0
    iteration = 0  # 本 turn 内 1-based 采样圈序号
    cur_iter_history_len: int | None = None  # 当前圈采样前 history 长度(dispatch 归一切点)
    for idx, item in enumerate(history):
        if item.kind == "user_message":
            k += 1
            iteration = 0
            cur_iter_history_len = None
            log = _reset_dispatch_seq_for_turn(log)
        elif item.kind == "assistant_message":
            iteration += 1
            cur_iter_history_len = idx
            log.record_iteration(
                turn_index=k, iteration_index=iteration,
                history_len=idx, cache_anchor=-1,
            )
        elif item.kind == "function_call":
            # dispatch 归一到所属圈采样前;无前导 assistant 时退化用 fc 自身下标
            base = cur_iter_history_len if cur_iter_history_len is not None else idx
            log.record_dispatch(
                turn_index=k, iteration_index=max(iteration, 1),
                iteration_history_len=base, cache_anchor=-1,
                call_id=item.payload["call_id"], target_id=item.payload["name"],
                inner_history_len=idx + 1, args_digest=item.payload["arguments"][:200],
            )
        # 其余 kind:default,只占下标(idx 已 +1),不产节点
    return log.checkpoints


def _reset_dispatch_seq_for_turn(log: RewindLog) -> RewindLog:
    """跨 turn 重置 dispatch 序号(disp 编号 turn 内从 0 起)。"""
    log._dispatch_seq = 0
    return log
```

> 注:`RewindLog._dispatch_seq` 是模块内私有字段,`derive_rewind_log` 与 `RewindLog` 同模块,直接访问合规(非跨模块 SLF001)。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py -v`
Expected: PASS(5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/rewind.py tests/loop/test_rewind_cold.py
git commit -m "feat(rewind): derive_rewind_log —— 从逻辑 history 推导全 turn 节点表

纯函数,下标自洽;同圈多派发归一、空尾圈不产节点、未知 kind 只占下标。"
```

---

## Task 5: 奇偶校验测试(derive ≡ live RewindLog)

**Files:**
- Test: `tests/loop/test_rewind_cold.py`(追加奇偶校验)

> 这是 derive 与热记录下标语义一致性的核心背书(spec §5.2)。用 MockClient 跑真实热 turn,把 engine `_history` 喂 derive,断言节点与 runner live `rewind_log` 一致。

- [ ] **Step 1: 写奇偶校验测试**

```python
# 追加到 tests/loop/test_rewind_cold.py
import pytest

from tests.loop._rewind_helpers import build_engine_with_mock  # 见 Step 2 复用既有 helper


async def test_parity_derive_equals_live_recording(tmp_path):
    """真实热 turn(多圈 + 多派发 + 空尾圈)→ derive(_history) ≡ live rewind_log。

    锁死 derive 与 turn.py 记录的下标语义一致(spec §5.2 背书)。
    """
    engine, captured_log = await build_engine_with_mock(tmp_path)
    await engine.run_one_turn("分析")  # helper 内封装:submit UserMessage + 等 TurnComplete
    live = captured_log.checkpoints            # runner 回写前抓的 live RewindLog
    derived = derive_rewind_log(engine.history_snapshot())
    assert [c.node_id for c in derived] == [c.node_id for c in live]
    for d, l in zip(derived, live, strict=True):
        assert (d.history_len, d.inner_history_len, d.turn_index) == \
               (l.history_len, l.inner_history_len, l.turn_index)
        assert d.args_digest == l.args_digest
```

- [ ] **Step 2: 建测试 helper(复用 `test_turn_rewind.py` 的 MockClient 装配)**

> 检查 `tests/loop/test_turn_rewind.py` 顶部已有的 MockClient + engine 装配代码,抽到 `tests/loop/_rewind_helpers.py`:`build_engine_with_mock(tmp_path)` 返回 `(engine, captured_log)`,其中 MockClient 脚本设计为「圈1:read_skill 派发 → 圈2:再派发 → 圈3:空文本收尾」,`captured_log` 通过订阅 `rewind_checkpoint_recorded` 事件或在 turn 结束 hook 抓 `runner.rewind_log`。具体装配照搬 `test_turn_rewind.py` 既有 fixture(那里已有可跑的 MockClient 多圈脚本)。

- [ ] **Step 3: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py::test_parity_derive_equals_live_recording -v`
Expected: PASS

> 若 FAIL:对比 derive 与 live 的第一个分歧节点的 `history_len`,定位是空尾圈 / 同圈多派发 / spawn 占位哪类布局差异,据 spec §5.2 修 `derive_rewind_log`(不改 live)。

- [ ] **Step 4: Commit**

```bash
git add tests/loop/test_rewind_cold.py tests/loop/_rewind_helpers.py
git commit -m "test(rewind): 奇偶校验 derive ≡ live RewindLog —— 锁死下标语义一致"
```

---

## Task 6: marker 补 `cut_index`(`_handle_rewind` + `_handle_rollback`)

**Files:**
- Modify: `src/taifeng/loop/engine.py`(`_handle_rewind` marker、`_handle_rollback` marker)
- Test: `tests/loop/test_rewind_cold.py`(追加 marker payload 用例)

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/loop/test_rewind_cold.py
async def test_rewind_marker_persists_cut_index(tmp_path):
    """rewind 后 store 里的 rewind marker payload 含 cut_index。"""
    engine, _ = await build_engine_with_mock(tmp_path)
    await engine.run_one_turn("分析")
    it2 = next(n for n in engine.rewind_nodes() if n.node_id == "t1:it2")
    await engine.run_rewind(node_id="t1:it2", mode="re_reason")  # helper:submit Rewind + 等
    items = await engine._store.load_thread(engine._thread_id)  # type: ignore[attr-defined]
    rewind_markers = [
        it async for it in items
        if it.kind == "system_injection" and it.payload.get("source") == "rewind"
    ]
    assert rewind_markers and rewind_markers[-1].payload["cut_index"] == it2.history_len
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py::test_rewind_marker_persists_cut_index -v`
Expected: FAIL —— `KeyError: 'cut_index'`

- [ ] **Step 3: 改 `_handle_rewind` marker(engine.py,marker 构造处)**

找到 `_handle_rewind` 内 marker 构造(`system_injection(f"[rewind] node=...", thread_id=..., source="rewind")`),改为带 `extra`:

```python
        marker = system_injection(
            f"[rewind] node={op.node_id} kind={cp.kind} mode={op.mode}",
            thread_id=self._thread_id, source="rewind", extra={"cut_index": cut},
        )
```

(`cut` 即该处已算出、用于 `self._history[:cut]` 的截断下标。)

- [ ] **Step 4: 改 `_handle_rollback` marker(engine.py)**

找到 `_handle_rollback` 内 marker 构造(`system_injection(f"[rollback] dropped ...", source="rollback")`),改为带 `extra={"cut_index": cut_idx}`(`cut_idx` 即该处用于 `new_history[:cut_idx]` 的截断下标):

```python
        marker = system_injection(
            f"[rollback] dropped {removed} item(s), {num_turns} turn(s)",
            thread_id=self._thread_id, source="rollback", extra={"cut_index": cut_idx},
        )
```

- [ ] **Step 5: 跑测试 + rollback 回归**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py::test_rewind_marker_persists_cut_index tests/loop/test_turn_rewind.py -v`
Expected: PASS(含既有 rollback / rewind 用例不回归)

- [ ] **Step 6: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_rewind_cold.py
git commit -m "feat(loop): rewind/rollback marker 持久化 cut_index

供 reconstruct_logical_history 重建逻辑 history 时按 cut_index 截断。"
```

---

## Task 7: 冷加载接线(engine `__init__` reconstruct + derive)+ `RewindTableRebuilt` 事件

**Files:**
- Modify: `src/taifeng/loop/event.py`(新增 `RewindTableRebuilt`)
- Modify: `src/taifeng/loop/engine.py`(`__init__` 冷加载段)
- Test: `tests/loop/test_rewind_cold.py`(冷重建 re_reason / 压缩 thread)

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/loop/test_rewind_cold.py
async def test_cold_load_rebuilds_rewind_table(tmp_path):
    """跑完一 turn → 新建 engine 灌 initial_history → rewind_nodes() 非空且可 re_reason。"""
    engine, _ = await build_engine_with_mock(tmp_path)
    await engine.run_one_turn("分析")
    tid = engine._thread_id  # type: ignore[attr-defined]
    raw = [it async for it in await engine._store.load_thread(tid)]  # type: ignore[attr-defined]

    cold = await build_cold_engine(tmp_path, initial_history=raw, thread_id=tid)  # helper
    assert any(n.node_id == "t1:it2" for n in cold.rewind_nodes())
    await cold.run_rewind(node_id="t1:it2", mode="re_reason")
    # 截断生效:history 末项是重采样后的新 assistant,长度回到截点后再增长
    assert cold.history_snapshot()  # 不抛、非空


async def test_cold_load_empty_history_empty_table(tmp_path):
    cold = await build_cold_engine(tmp_path, initial_history=[], thread_id="new")
    assert cold.rewind_nodes() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py::test_cold_load_rebuilds_rewind_table -v`
Expected: FAIL —— `rewind_nodes()` 为空(冷加载未重建)

- [ ] **Step 3: 加 `RewindTableRebuilt` 事件(event.py)**

在 `event.py` 的 `MsgKind` Literal 加 `"rewind_table_rebuilt"`;在 rewind 事件区(约 :464 附近)加类,并加入文件末 `__all__`/导出元组:

```python
class RewindTableRebuilt(_Msg):
    """冷加载从逻辑 history 重建 rewind 节点表后发出(R3 可观测)。

    data: {"thread_id": str, "turn_count": int, "node_count": int}
    """
    kind: Literal["rewind_table_rebuilt"] = "rewind_table_rebuilt"
```

- [ ] **Step 4: 改 engine `__init__` 冷加载段**

`engine.py` 顶部 import:

```python
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.loop.rewind import RewindCheckpoint, count_turns, derive_rewind_log
```

`__init__` 中现有 `self._history = list(initial_history) if initial_history else []` 改为:

```python
        # 冷加载:先把 raw transcript 重建成逻辑 history(= 热内存等价),再推导节点表
        raw_init = list(initial_history) if initial_history else []
        self._history: list[ResponseItem] = reconstruct_logical_history(raw_init)
        ...
        # (现有 _cache_anchor_index = -1 等保持不变)
        self._rewind_checkpoints: list[RewindCheckpoint] = derive_rewind_log(self._history)
```

> `derive_rewind_log` 调用替换原 `self._rewind_checkpoints = []` 初始化行。空 `initial_history` → 空 history → 空表。

`RewindTableRebuilt` emit:engine 的 emit 在事件循环启动后才可用,故**不在 `__init__` emit**;改在 `run()` 启动后或首次被订阅时补发。最简:在 `pool.py` resume 段(`_rebuild_spawn_state_from_history` 之后)加:

```python
            if resume_thread_id is not None:
                await engine._emit_rewind_table_rebuilt()  # noqa: SLF001
```

engine 加方法:

```python
    async def _emit_rewind_table_rebuilt(self) -> None:
        """冷恢复后补发 rewind_table_rebuilt(R3);turn 序号取 history 内 user_message 数。"""
        await self._emit(EventMsg(submission_id="*", msg=RewindTableRebuilt(data={
            "thread_id": self._thread_id,
            "turn_count": count_turns(self._history),
            "node_count": len(self._rewind_checkpoints),
        })))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py -v`
Expected: PASS(冷重建用例 + 空表用例)

- [ ] **Step 6: Commit**

```bash
git add src/taifeng/loop/event.py src/taifeng/loop/engine.py src/taifeng/loop/pool.py tests/loop/test_rewind_cold.py
git commit -m "feat(loop): 冷加载 reconstruct + derive 重建 rewind 节点表 + RewindTableRebuilt 事件

engine __init__ 把 raw transcript 重建逻辑 history 后现算节点表;resume 后补发 R3 事件。"
```

---

## Task 8: 热路径 re-derive(turn 结束 + CompactNow)

**Files:**
- Modify: `src/taifeng/loop/engine.py`(:801 turn 结束、:1576 CompactNow)
- Test: `tests/loop/test_rewind_cold.py`(冷加载后再跑新 turn 号不撞)

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/loop/test_rewind_cold.py
async def test_cold_then_new_turn_node_ids_no_collision(tmp_path):
    """冷加载 1 个历史 turn → 跑新 turn → t1 / t2 前缀严格递增、无重复。"""
    engine, _ = await build_engine_with_mock(tmp_path)
    await engine.run_one_turn("第一轮")
    tid = engine._thread_id  # type: ignore[attr-defined]
    raw = [it async for it in await engine._store.load_thread(tid)]  # type: ignore[attr-defined]

    cold = await build_cold_engine(tmp_path, initial_history=raw, thread_id=tid)
    await cold.run_one_turn("第二轮")  # 暖起来后再跑一 turn
    prefixes = {n.node_id.split(":")[0] for n in cold.rewind_nodes()}
    ids = [n.node_id for n in cold.rewind_nodes()]
    assert "t1" in prefixes and "t2" in prefixes
    assert len(ids) == len(set(ids))  # 无重复 node_id
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py::test_cold_then_new_turn_node_ids_no_collision -v`
Expected: FAIL —— 新 turn 覆盖了历史表(`= list(runner.rewind_log.checkpoints)` 丢掉 t1),`t1` 不在 prefixes

- [ ] **Step 3: 改两处 writeback**

`engine.py` turn 结束处(约 :801):

```python
            # turn-rewind:对当前全量逻辑 history 重算节点表(derive 为唯一产出方)
            # self._history 已于上一行写回 runner.history_buffer
            self._rewind_checkpoints = derive_rewind_log(self._history)
```

CompactNow 处(约 :1576)同样改为:

```python
            self._rewind_checkpoints = derive_rewind_log(self._history)
```

- [ ] **Step 4: 跑测试 + 全 rewind 回归**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py tests/loop/test_turn_rewind.py -v`
Expected: PASS(号不撞用例 + 既有热 rewind 全过)

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_rewind_cold.py
git commit -m "refactor(loop): 热 turn 结束/CompactNow 改 derive_rewind_log 重算

derive 成为冷加载/热结束/CompactNow 唯一产出方,消除 re-run 撞号与覆盖丢历史。"
```

---

## Task 9: 冷 rewind 指令层(`_handle_rewind` 惰性 resolve `_last_resolved`)

**Files:**
- Modify: `src/taifeng/loop/engine.py`(`_handle_rewind` 入口)
- Test: `tests/loop/test_rewind_cold.py`(冷 rewind 带指令)

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/loop/test_rewind_cold.py
async def test_cold_rewind_resolves_instructions(tmp_path):
    """冷 engine 首次 rewind 前 _last_resolved 为空 → 惰性 resolve,re-run 带指令层。"""
    engine, _ = await build_engine_with_mock(tmp_path)
    await engine.run_one_turn("分析")
    tid = engine._thread_id  # type: ignore[attr-defined]
    raw = [it async for it in await engine._store.load_thread(tid)]  # type: ignore[attr-defined]

    cold = await build_cold_engine(tmp_path, initial_history=raw, thread_id=tid)
    assert cold._last_resolved == [] or cold._last_resolved is None  # type: ignore[attr-defined]
    await cold.run_rewind(node_id="t1:it1", mode="re_reason")
    assert cold._last_resolved  # type: ignore[attr-defined]  # 惰性 resolve 后非空
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py::test_cold_rewind_resolves_instructions -v`
Expected: FAIL —— `_last_resolved` 仍为空(冷 rewind 未 resolve)

- [ ] **Step 3: 改 `_handle_rewind` 入口**

在 `_handle_rewind` 的 checkpoint 查找之前,加惰性 resolve:

```python
        # 冷场景:engine 未跑过 turn 时 _last_resolved 为空 → 按构造 entry skill 补 resolve,
        # 使 re-run 与热 turn 起跑等价(带指令层)。已知边界:不还原 per-turn 历史 entry skill。
        if not self._last_resolved and self._history:
            await self._resolve_instructions_for_entry()  # 复用 warmup/turn 起跑的 resolve 路径
```

> 实现 `_resolve_instructions_for_entry`:抽取 live turn 起跑时填充 `_last_resolved` 的既有 resolve 逻辑(engine.py 现有 resolve 调用,约 :644 一带)为可复用方法,冷 rewind 与热路径共用。若该逻辑已是独立方法,直接调用并把结果赋给 `self._last_resolved`。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_rewind_cold.py::test_cold_rewind_resolves_instructions -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/engine.py tests/loop/test_rewind_cold.py
git commit -m "fix(loop): 冷 rewind 惰性 resolve 指令层 —— re-run 不丢 _last_resolved"
```

---

## Task 10: 全量回归 + 文档落档(收尾红线)

**Files:**
- Modify: `docs/architecture/capabilities/turn-rewind.md`
- Modify: `docs/architecture/agent-loop.md`
- Modify: `docs/architecture/conversation.md` + `docs/architecture/context-compression.md`
- Create: `docs/decisions/00NN-cold-rewind-rebuild.md`

- [ ] **Step 1: 全量测试 + lint/type**

Run:
```bash
PYTHONPATH=src uv run pytest tests/ -q
uv run ruff check src/ tests/
uv run mypy src/
```
Expected: 全绿(如有红,先定位是否本次相关,不以 pre-existing 借口跳过)。

- [ ] **Step 2: 更新能力契约 `capabilities/turn-rewind.md`**

- node_id 全部改为 `t{k}:it{n}` / `t{k}:disp{m}`;`RewindCheckpoint` 增 `turn_index` 行。
- 新增 Requirement「冷场景重建」:reconstruct + derive、任意历史 turn 可寻址、依赖 `load_thread` 保序+完整契约。
- 「边界与暂不支持」移除「冷重建不支持」;新增「per-turn 历史 entry skill 指令不还原」「自定义压缩策略孤儿 salvage note 边界」。

- [ ] **Step 3: 更新 `agent-loop.md` + `conversation.md`/`context-compression.md`**

- `agent-loop.md`:冷加载 reconstruct→derive 接线、热路径 re-derive、derive 三处唯一产出方。
- `conversation.md`:新增 `reconstruct_logical_history`(消费 replaced_range + salvage 挪位 + rewind/rollback cut_index)。
- `context-compression.md`:注明 compacted 的 `replaced_range` + salvage note 由 reconstruct 在冷加载/resume 时消费,修正既有 resume 重发被压缩内容的隐患。

- [ ] **Step 4: 新增 ADR `docs/decisions/00NN-cold-rewind-rebuild.md`**

记录:① `Supersedes #0014` 的 node_id 格式段(`it{n}`→`t{k}:it{n}`);② 取舍「读时重建逻辑 history(reconstruct)而非写时改存储布局」;③ 顺带修正 resume 对压缩 thread 的废弃项重放。更新 `docs/decisions/README.md` 索引。

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "docs(turn-rewind): 冷场景重建落档 —— 契约/agent-loop/conversation/ADR

node_id 升级 t{k}:、新增冷重建 Requirement、reconstruct 消费 replaced_range/cut_index、
ADR 记录读时重建取舍 + Supersedes #0014 node_id 段。"
```

---

## Self-Review 检查(写计划后已核对)

- **Spec 覆盖**:§4 reconstruct→Task 3;§4.3 marker cut_index→Task 6 + Task 1(签名);§5 derive→Task 4 + 奇偶校验 Task 5;§5.1 count_turns/turn_index→Task 2;§6 接线→Task 7;§6.3 热路径 re-derive→Task 8;§7 _last_resolved→Task 9;§8 R3 事件→Task 7;§9 测试矩阵→Task 3/4/5/6/7/8/9;§10 文档→Task 10。无遗漏。
- **类型一致**:`reconstruct_logical_history(list)->list`、`derive_rewind_log(list)->list[RewindCheckpoint]`、`count_turns(list)->int`、`RewindCheckpoint.turn_index:int`、`system_injection(..., extra=None)` 全程一致。
- **无占位**:每步含真实代码/命令/期望输出。helper 装配(Task 5 Step 2)指向既有 `test_turn_rewind.py` fixture 复用,非凭空。
