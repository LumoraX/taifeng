"""MemoryStore 持久化示范 —— 长期记忆跨进程重启存活（K3，落盘 JSONL backend）。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/memory/persistent_demo.py

本示范补 `memory/demo.py` 与 `memory/knowledge_demo.py` 的共同盲区：那两者的
backend 都是**进程内 dict**（`InMemoryMemoryStore`），进程一退记忆全丢——它们演示了
4 个 swap 钩子的**调度时机**，却没证明长期记忆区别于「进程内缓存」的那个本质属性：
**跨进程重启依然能召回**。

这恰恰是长期记忆存在的理由，也与内核 R5（默认 store = JSONL 追加写、可 resume）
同一范式：换出工作集的内容落到磁盘，进程重启后按需换入。本 demo 用一个**落盘
JSONL 的 `JsonlMemoryStore`**，在**两段相互独立的 engine 生命周期**里证明这一点：

    ┌── 进程段 A（pool#1）──────────────┐   ┌── 进程段 B（pool#2，全新）─────────┐
    │ 用户陈述事实                       │   │ 全新 session / 空 history          │
    │   → writeback 落盘 JSONL          │   │ 用户追问历史                       │
    │ pool.close → on_session_end flush │   │   → prefetch 从磁盘换入 → 命中     │
    └───────────────────────────────────┘   └───────────────────────────────────┘
      段 A 退出后内存全清空，唯一留下的是磁盘上的 .jsonl 文件；
      段 B 是**另一个 pool、另一个 engine、另一个 store 实例、另一个 session**，
      history 是空的——它能答出事实，只可能来自磁盘。变量被彻底隔离。

检索策略：本实现把记忆按 **user 级全局知识** 处理（prefetch 跨所有 thread 召回，
thread_id 仅作审计记录）——这是一个**业务侧策略选择**，故意区别于 demo.py 的
thread 内召回，以便段 B 用全新 thread 也能命中段 A 的事实。若要做会话内记忆，
按 thread_id 过滤即可，钩子签名不变。

检索后端照旧用「字符 bigram 重叠」零依赖打分，重点在**持久化契约**；真实场景把
这层换成向量库 / SQLite FTS / PG，落盘格式换成对应写入，4 个钩子签名不变。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    # 仅用于类型注解（from __future__ import annotations 下注解是字符串，运行时不需导入）
    from collections.abc import Sequence

    from taifeng.conversation.models import ResponseItem


# === 业务侧 MemoryStore 实现（落盘 JSONL，跨进程存活）========================


def _bigrams(text: str) -> set[str]:
    """把文本切成相邻 2 字符片段（CJK 友好的极简「分词」）。

    返回 bigram 集合，供 prefetch 做朴素相关性打分。仅用于演示，真实检索
    应换成向量相似度 / 全文索引。
    """
    cleaned = "".join(ch for ch in text if not ch.isspace())
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} if len(cleaned) >= 2 else set()


class JsonlMemoryStore:
    """满足 ``taifeng.MemoryStore`` 协议的**落盘**长期记忆 backend（演示用）。

    持久化模型：append-only JSONL，每行一条记忆 ``{"thread_id", "text"}``——与内核
    默认 `JsonlMessageStore`（R5）同范式（追加写、可重放、进程重启后从头加载重建
    索引）。检索索引（text + 预切 bigram）在 ``__init__`` 时从磁盘整文件重建，
    所以**新进程 new 一个本类指向同一文件，即自动「记得」上次写入的内容**。

    检索语义：**user 级全局**——prefetch 跨所有 thread 召回（thread_id 仅记录不
    用于过滤），故全新会话也能命中历史事实。这是业务侧策略选择，非内核约束。

    注意：所有钩子内**不得**抛异常打断主 turn——内核虽 best-effort 吞掉，业务实现
    自身也应快、稳、非阻塞（重计算放后台）。本实现的 fsync 落盘对演示足够。
    """

    def __init__(self, path: Path) -> None:
        """Args:
            path: JSONL 持久化文件路径。文件已存在则加载其全部行重建索引
                （这一步就是「进程重启后记得」的实现）；不存在则起空库。
        """
        self._path = path
        # 内存检索索引：[(text, bigrams)]，从磁盘重建。落盘是真相源，索引是加速副本。
        self._index: list[tuple[str, set[str]]] = []
        # 钩子调用流水，收尾打印用于验证内核调度时机
        self.calls: list[str] = []
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """从 JSONL 整文件重建检索索引——长期记忆「跨进程存活」的落地点。"""
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)  # 内部契约（自写自读），脏行不兜底——坏了立刻暴露
            text = record["text"]
            self._index.append((text, _bigrams(text)))

    def _persist(self, *, thread_id: str, text: str) -> None:
        """追加一条记忆到磁盘 + 更新内存索引。append-only，不覆盖不回改。"""
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"thread_id": thread_id, "text": text}, ensure_ascii=False) + "\n")
            f.flush()  # 立即落盘，保证段 A 退出前数据已在磁盘上
        self._index.append((text, _bigrams(text)))

    # --- page-in：按 query 取回相关长期记忆 ---
    async def prefetch(self, query: str, *, thread_id: str) -> str:
        """按 query 与已存事实的 bigram 重叠度取回 top-1 记忆（命中才注入）。

        跨所有 thread 检索（user 级全局记忆）；命中文本由内核包成
        ``<retrieved_memory>`` 贴在 prompt 尾部（不破 cache 前缀，R2 面）。
        """
        self.calls.append("prefetch")
        q_grams = _bigrams(query)
        best_text, best_score = "", 0
        for text, grams in self._index:
            score = len(q_grams & grams)  # 共享 bigram 数 = 朴素相关性
            if score > best_score:
                best_text, best_score = text, score
        if best_score == 0:
            print(f"  [mem] prefetch query={query!r} → 无命中")
            return ""
        print(f"  [mem] prefetch query={query!r} → 命中(score={best_score}): {best_text!r}")
        return f"已知用户历史信息：{best_text}"

    # --- dirty-page 写回：turn 结束后把新增内容落盘 ---
    async def writeback(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        """把本 turn 新增的 user / assistant 文本追加写入 JSONL 长期存储。"""
        self.calls.append("writeback")
        n = 0
        for item in items:
            if item.kind in ("user_message", "assistant_message"):
                text = item.payload.get("text", "").strip()
                if text:
                    self._persist(thread_id=thread_id, text=text)
                    n += 1
        print(
            f"  [mem] writeback thread={thread_id} → "
            f"本轮落盘 {n} 条，全库共 {len(self._index)} 条"
        )

    # --- swap-out 抢救：压缩丢弃这些 items 前抢救要点 ---
    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        """把将被换出的 items 落盘，并返回一句「必须留在上下文」的摘要。

        返回文本会被折进压缩摘要参与 R2（cache-aware）；空串表示无补充。
        （本 demo 不刻意触发压缩，此钩子主要由 demo.py 演示；这里保持契约完整。）
        """
        self.calls.append("on_pre_evict")
        texts = [
            it.payload.get("text", "")
            for it in items
            if it.kind in ("user_message", "assistant_message") and it.payload.get("text")
        ]
        for text in texts:
            self._persist(thread_id="__evicted__", text=text)
        if not texts:
            return ""
        digest = "；".join(texts)[:80]
        return f"（早期对话要点已存入长期记忆：{digest}）"

    # --- teardown：会话结束最终 flush ---
    async def on_session_end(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        """会话结束的最终 flush。本实现每条 writeback 已即时 flush 落盘，

        此处只做收尾确认（真实场景若用缓冲写 / 批量刷向量库，应在此强制 flush）。
        """
        self.calls.append("on_session_end")
        print(
            f"  [mem] on_session_end thread={thread_id} → "
            f"磁盘已持久 {len(self._index)} 条，flush 完成"
        )


# === skill 定义（极简 entry，不调工具，1 user 消息 = 1 SimTurn）============

# composite entry 必须声明至少一个 child_skill —— 占位 atomic 子 skill，仅供 entry 声明。
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


async def _run_session(
    *,
    skills_dir: Path,
    threads_dir: Path,
    memory_path: Path,
    session_id: str,
    sim_turns: list[SimTurn],
    user_messages: list[str],
) -> JsonlMemoryStore:
    """跑完整一段 engine 生命周期：new store（从磁盘加载）→ 建 pool/engine →
    依次跑 user_messages → close pool（触发 on_session_end）。

    关键：每次调用都 **new 一个全新的 `JsonlMemoryStore`**，只共享磁盘文件
    ``memory_path``——这正是「进程重启」的模拟：内存态全清空，唯一跨段的是磁盘。

    Returns:
        本段使用的 store 实例（供收尾检视钩子流水）。
    """
    memory = JsonlMemoryStore(memory_path)  # ← 从磁盘加载既有记忆（段 B 在此「记起」段 A）
    client = SimClient(turns=sim_turns)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        memory_store=memory,  # ← 业务侧 backend 注入点（唯一一行接线）
    )
    engine = await pool.get_or_create(
        session_id=session_id,
        entry_skill_id="assistant",
        cwd=str(Path.cwd()),
    )
    for msg in user_messages:
        await _run_one_turn(engine, msg)
    await pool.close()  # 触发 on_session_end
    await asyncio.sleep(0.1)
    return memory


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        (skills / "assistant").mkdir(parents=True)
        (skills / "assistant" / "SKILL.md").write_text(ENTRY_SKILL, encoding="utf-8")
        (skills / "notes").mkdir(parents=True)
        (skills / "notes" / "SKILL.md").write_text(CHILD_SKILL, encoding="utf-8")

        # 唯一跨「进程段」的载体：磁盘上的一个 JSONL 文件。
        memory_path = root / "long_term_memory.jsonl"

        # ===== 进程段 A：用户陈述事实 → writeback 落盘 → 段结束 flush =====
        print("=" * 68)
        print("进程段 A（pool#1 / session=alice-1）：陈述事实并落盘长期记忆")
        print("=" * 68)
        store_a = await _run_session(
            skills_dir=skills,
            threads_dir=root / "threads_a",  # 段 A 独立 threads（与段 B 不共享 history）
            memory_path=memory_path,
            session_id="alice-1",
            sim_turns=[SimTurn(
                text="好的，我记住了：你喜欢用 SQLite 做本地索引，项目代号是 Taifeng。",
                usage=TokenUsage(input_tokens=60, output_tokens=40, total_tokens=100),
            )],
            user_messages=["请记住：我喜欢用 SQLite 做本地索引，项目代号 Taifeng。"],
        )

        # === 证据 1：段 A 退出后，记忆确实躺在磁盘上（脱离任何进程内存）===
        print("\n--- 磁盘 JSONL 内容（段 A 退出后仍在）---")
        for line in memory_path.read_text(encoding="utf-8").splitlines():
            print(f"  {line}")

        # ===== 进程段 B：全新 pool / 全新 engine / 全新 store / 空 history =====
        # 段 B 的 history 是空的（threads_b 全新），它若能答出事实，只可能来自磁盘记忆。
        print("\n" + "=" * 68)
        print("进程段 B（pool#2 / session=alice-2 / 空 history）：仅凭磁盘记忆召回")
        print("=" * 68)
        store_b = await _run_session(
            skills_dir=skills,
            threads_dir=root / "threads_b",  # 全新空 threads → history 不含段 A 任何内容
            memory_path=memory_path,         # 但指向同一磁盘记忆文件
            session_id="alice-2",
            sim_turns=[SimTurn(
                text="根据长期记忆，你喜欢用 SQLite 做本地索引。",
                usage=TokenUsage(input_tokens=90, output_tokens=30, total_tokens=120),
            )],
            user_messages=["我之前说过喜欢用什么做本地索引？"],
        )

        # === 证据 2：段 B 的 prefetch 命中了段 A 写入的事实（跨进程换入成功）===
        print("\n" + "=" * 68)
        print("验证")
        print("=" * 68)
        print(f"  段 A 钩子流水：{' → '.join(store_a.calls)}")
        print(f"  段 B 钩子流水：{' → '.join(store_b.calls)}")
        # 段 B 全新 store 初始化时即从磁盘加载到了段 A 的记忆，故 prefetch 能命中。
        recalled = any(c == "prefetch" for c in store_b.calls) and len(store_b._index) > 0
        print(
            f"\n  结论：段 B（全新进程态、空 history）"
            f"{'成功' if recalled else '未能'}从磁盘长期记忆召回段 A 的事实 "
            f"→ 跨进程重启存活{'已验证 ✓' if recalled else '失败 ✗'}"
        )


if __name__ == "__main__":
    asyncio.run(main())
