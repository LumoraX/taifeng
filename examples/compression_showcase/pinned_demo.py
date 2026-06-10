"""postcompact 状态保活 demo —— 规划清单穿越压缩存活(mock,无需 key)。

参照 hermes 压缩后重注入 todo_snapshot 的范式:agent-owned 状态(规划/任务清单)
在历史被压缩吸收后,以 ``PinnedStateSource`` 渲染结果钉回 history 尾,
保证 LLM 在压缩后仍能看到「我正在做什么」。本 demo 展示:

1. 业务实现 ``PinnedStateSource``(name / max_chars / format_for_injection);
2. ``EnginePool.create(pinned_state_sources=[...])`` 构造期注入;
3. 多轮对话 + 手动压缩(CompactNow)后,pinned 项以
   ``system_injection(source="pinned:<name>")`` 出现在历史尾部;
4. ``pinned_state_reinjected`` 事件(sources/total_chars/dropped/phase);
5. 运行时 ``engine.register_pinned_state`` / ``unregister_pinned_state`` 增删。

契约:docs/architecture/capabilities/postcompact-state-reinjection.md。

运行:
    PYTHONPATH=src uv run python examples/compression_showcase/pinned_demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import CompactNow

_SKILL = """---
name: planner
description: 规划助手
version: 1.0.0
type: composite
entry: true
tool_names: [file_read]
---
# 规划助手
按当前规划清单推进任务。
"""


class TodoListSource:
    """业务侧 pinned source 范例:维护一份任务清单,压缩后自动钉回。

    name 是事件/审计标识;max_chars 超出由 truncate_middle 截断。
    format_for_injection 返回 None 表示本次不注入(如清单为空)。
    """

    name = "todo"
    max_chars = 800

    def __init__(self) -> None:
        self.items: list[tuple[str, bool]] = []

    def add(self, text: str) -> None:
        self.items.append((text, False))

    def complete(self, text: str) -> None:
        self.items = [(t, True if t == text else d) for t, d in self.items]

    def format_for_injection(self) -> str | None:
        if not self.items:
            return None
        lines = [f"[{'x' if done else ' '}] {t}" for t, done in self.items]
        return "## 当前任务清单(压缩保活)\n" + "\n".join(lines)


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "skills" / "planner").mkdir(parents=True)
        (root / "skills" / "planner" / "SKILL.md").write_text(
            _SKILL, encoding="utf-8")

        todo = TodoListSource()
        todo.add("梳理需求边界")
        todo.add("实现核心模块")
        todo.add("补齐测试")
        todo.complete("梳理需求边界")

        summary_client = MockClient(turns=[
            MockTurn(text="## 会话摘要\n前几轮讨论了需求边界与模块拆分。",
                     usage=TokenUsage(input_tokens=200, output_tokens=30)),
        ])
        pool = await taifeng.EnginePool.create(
            skills_dir=root / "skills", threads_dir=root / "threads",
            model_client=MockClient(
                turns=[MockTurn(text=f"好的,推进第 {i + 1} 步。") for i in range(6)]
            ),
            compressors=[HandoffCompactionStrategy(model_client=summary_client)],
            pinned_state_sources=[todo],  # ← 构造期注入
        )
        engine = await pool.get_or_create(
            session_id="demo", entry_skill_id="planner")

        # 多轮对话制造可压缩历史
        for q in ("先做什么?", "然后呢?", "继续推进。"):
            sub_id = await engine.submit(taifeng.UserMessage(text=q))
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break
        print(f"[1] 压缩前历史条数 = {len(engine.history_snapshot())}")

        # 手动强制压缩 → 观察 pinned 重注入
        sub_id = await engine.submit(CompactNow(force=True))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind == "pinned_state_reinjected":
                print(f"[2] pinned_state_reinjected: sources={ev.msg.data['sources']}"
                      f" phase={ev.msg.data['phase']} dropped={ev.msg.data['dropped']}")
            if ev.msg.kind in ("compaction_completed", "turn_failed"):
                break

        history = engine.history_snapshot()
        print(f"[3] 压缩后历史条数 = {len(history)}")
        pinned = [it for it in history if it.kind == "system_injection"
                  and str(it.payload.get("source", "")).startswith("pinned:")]
        assert pinned, "pinned 项未存活!"
        print("[4] 钉回尾部的 pinned 项内容:")
        for line in pinned[-1].payload["text"].splitlines():
            print(f"      {line}")

        # 运行时注销 → 后续压缩不再注入
        engine.unregister_pinned_state("todo")
        print("[5] 已运行时注销 source 'todo'(下次压缩不再注入)")

        await pool.close()
        print("\n✅ demo 完成:任务清单穿越压缩存活(hermes todo_snapshot 范式,协议化)")


if __name__ == "__main__":
    asyncio.run(main())
