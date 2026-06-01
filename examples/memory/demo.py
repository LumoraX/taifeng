"""MemoryStore 业务侧示范 —— 进程内长期记忆 backend（K3）。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/memory/demo.py

本示范**不动 `src/`**：只在业务侧实现一个满足 `taifeng.MemoryStore` 协议的类，
演示内核如何在正确时机调用 4 个 swap 钩子（VM/demand-paging 语义）：

    | 钩子            | 语义              | 触发点                         |
    | --------------- | ----------------- | ------------------------------ |
    | prefetch        | page-in（按需取回）| 每个 turn 计算前一次           |
    | writeback       | dirty-page 写回   | 每个 turn 结束后（fire-and-forget）|
    | on_pre_evict    | swap-out 抢救     | 压缩换页丢弃前                 |
    | on_session_end  | teardown 最终 flush| engine shutdown（pool.close）  |

检索后端故意用「进程内 dict + 字符 bigram 重叠」——零外部依赖，把重点放在
**协议契约**上。真实场景把这层换成向量库 / SQLite FTS / PG 即可，钩子签名不变。

预期能观察到：
    1. turn 1 的事实被 writeback 进 store；
    2. turn 2 的 prefetch 用 query 命中 turn 1 的事实并取回（page-in），
       内核把它作为 ``<retrieved_memory>`` 注入 prompt 尾部（见 loop/prompt.py）；
    3. 历史增长触发压缩时 on_pre_evict 抢救被换出的 items；
    4. pool.close 时 on_session_end 做最终 flush。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import taifeng
from taifeng.context import ContextBudget
from taifeng.context.strategies.sliding import SlidingWindowStrategy
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    # 仅用于类型注解（from __future__ import annotations 下注解是字符串，运行时不需导入）
    from collections.abc import Sequence

    from taifeng.conversation.models import ResponseItem


# === 业务侧 MemoryStore 实现 =================================================


def _bigrams(text: str) -> set[str]:
    """把文本切成相邻 2 字符片段（CJK 友好的极简「分词」）。

    返回 bigram 集合，供 prefetch 做朴素相关性打分。仅用于演示，真实检索
    应换成向量相似度 / 全文索引。
    """
    cleaned = "".join(ch for ch in text if not ch.isspace())
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} if len(cleaned) >= 2 else set()


class InMemoryMemoryStore:
    """满足 ``taifeng.MemoryStore`` 协议的进程内长期记忆 backend（演示用）。

    存储模型：``thread_id -> [(text, bigrams)]``。每个钩子被调用时打印一行
    带 ``[mem]`` 前缀的日志，让你直观看到内核的 swap 调度时机与内容。

    注意：所有钩子内**不得**抛异常打断主 turn —— 内核虽会 best-effort 吞掉，
    但业务实现自身也应保证快、稳、非阻塞（重计算放后台）。
    """

    def __init__(self) -> None:
        # thread_id -> 该 thread 沉淀的记忆条目（文本 + 预切的 bigram 集合）
        self._facts: dict[str, list[tuple[str, set[str]]]] = {}
        # 钩子调用流水，收尾打印用于验证 4 个钩子都被内核调到
        self.calls: list[str] = []

    # --- page-in：按 query 取回相关长期记忆 ---
    async def prefetch(self, query: str, *, thread_id: str) -> str:
        """按 query 与已存事实的 bigram 重叠度取回 top-1 记忆（命中才注入）。"""
        self.calls.append("prefetch")
        q_grams = _bigrams(query)
        best_text, best_score = "", 0
        # 检索范围：本 thread 沉淀 + on_pre_evict 抢救桶（被换出工作集仍可召回）
        candidates = self._facts.get(thread_id, []) + self._facts.get("__evicted__", [])
        for text, grams in candidates:
            score = len(q_grams & grams)  # 共享 bigram 数 = 朴素相关性
            if score > best_score:
                best_text, best_score = text, score
        if best_score == 0:
            print(f"  [mem] prefetch query={query!r} → 无命中")
            return ""
        print(f"  [mem] prefetch query={query!r} → 命中(score={best_score}): {best_text!r}")
        # 注入文本：内核会包成 <retrieved_memory> 贴在 prompt 尾部（不破 cache 前缀）
        return f"已知用户历史信息：{best_text}"

    # --- dirty-page 写回：turn 结束后把新增内容异步沉淀 ---
    async def writeback(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        """把本 turn 新增的 user / assistant 文本写入长期存储。"""
        self.calls.append("writeback")
        bucket = self._facts.setdefault(thread_id, [])
        for item in items:
            if item.kind in ("user_message", "assistant_message"):
                text = item.payload.get("text", "").strip()
                if text:
                    bucket.append((text, _bigrams(text)))
        print(f"  [mem] writeback thread={thread_id} → 当前沉淀 {len(bucket)} 条")

    # --- swap-out 抢救：压缩丢弃这些 items 前抢救要点 ---
    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        """把将被换出的 items 沉淀进 store，并返回一句「必须留在上下文」的摘要。

        返回文本会被折进压缩摘要参与 R2（cache-aware）；空串表示无补充。
        """
        self.calls.append("on_pre_evict")
        texts = [
            it.payload.get("text", "")
            for it in items
            if it.kind in ("user_message", "assistant_message")
            and it.payload.get("text")
        ]
        # 抢救：把即将被换出工作集的文本沉淀进长期存储（与 writeback 同库），
        # 这样即便它离开了 history，后续 turn 仍能通过 prefetch 召回。
        # 用 thread_id 取不到（钩子签名不带），演示中存入一个公共抢救桶。
        bucket = self._facts.setdefault("__evicted__", [])
        for text in texts:
            bucket.append((text, _bigrams(text)))
        n = len(items)
        print(f"  [mem] on_pre_evict 抢救 {n} 条被换出 items（{len(texts)} 条含文本）")
        if not texts:
            return ""
        digest = "；".join(texts)[:80]
        return f"（早期对话要点已存入长期记忆：{digest}）"

    # --- teardown：会话结束最终 flush ---
    async def on_session_end(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        """会话结束的最终 flush（演示中只打印，真实场景在此落盘 / 刷向量库）。"""
        self.calls.append("on_session_end")
        total = sum(len(bucket) for bucket in self._facts.values())
        print(f"  [mem] on_session_end thread={thread_id} → 全库沉淀 {total} 条，flush 完成")


# === skill 定义（极简 entry，不调工具，1 user 消息 = 1 MockTurn）============

# composite entry 必须声明至少一个 child_skill —— 这里放一个占位 atomic 子 skill，
# 演示中助手直接作答、并不真正派发它，仅为满足启动期校验。
CHILD_SKILL = """---
name: notes
description: 备忘参考
version: 1.0.0
type: atomic
---
# 备忘
（占位子 skill，仅供 entry 声明，本示范不实际调用。）
"""

ENTRY_SKILL = """---
name: assistant
description: 通用助手
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [notes]
tool_names: []
max_call_depth: 1
---
# 通用助手
你是一位通用助手，直接简洁地回答用户。如果上下文带有 <retrieved_memory>，请优先利用其中的历史信息。
"""


async def _run_one_turn(engine: taifeng.AgentEngine, text: str) -> None:
    """提交一条用户消息并等待该 turn 结束（成功或失败均返回）。"""
    sub_id = await engine.submit(taifeng.UserMessage(text=text))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        (skills / "assistant").mkdir(parents=True)
        (skills / "assistant" / "SKILL.md").write_text(ENTRY_SKILL, encoding="utf-8")
        (skills / "notes").mkdir(parents=True)
        (skills / "notes" / "SKILL.md").write_text(CHILD_SKILL, encoding="utf-8")

        memory = InMemoryMemoryStore()

        # Mock LLM 脚本：3 条用户消息对应 3 个回合。故意把每条回复写长，
        # 配合下方极小 context_window 触发压缩 → on_pre_evict。
        client = MockClient(turns=[
            MockTurn(
                text="好的，我记住了：你喜欢用 SQLite 做本地索引，项目代号是 Taifeng。" * 3,
                usage=TokenUsage(input_tokens=60, output_tokens=40, total_tokens=100),
            ),
            MockTurn(
                text="今天天气晴朗，适合写代码喝咖啡，我们继续聊技术吧。" * 3,
                usage=TokenUsage(input_tokens=80, output_tokens=40, total_tokens=120),
            ),
            MockTurn(
                text="根据你之前告诉我的信息，你喜欢用 SQLite 做本地索引。",
                usage=TokenUsage(input_tokens=90, output_tokens=30, total_tokens=120),
            ),
        ])

        # context_window 调到极小 → 历史累积几轮后触发 SlidingWindowStrategy 换页。
        pool = await taifeng.EnginePool.create(
            skills_dir=skills,
            threads_dir=root / "threads",
            model_client=client,
            compressors=[SlidingWindowStrategy(keep_tail=2)],
            # soft 门控 _maybe_compress 是否进策略；hard 才是 SlidingWindow 真正换页线。
            # 两者都调低，让第 3 轮 pre_turn（history 累计 4 条）越过 hard → 换出最早 2 条。
            budget=ContextBudget(
                context_window=100, soft_limit_ratio=0.3, hard_limit_ratio=0.4,
            ),
            memory_store=memory,  # ← 业务侧 backend 注入点（唯一一行接线）
        )
        engine = await pool.get_or_create(
            session_id="mem-demo",
            entry_skill_id="assistant",
            cwd=str(Path.cwd()),
        )

        print("=== turn 1：用户陈述一个事实（writeback 沉淀）===")
        await _run_one_turn(engine, "请记住：我喜欢用 SQLite 做本地索引，项目代号 Taifeng。")

        print("\n=== turn 2：闲聊（历史增长，可能触发压缩 → on_pre_evict）===")
        await _run_one_turn(engine, "随便聊聊，今天天气怎么样？")

        print("\n=== turn 3：追问历史（prefetch 应命中 turn 1 的事实并 page-in）===")
        await _run_one_turn(engine, "我之前说过喜欢用什么做本地索引？")

        print("\n=== 关闭 pool（触发 on_session_end）===")
        await pool.close()
        await asyncio.sleep(0.1)

        print("\n=== 钩子调用流水（验证 4 个 swap 钩子都被内核调到）===")
        print("  " + " → ".join(memory.calls))
        fired = set(memory.calls)
        for hook in ("prefetch", "writeback", "on_pre_evict", "on_session_end"):
            mark = "✓" if hook in fired else "✗（本次未触发）"
            print(f"    {hook:<16} {mark}")


if __name__ == "__main__":
    asyncio.run(main())
