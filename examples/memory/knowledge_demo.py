"""知识库接入 demo —— 三件套:最简只读源 + 多源组合 + 检索语境定制(mock,无需 key)。

memory/demo.py 演示 4 钩子全生命周期;本 demo 演示**接入工效**——业务把自有
知识库(向量 DB / 本地文件 / 全文索引)接进来只需:

1. **最简只读源**:继承 ``NullMemoryStore`` 仅覆写 ``prefetch``(三行);
2. **多源组合**:``CompositeMemoryStore([知识库, 会话记忆])`` 一行合成;
3. **检索语境定制**:``memory_query_builder`` 用近 N 轮对话构造检索词
   (默认只用最后一条用户消息——多轮指代场景必须看得到上文)。

运行:
    PYTHONPATH=src uv run python examples/memory/knowledge_demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import taifeng
from taifeng.context.memory import CompositeMemoryStore, NullMemoryStore
from taifeng.llm.providers import SimClient, SimTurn

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.models import ResponseItem

# ── 1. 最简只读知识库:继承 NullMemoryStore,只写 prefetch ──

_KB_DOCS = {
    "上下文压缩": "【知识库】压缩策略分 handoff(LLM 摘要)/ sliding(滑窗)/ surgical(就地剪枝)三档。",
    "prompt cache": "【知识库】prompt cache 按前缀命中;压缩动作必须声明 cache_invalidated。",
    "挂起": "【知识库】HITL 挂起经 SuspensionRecord 落盘,Resume 全量核销后续跑。",
}


class KeywordKnowledgeBase(NullMemoryStore):
    """演示用知识库:关键词命中(真实场景换向量检索/FTS,prefetch 签名不变)。

    只覆写 prefetch —— writeback / on_pre_evict / on_session_end 继承 no-op,
    只读源天然不参与写路径。
    """

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        hits = [doc for kw, doc in _KB_DOCS.items() if kw in query]
        return "\n".join(hits)


# ── 2. 会话长期记忆:既有 4 钩子实现(此处极简化,重点在组合) ──


class SessionMemory(NullMemoryStore):
    """极简会话记忆:writeback 沉淀本 turn 新增文本,prefetch 附带最近一条。

    注意:writeback 收到的是「本 turn 运行期间新增」的 items(assistant 输出
    等);用户消息在 turn 构造前已入 history,不在新增集合内——要沉淀用户
    原话可在 prefetch/writeback 里按 thread_id 自行读历史。
    """

    def __init__(self) -> None:
        self._notes: list[str] = []

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        return f"【会话记忆】上一轮要点:{self._notes[-1]}" if self._notes else ""

    async def writeback(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        for it in items:
            if it.kind in ("user_message", "assistant_message"):
                text = str(it.payload.get("text", "")).strip()
                if text:
                    self._notes.append(text)


# ── 3. 检索语境定制:近 3 条消息拼接(默认只看最后一条用户消息) ──


def recent_context_query(history: list[ResponseItem]) -> str:
    """用近 3 条消息文本构造检索词——多轮指代(「它怎么配置?」)也能命中。"""
    texts = [str(it.payload.get("text", "")) for it in history[-3:]
             if it.kind in ("user_message", "assistant_message")]
    return " ".join(t for t in texts if t)


_SKILL = """---
name: helper
description: 技术助手
version: 1.0.0
type: composite
entry: true
child_skills: [notes]
tool_names: []
max_call_depth: 1
---
# 技术助手
简洁回答;上下文带 <retrieved_memory> 时优先利用。
"""

_CHILD = """---
name: notes
description: 占位
version: 1.0.0
type: atomic
---
# 占位
"""


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, body in (("helper", _SKILL), ("notes", _CHILD)):
            (root / "skills" / name).mkdir(parents=True)
            (root / "skills" / name / "SKILL.md").write_text(body, encoding="utf-8")

        kb = KeywordKnowledgeBase()
        session = SessionMemory()
        client = SimClient(turns=[
            SimTurn(text="好的,已了解你在做压缩相关的工作。"),
            SimTurn(text="(结合知识库)三档策略中 surgical 最便宜。"),
        ])
        pool = await taifeng.EnginePool.create(
            skills_dir=root / "skills", threads_dir=root / "threads",
            model_client=client, compressors=[],
            memory_store=CompositeMemoryStore([kb, session]),  # ← 双源一行合成
            memory_query_builder=recent_context_query,         # ← 检索语境定制
        )
        engine = await pool.get_or_create(
            session_id="kb-demo", entry_skill_id="helper")

        async def turn(text: str) -> None:
            sub_id = await engine.submit(taifeng.UserMessage(text=text))
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break

        # turn 1:提到「上下文压缩」→ 沉淀进会话记忆
        await turn("我在研究上下文压缩,先记住这个方向。")
        # turn 2:只说「三档是哪三档?」——默认 query(仅本句)无法命中知识库;
        # recent_context_query 把上一轮的「上下文压缩」带进检索词 → 命中
        captured: dict = {}
        orig = kb.prefetch

        async def spy(query: str, *, thread_id: str) -> str:
            captured["query"] = query
            return await orig(query, thread_id=thread_id)

        kb.prefetch = spy  # type: ignore[method-assign]
        await turn("三档是哪三档?")

        print(f"[1] builder 构造的检索词 = {captured['query']!r}")
        assert "上下文压缩" in captured["query"], "近 3 轮语境未带入检索词"
        kb_hit = await orig(captured["query"], thread_id="t")
        sess_hit = await session.prefetch(captured["query"], thread_id="t")
        print(f"[2] 知识库命中 = {kb_hit!r}")
        print(f"[3] 会话记忆命中 = {sess_hit!r}")
        assert kb_hit and sess_hit
        await pool.close()
        print("\n✅ demo 完成:三行只读源 + 一行双源组合 + 近 3 轮语境检索,全部生效")


if __name__ == "__main__":
    asyncio.run(main())
