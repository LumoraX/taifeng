"""IndexHook 端到端示例 —— 投递 thread 生命周期事件到本地审计日志。

业务侧典型用法：把 ``on_thread_created`` / ``on_message_appended`` / ``on_metadata_updated``
作为审计 / metrics / 异步索引（ES / Kafka）的钩子。本例最小化为「append 一行 JSON 到 audit.log」。

运行（端到端，无需 LLM API key）：

    PYTHONPATH=src uv run python examples/observability/audit_index_hook.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import taifeng
from taifeng.conversation import ResponseItem, ThreadMetadata
from taifeng.llm.providers.mock import MockClient, MockTurn
from taifeng.llm.types import TokenUsage


# ====================================================================
# 业务侧 IndexHook 实现
# ====================================================================


class AuditLogHook:
    """所有 thread 生命周期事件 → append 一行 JSON 到 audit.log。

    用 ``asyncio.Lock`` 串行化写入避免并发撕裂行（生产环境可换 aiofiles / 异步 io）。
    """

    def __init__(self, audit_path: Path) -> None:
        self._path = audit_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def _append(self, payload: dict[str, object]) -> None:
        payload["timestamp"] = datetime.now(UTC).isoformat()
        line = json.dumps(payload, ensure_ascii=False)
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    async def on_thread_created(self, meta: ThreadMetadata) -> None:
        await self._append(
            {
                "event": "thread_created",
                "thread_id": meta.thread_id,
                "entry_skill_id": meta.entry_skill_id,
                "tags": list(meta.tags),
            }
        )

    async def on_message_appended(self, thread_id: str, items: list[ResponseItem]) -> None:
        await self._append(
            {
                "event": "message_appended",
                "thread_id": thread_id,
                "item_count": len(items),
                "item_kinds": [it.kind for it in items],
            }
        )

    async def on_metadata_updated(self, thread_id: str, patch: dict[str, object]) -> None:
        await self._append(
            {
                "event": "metadata_updated",
                "thread_id": thread_id,
                "patch_keys": list(patch.keys()),
            }
        )


# ====================================================================
# 准备最小可跑的 skill 目录（无需真实 LLM）
# ====================================================================


ENTRY_SKILL = """---
name: general
description: 通用入口 skill
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [helper]
tool_names: []
max_call_depth: 2
---
# General

You are a helpful assistant.
"""

CHILD_SKILL = """---
name: helper
description: 辅助 atomic skill
version: 1.0.0
type: atomic
---
# Helper

辅助内容。
"""


def _prepare_skills(skills_dir: Path) -> None:
    (skills_dir / "general").mkdir(parents=True, exist_ok=True)
    (skills_dir / "general" / "SKILL.md").write_text(ENTRY_SKILL, encoding="utf-8")
    (skills_dir / "helper").mkdir(parents=True, exist_ok=True)
    (skills_dir / "helper" / "SKILL.md").write_text(CHILD_SKILL, encoding="utf-8")


# ====================================================================
# main
# ====================================================================


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skills_dir = root / "skills"
        storage_dir = root / "data"
        audit_path = root / "audit.log"

        _prepare_skills(skills_dir)

        # MockClient：3 个 turn 各返回固定文本（避免依赖真实 LLM）
        client = MockClient(
            turns=[
                MockTurn(text=f"answer {i}", usage=TokenUsage(input_tokens=10, output_tokens=3))
                for i in range(3)
            ]
        )

        hook = AuditLogHook(audit_path)
        pool = await taifeng.EnginePool.create(
            skills_dir=skills_dir,
            storage_dir=storage_dir,
            model_client=client,
            compressors=[],
            index_hook=hook,
        )
        try:
            engine = await pool.get_or_create(session_id="demo", entry_skill_id="general")
            # 跑 3 个 turn 触发 create + 多次 append
            for i in range(3):
                sub_id = await engine.submit(taifeng.UserMessage(text=f"question {i}"))
                async for ev in engine.subscribe(sub_id):
                    if ev.msg.kind in ("turn_completed", "turn_failed"):
                        break
        finally:
            await pool.close()

        # 校验 audit.log 内容
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line) for line in lines]
        creates = [e for e in events if e["event"] == "thread_created"]
        appends = [e for e in events if e["event"] == "message_appended"]

        print(f"✓ audit.log 共 {len(events)} 行事件")
        print(f"  - thread_created: {len(creates)}")
        print(f"  - message_appended: {len(appends)}")
        print(f"  - 首条事件: {events[0]}")
        assert len(creates) == 1, "1 个 session 仅应 create 一次"
        assert len(appends) >= 3, "3 个 turn 至少触发 3 次 append"


if __name__ == "__main__":
    asyncio.run(main())
