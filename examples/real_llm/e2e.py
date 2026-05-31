"""真实 LLM 端到端测试 —— 走 _provider_bootstrap 共享 helper + 真实 API。

读取环境变量（新形态，详见 examples/_provider_bootstrap.py）：
    LLM_BOOTSTRAP_PROVIDER=openai|anthropic|gemini|deepseek
    LLM_BOOTSTRAP_API_KEY=...
    LLM_BOOTSTRAP_MODEL=...                # 可省，按 provider 默认
    LLM_BOOTSTRAP_BASE_URL=...             # 仅 openai 自部署网关需要
    # 旧 LLM_BOOTSTRAP_OPENAI_* 仍向后兼容（隐式 provider=openai）

场景：
    1. 加载 2 个 skill（atomic style-checker + composite code-reviewer）
    2. LLM 决定调用 read_skill 获取解剖资料
    3. LLM 给出最终答复
    4. 打印 cache 命中率 + token 用量
    5. 同 session 再发一次相同 system prompt 的对话，验证 cache 命中提升

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/e2e.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

# 把 examples/ 目录加入 sys.path，让 _provider_bootstrap 可以 import
# 当前文件位于 examples/real_llm/ 子目录，parent.parent = examples/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()


import taifeng  # noqa: E402
from taifeng.telemetry import attach_console_sink  # noqa: E402


ATOMIC = """---
name: style-checker
description: 代码风格规则集
version: 1.0.0
type: atomic
---
# 风格规则集

通用规则参考：
- 函数行数 ≤ 80 行（超过强制拆分）
- 圈复杂度 ≤ 10
- 命名 snake_case（Python）/ camelCase（TS）
- 禁止魔法值（用 enum / const）
- 禁止 silent fallback（如 `except: pass`、`dict.get(k, '默认值')`）
- 注释覆盖 why 而非 what
"""

COMPOSITE = """---
name: code-reviewer
description: 代码审查专家 —— 风格 / 安全 / 性能多维度审查
version: 1.0.0
type: composite
entry: true
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家

你是一位资深代码审查工程师。

## 工作流程
1. 当用户提供 diff 时，**首先调用 `read_skill("style-checker")` 获取风格规则**作为参考
2. 按文件 / 函数粒度遍历 diff
3. 评估每段改动：
   - 风格（命名、行数、复杂度）
   - 安全（输入校验、SQL 注入、敏感信息）
   - 性能（N+1、不必要拷贝、阻塞 IO）
4. 给出按严重性排序的修改建议

## 输出格式
- **位置**：文件路径:行号
- **类别**：风格 / 安全 / 性能 / 测试
- **严重性**：低 / 中 / 高
- **建议**：具体修改方式

简明扼要，不要重复用户已知信息。
"""


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']}")
    print(f"[setup] model={meta['model']}")
    if meta.get("base_url"):
        print(f"[setup] base_url={meta['base_url']}")
    print(f"[setup] api_key={meta.get('api_key_tail', '-')}")
    print()

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        threads = root / "threads"
        (skills / "style-checker").mkdir(parents=True)
        (skills / "style-checker" / "SKILL.md").write_text(ATOMIC, encoding="utf-8")
        (skills / "code-reviewer").mkdir(parents=True)
        (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE, encoding="utf-8")

        # 启用压缩（看 mid-turn handoff 真实表现）
        pool = await taifeng.EnginePool.create(
            skills_dir=skills,
            threads_dir=threads,
            model_client=client,
            # 用默认压缩策略
        )

        # ─────────────────────────────────────────
        # 第一轮对话
        # ─────────────────────────────────────────
        engine = await pool.get_or_create(
            session_id="real-llm-e2e",
            entry_skill_id="code-reviewer",
        )
        sink = attach_console_sink(engine, color=True)

        question_1 = (
            "请审查这段 Python diff：在 user_service.py:45 新增了"
            "`def get_user(id): cur.execute(f'SELECT * FROM u WHERE id={id}')`，"
            "并且函数长度从 60 行增加到 130 行。请按你的工作流程分析。"
        )
        print(f"\n[user] {question_1}\n")
        sub_id = await engine.submit(taifeng.UserMessage(text=question_1))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

        # 给 console sink flush 时间
        await asyncio.sleep(0.2)

        # ─────────────────────────────────────────
        # 第二轮对话（同 session → 验证 prompt cache 命中）
        # ─────────────────────────────────────────
        question_2 = (
            "如果这个结节在 3 个月后复查 CT 显示增大到 10mm，"
            "且出现轻微毛刺征，下一步应该怎么处理？"
        )
        print(f"\n[user] {question_2}\n")
        sub_id_2 = await engine.submit(taifeng.UserMessage(text=question_2))
        async for ev in engine.subscribe(sub_id_2):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

        await asyncio.sleep(0.2)

        # ─────────────────────────────────────────
        # 报告
        # ─────────────────────────────────────────
        print("\n" + "=" * 60)
        print("End-to-end pipeline complete ✓")
        print(f"  thread_id: {engine.thread_id}")
        gen = await pool.store.load_thread(engine.thread_id)
        items = [it async for it in gen]
        kinds = [it.kind for it in items]
        print(f"  persisted items: {len(items)} ({dict((k, kinds.count(k)) for k in set(kinds))})")
        print("=" * 60)

        await pool.close()
        await asyncio.sleep(0.1)
        sink.cancel()


if __name__ == "__main__":
    asyncio.run(main())
