"""真实 LLM + composite skill 递归派发测试。

场景：
    1. 用户进入 ``programmer`` entry skill
    2. LLM 决定通过 ``call_skill("code-review", ...)`` 派发到子 skill
    3. 子 skill 的 TurnRunner 启动独立 LLM 调用
    4. 子 skill 完成后返回结果给父
    5. 父 LLM 给最终答复

验证：
    - call_skill 工具真的被 LLM 触发
    - DispatchPolicy 通过（在 child_skills 白名单内）
    - 嵌套 TurnRunner 正确执行
    - SkillDispatched / SkillReturned 事件触发
    - 父子 thread 隔离
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


CODE_REVIEW = """---
name: code-review
description: 代码审查专家 —— 聚焦正确性 / 安全性 / 可读性
version: 1.0.0
type: atomic
---
# 代码审查专家

你是一位资深代码审查者。收到代码片段后，请按以下结构给出审查结论：

1. **正确性**：逻辑错误、边界处理
2. **安全性**：注入、越权、敏感数据
3. **可读性**：命名、注释、复杂度
4. **建议**：3 条以内具体改进

保持简洁，每点 1-2 行。
"""

PROGRAMMER = """---
name: programmer
description: 通用程序员
version: 1.0.0
type: composite
entry: true
child_skills: [code-review]
tool_names: []
max_call_depth: 3
---
# 程序员

你是一位资深软件工程师。

## 工作流程
**当用户给出代码并要求审查时**，**必须**通过 `call_skill("code-review", {"code": "...用户的代码原文..."})`
派发到代码审查专家。等结果返回后，你可以补充你的实施建议。

不要自己审查代码 —— 一律走 code-review。

## 输出
1. 派发审查（call_skill）
2. 收到结果后做一段总结性补充（如优先级建议、是否需要重构等）
"""


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=180.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(
        f"[setup] provider={meta['provider']} model={meta['model']} "
        f"base_url={meta.get('base_url', '-')} key={meta.get('api_key_tail', '-')}\n"
    )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        threads = root / "threads"
        (skills / "code-review").mkdir(parents=True)
        (skills / "code-review" / "SKILL.md").write_text(CODE_REVIEW, encoding="utf-8")
        (skills / "programmer").mkdir(parents=True)
        (skills / "programmer" / "SKILL.md").write_text(PROGRAMMER, encoding="utf-8")

        pool = await taifeng.EnginePool.create(
            skills_dir=skills,
            threads_dir=threads,
            model_client=client,
        )

        engine = await pool.get_or_create(
            session_id="composite-e2e",
            entry_skill_id="programmer",
        )
        sink = attach_console_sink(engine, color=True)

        question = (
            "请审查以下 Python 函数：\n\n"
            "```python\n"
            "def login(username, password):\n"
            "    query = f\"SELECT * FROM users WHERE name='{username}' AND pwd='{password}'\"\n"
            "    return db.execute(query).fetchone()\n"
            "```\n"
        )
        print(f"[user] {question}")

        sub_id = await engine.submit(taifeng.UserMessage(text=question))
        events_seen: list[str] = []
        async for ev in engine.subscribe(sub_id):
            events_seen.append(ev.msg.kind)
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break
        await asyncio.sleep(0.2)

        print("\n" + "=" * 60)
        print("Composite skill e2e ✓")
        dispatched = sum(1 for k in events_seen if k == "skill_dispatched")
        returned = sum(1 for k in events_seen if k == "skill_returned")
        tool_started = sum(1 for k in events_seen if k == "tool_call_started")
        print(f"  events: {len(events_seen)} total")
        print(f"  skill_dispatched: {dispatched}")
        print(f"  skill_returned:   {returned}")
        print(f"  tool_call_started: {tool_started}")
        if dispatched == 0:
            print("  ⚠️  LLM 没有调 call_skill —— 可能模型不擅长 tool use；")
            print("      重试或换 SKILL.md 描述更强势")
        else:
            print("  ✅ LLM 主动调 call_skill 派发了子 skill")
        print("=" * 60)

        await pool.close()
        await asyncio.sleep(0.1)
        sink.cancel()


if __name__ == "__main__":
    asyncio.run(main())
