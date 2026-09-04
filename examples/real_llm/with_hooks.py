"""真实 LLM + Hooks + Permission gate 集成测试。

场景：
    1. 注册一个 PreToolUse hook：每次工具调用都打印 + 给所有 read_skill 加放行；
       但对 ``forbidden_skill`` 拒绝。
    2. 业务侧观察 hook 是否真的拦截了 LLM 的工具调用。
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
from taifeng.hooks import (  # noqa: E402
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    PreToolUseHook,
)
from taifeng.telemetry import attach_console_sink  # noqa: E402


SKILL = """---
name: style-checker
description: 代码风格规则
version: 1.0.0
type: atomic
---
代码风格规则内容。
"""

ENTRY = """---
name: expert
description: 专家
version: 1.0.0
type: composite
entry: true
child_skills: [style-checker]
max_call_depth: 3
---
# 专家
请尽快调用 `read_skill("style-checker")` 获取风格规则，然后回答用户问题。
"""


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(
        f"[setup] provider={meta['provider']} model={meta['model']} "
        f"key={meta.get('api_key_tail', '-')}\n"
    )

    # === Hook 注册 ===
    hook_log: list[str] = []

    async def pre_tool_use_hook(hook: PreToolUseHook, ctx: HookContext) -> HookDecision:
        hook_log.append(f"PreToolUse[{hook.tool_name}] args={hook.arguments}")
        # 拒绝示例：如果 LLM 尝试读 forbidden 文件
        if hook.tool_name == "read_skill" and hook.arguments.get("skill_id") == "forbidden":
            return HookDecision.deny("hook_test_block")
        return HookDecision.ok()

    hook_registry = HookRegistry()
    hook_registry.register("pre_tool_use", pre_tool_use_hook)
    hook_runner = HookRunner(hook_registry)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        threads = root / "threads"
        (skills / "style-checker").mkdir(parents=True)
        (skills / "style-checker" / "SKILL.md").write_text(SKILL, encoding="utf-8")
        (skills / "expert").mkdir(parents=True)
        (skills / "expert" / "SKILL.md").write_text(ENTRY, encoding="utf-8")

        pool = await taifeng.EnginePool.create(
            skills_dir=skills,
            threads_dir=threads,
            model_client=client,
            hooks=hook_runner,  # ⭐ 注入 hooks
        )

        engine = await pool.get_or_create(
            session_id="hooks-e2e",
            entry_skill_id="expert",
        )
        sink = attach_console_sink(engine, color=True)

        print("[user] 请用一句话概括 PEP 8 命名规则。\n")
        sub_id = await engine.submit(taifeng.UserMessage(text="请用一句话概括 PEP 8 命名规则。"))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

        await asyncio.sleep(0.2)
        print("\n" + "=" * 60)
        print("Hook integration ✓")
        print(f"  hook calls: {len(hook_log)}")
        for log in hook_log:
            print(f"    {log}")
        print("=" * 60)

        await pool.close()
        await asyncio.sleep(0.1)
        sink.cancel()


if __name__ == "__main__":
    asyncio.run(main())
