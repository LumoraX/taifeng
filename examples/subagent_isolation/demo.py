"""G3 subagent isolation 演示 —— 三种 mode 跑同一 composite skill。

演示 ``DispatchPolicy.subagent_approval_mode`` 三种取值在 call_skill 派发时
的差异：

    - inherit:    子 turn 复用父 PermissionPolicy；prompter 仍会被触发
    - auto_deny:  子 turn ``ask`` 自动 deny；不调 prompter
    - auto_allow: 子 turn ``ask`` 自动 allow；不调 prompter

每个 mode 跑一次 programmer → code-review 派发，订阅 EventMsg 流量打印：

    - skill_dispatched          ← call_skill 触发
    - subagent_policy_overridden ← G3 新事件（inherit 模式无此事件）
    - skill_returned            ← 子 skill 完成

运行（mock LLM，无需 API key）：

    cd taifeng
    PYTHONPATH=src uv run python examples/subagent_isolation_demo.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.permission.types import PermissionPolicy, PermissionRule
from taifeng.skill.dispatch import DispatchPolicy

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SKILLS_DIR = HERE / "skills"


def _scripted_client() -> MockClient:
    """父 turn → call_skill；子 turn → 给 review；父 turn → 总结。"""
    return MockClient(turns=[
        MockTurn(
            text="正在派发代码审查专家...",
            tool_calls=[{
                "id": "tc1",
                "name": "call_skill",
                "arguments": (
                    '{"skill_id": "code-review", '
                    '"args": {"code": "def add(a,b): return a+b"}}'
                ),
            }],
            usage=TokenUsage(input_tokens=120, output_tokens=20),
        ),
        MockTurn(
            text=(
                "审查结论：\n"
                "1. 正确性：基础加法逻辑正确\n"
                "2. 安全性：无显式风险\n"
                "3. 可读性：缺 type hints 与 docstring\n"
                "4. 建议：补充类型注解"
            ),
            usage=TokenUsage(input_tokens=80, output_tokens=40),
        ),
        MockTurn(
            text="综合建议：保留逻辑，补充类型注解与 docstring。",
            usage=TokenUsage(
                input_tokens=180, output_tokens=30,
                cache_read_input_tokens=100,
            ),
        ),
    ])


async def run_one_mode(
    mode: str,
    *,
    permission_policy: PermissionPolicy | None = None,
) -> None:
    """跑一次端到端 + 收集关心的 EventMsg。"""
    print(f"\n{'=' * 60}", flush=True)
    print(f"=== subagent_approval_mode = {mode!r} ===", flush=True)
    print(f"{'=' * 60}", flush=True)

    client = _scripted_client()
    pool = await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        # 演示落盘统一收口到 .taifeng/examples/ 伞形目录，避免污染仓库根
        threads_dir=HERE / ".runs" / f"mode-{mode}",
        model_client=client,
        compressors=[],
        dispatch_policy=DispatchPolicy(subagent_approval_mode=mode),  # type: ignore[arg-type]
        permission_policy=permission_policy,
    )
    engine = await pool.get_or_create(
        session_id=f"demo-{mode}", entry_skill_id="programmer",
    )

    interesting = {
        "skill_dispatched",
        "skill_returned",
        "subagent_policy_overridden",
        "tool_call_started",
        "tool_call_completed",
        "turn_completed",
    }
    completed_count = 0

    sub_id = await engine.submit(taifeng.UserMessage(
        text="请审查这段：def add(a, b): return a + b",
    ))
    async for ev in engine.subscribe(sub_id):
        kind = ev.msg.kind
        if kind in interesting:
            data = dict(ev.msg.data)
            # 修剪过长字段
            for k in ("output", "summary", "arguments"):
                if k in data and isinstance(data[k], str):
                    data[k] = data[k][:80] + ("..." if len(data[k]) > 80 else "")
            print(f"  ▸ {kind}: {data}", flush=True)
        if kind in ("turn_completed", "turn_failed"):
            completed_count += 1
            if completed_count >= 2 or kind == "turn_failed":
                break

    await pool.close()


async def main() -> None:
    # 注：没有 permission_policy 时 auto_deny / auto_allow 不会 emit
    # subagent_policy_overridden（spec：父 policy=None 时不包装）
    # 所以我们用一个简单 allow 全开的 policy，让包装生效

    policy = PermissionPolicy(
        rules=[
            # 明确允许 call_skill 派发（避免 default ask 触发 prompter）
            PermissionRule(
                scope="skill_dispatch", target_pattern="code-review",
                mode="allow",
            ),
        ],
        default_mode="allow",   # 简单 demo：默认放行
    )

    for mode in ("inherit", "auto_deny", "auto_allow"):
        await run_one_mode(mode, permission_policy=policy)

    print("\n" + "=" * 60, flush=True)
    print("✓ 三种 mode 演示完毕。", flush=True)
    print("  - inherit  ：子 turn 直接复用父 policy；无 subagent_policy_overridden",
          flush=True)
    print("  - auto_deny：子 turn 用 _SubagentAutoDecisionPolicy 包装，"
          "ask → auto_deny", flush=True)
    print("  - auto_allow：同上但 ask → auto_allow", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
