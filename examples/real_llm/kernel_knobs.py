"""真实 LLM 内核旋钮验证 —— K1–K4 + K6 经公开 API 接出，用真实 token 计量驱动。

MockClient 版（examples/kernel_knobs/demo.py）证明"机制接得通"；本文用**真实 LLM**
证明它们在真实 token 流下也成立——尤其 K2 OOM-killer：会话累计是 provider 回报的
**真实 usage**，不是脚本捏的数。

读取环境变量（详见 examples/_provider_bootstrap.py）：
    LLM_BOOTSTRAP_PROVIDER / LLM_BOOTSTRAP_API_KEY / LLM_BOOTSTRAP_MODEL / LLM_BOOTSTRAP_BASE_URL

两条路径：
    ① 正常路径：K1 spawn 配额 + K3 memory 换页钩子 + K4 流控 全接出，跑真 turn，
       introspect() 打快照（spawn 配额 / 真实 session_tokens / cache 健康度），
       断言 K3 三个钩子被回调。
    ② K2 OOM 路径：max_session_tokens 设小，真实 usage 触顶后，下一轮被内核拒绝。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/kernel_knobs.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

# 把 examples/ 目录加入 sys.path，让 _provider_bootstrap 可以 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402

ATOMIC = """---
name: style-checker
description: 代码风格规则集
version: 1.0.0
type: atomic
---
# 风格规则集
- 函数 ≤ 80 行；圈复杂度 ≤ 10；命名 snake_case
- 禁止魔法值 / silent fallback
"""

COMPOSITE = """---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite
entry: true
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家
你是资深代码审查工程师。需要风格规范时先调用 `read_skill("style-checker")`，
再按风格 / 安全 / 性能给出按严重性排序的建议。
"""


class SpyMemoryStore:
    """K3：最小 MemoryStore —— 记录每个换页钩子是否被真实 turn 回调。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        self.calls.append("prefetch")
        return "「长期记忆」上次审查发现该模块命名不规范，需重点关注。"

    async def writeback(self, *, thread_id: str, items: object) -> None:
        self.calls.append("writeback")

    async def on_pre_evict(self, items: object) -> str:
        self.calls.append("on_pre_evict")
        return ""

    async def on_session_end(self, *, thread_id: str, items: object) -> None:
        self.calls.append("on_session_end")


def _write_skills(root: Path) -> Path:
    """落两个 SKILL.md，返回 skills 根。"""
    skills = root / "skills"
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(ATOMIC, encoding="utf-8")
    (skills / "code-reviewer").mkdir(parents=True)
    (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE, encoding="utf-8")
    return skills


async def scenario_normal(client: object, root: Path) -> bool:
    """① K1/K3/K4 全接出 + 真 turn + introspect。返回 K3 钩子是否全触发。"""
    (root / "threads").mkdir(parents=True, exist_ok=True)
    mem = SpyMemoryStore()
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_skills(root), threads_dir=root / "threads",
        model_client=client, compressors=[],
        max_concurrent_spawns=8,    # K1 广度准入
        max_total_spawns=100,       # K1 兜底
        memory_store=mem,           # K3 内存层级
        submission_queue_size=64,   # K4 入站流控
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub = await engine.submit(taifeng.UserMessage(
        text="审查这段代码：\n```python\ndef getCwd(x):\n    q = \"SELECT * FROM t WHERE id=\" + str(x)\n    return run(q)\n```"
    ))
    async for ev in engine.subscribe(sub):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    snap = engine.introspect()  # K6 自省（真实 token 计量）
    print("=== ① 正常路径（真 turn + K1/K3/K4 + introspect）===")
    print(f"  spawn 配额 (K1)     : {snap['spawn']}")
    print(f"  session_tokens (K2) : {snap['session_tokens']}  ← provider 真实 usage")
    print(f"  events_dropped (K4) : {snap['events_dropped']}")
    print(f"  cache 健康度        : {snap['cache']}")
    await pool.close()  # 触发 on_session_end
    await asyncio.sleep(0.05)
    print(f"  K3 memory 钩子回调   : {mem.calls}")
    return {"prefetch", "writeback", "on_session_end"}.issubset(set(mem.calls))


async def scenario_oom(client: object, root: Path) -> bool:
    """② K2 OOM：真实 usage 触顶后拒新 turn。返回是否被拒。"""
    (root / "threads").mkdir(parents=True, exist_ok=True)
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_skills(root), threads_dir=root / "threads",
        model_client=client, compressors=[],
        max_session_tokens=500,     # K2：500 上限，真实一轮 usage 即触顶
    )
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")
    kinds: list[str] = []
    for text in ("简单介绍下你的职责。", "再审查一段代码。"):
        sub = await engine.submit(taifeng.UserMessage(text=text))
        async for ev in engine.subscribe(sub):
            kinds.append(ev.msg.kind)
            if ev.msg.kind in ("turn_completed", "turn_failed", "turn_refused",
                               "resource_limit_exceeded"):
                break
    print("\n=== ② K2 OOM 路径（max_session_tokens=500，真实 usage 触顶）===")
    print(f"  事件序列: {kinds}")
    refused = any(k in ("turn_refused", "resource_limit_exceeded") for k in kinds)
    print(f"  → 触顶后被内核{'拒绝 ✓' if refused else '放行 ✗'}")
    await pool.close()
    return refused


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")
    if meta.get("base_url"):
        print(f"[setup] base_url={meta['base_url']}")
    print()

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mem_ok = await scenario_normal(client, root / "a")
        oom_ok = await scenario_oom(client, root / "b")

    print("\n" + "=" * 60)
    print("真实 LLM 内核旋钮验证")
    print(f"  K3 memory 钩子全触发 : {'✓' if mem_ok else '✗'}")
    print(f"  K2 OOM 触顶拒新 turn : {'✓' if oom_ok else '✗'}")
    ok = mem_ok and oom_ok
    print(f"  {'✅ 内核旋钮在真实 LLM 下成立' if ok else '❌ 有断言未通过'}")
    print("=" * 60)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
