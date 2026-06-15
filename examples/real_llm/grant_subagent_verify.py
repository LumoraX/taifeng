"""真实 LLM 验证：grant 的子 agent 生效范围（ADR 0022 决策五「auto 模式硬墙」）。

担心「写错」的正是这条：grant **仅在 inherit 模式子 turn 生效**；auto_deny / auto_allow
子 turn 走 `_SubagentAutoDecisionPolicy`，有意绕过交互式审批（含缓存的 grant）。

为什么必须用嵌套 call_skill 探测（而非 shell 等工具）：内置工具如 shell_exec 用的是
**构造时绑定的 policy**，不经过子 runner 的 policy；只有 call_skill 的 skill_dispatch
检查从 ctx 取**子 runner 的 policy**（auto_deny 下即 `_SubagentAutoDecisionPolicy`
包装）。所以唯一能触达子 turn 包装的门控动作是 call_skill（同 verify_breaker 用
`Skill(*)` 的原因）。

A/B 设计（**同一 3 层 skill 树、同一组 grant**，只翻 `subagent_approval_mode`）：

    entry ──call_skill──▶ mid ──call_skill──▶ leaf
       │(root,真实 policy)   │(子 turn,受 subagent_approval_mode 控制)
       ▼                      ▼
    grant(mid) root 必命中     grant(leaf) 是否命中 = 本验证焦点

    - inherit（对照）：mid 复用同一 policy → grant(leaf) **命中** → leaf 被派发
    - auto_deny（焦点）：mid 走 _SubagentAutoDecisionPolicy → grant(leaf) **不命中**
      （硬墙）→ leaf 被拒（fallback deny）

注：判别需真实模型在子 turn 内**再发起一次** call_skill（嵌套派发），遵循度有限；
机制底线由 sim 单测 `test_grant_hard_wall_under_auto_deny_subagent` 确定性覆盖。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/grant_subagent_verify.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.permission import (  # noqa: E402
    PermissionDecision,
    PermissionGrant,
    PermissionPolicy,
    PermissionRequest,
)
from taifeng.skill import DispatchPolicy  # noqa: E402

_ENTRY = """---
name: mdt-entry
description: 会诊入口
version: 1.0.0
type: composite
entry: true
child_skills: [mid-specialist]
max_call_depth: 4
---
# 会诊入口
用户要求「会诊」时：**第一步就调用 `call_skill`**，参数严格为
`{"skill_id": "mid-specialist", "args": {}}`。等 mid-specialist 返回后，用一句话综合。
"""

# mid 的唯一职责就是再下派一次 leaf-expert —— 最大化嵌套派发的遵循度。
_MID = """---
name: mid-specialist
description: 中层调度（唯一职责：下派 leaf-expert）
version: 1.0.0
type: composite
child_skills: [leaf-expert]
max_call_depth: 4
---
# 中层调度
你**唯一**的职责：被派发后**立刻调用 `call_skill`**，参数严格为
`{"skill_id": "leaf-expert", "args": {}}`。**禁止**自己直接作答、禁止跳过这一步。
只有在 call_skill 返回 permission_denied 错误时，才回一句「无法获取 leaf 结论」。
"""

_LEAF = """---
name: leaf-expert
description: 叶子专家
version: 1.0.0
type: atomic
---
# 叶子专家
给一句结论。
"""


class _CountingDenyPrompter:
    """计数 prompter：被调用即 +1 并返回 deny（证明放行靠 grant 而非 prompter）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        self.calls += 1
        return PermissionDecision.deny(reason="prompter_deny")


def _write(skills: Path) -> None:
    for sub, body in {
        "mdt-entry": _ENTRY, "mid-specialist": _MID, "leaf-expert": _LEAF,
    }.items():
        (skills / sub).mkdir(parents=True)
        (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")


async def _watch(engine, events: list):
    async def w():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break
    task = asyncio.create_task(w())
    await asyncio.sleep(0)
    return task


async def _drive(engine, events: list) -> None:
    before = sum(1 for m in events if m.kind in ("turn_completed", "turn_failed"))
    await engine.submit(taifeng.UserMessage(text="请发起会诊。"))
    for _ in range(2400):
        if sum(1 for m in events
               if m.kind in ("turn_completed", "turn_failed")) > before:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)


async def _run(mode: str, root: Path):
    """跑一次会诊；返回 (事件列表, grant 命中的 target 列表, prompter 调用数)。"""
    skills = root / mode
    _write(skills)
    grant_hits: list[str] = []

    async def tel(kind: str, payload: dict) -> None:
        if kind == "permission_grant_hit":
            grant_hits.append(payload.get("target_pattern", "?"))

    prompter = _CountingDenyPrompter()
    policy = PermissionPolicy(default_mode="ask", prompter=prompter, telemetry=tel)
    # 同一组 grant：mid 与 leaf 都预签发（差异只应来自 subagent_approval_mode）。
    policy.issue_grant(PermissionGrant(scope="skill_dispatch",
                                       target_pattern="mid-specialist"))
    policy.issue_grant(PermissionGrant(scope="skill_dispatch",
                                       target_pattern="leaf-expert"))

    client, _ = build_model_client(timeout_seconds=120.0)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / f"t-{mode}", model_client=client,
        compressors=[], permission_policy=policy,
        dispatch_policy=DispatchPolicy(subagent_approval_mode=mode),
    )
    engine = await pool.get_or_create(session_id=mode, entry_skill_id="mdt-entry")
    events: list = []
    task = await _watch(engine, events)
    await _drive(engine, events)
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()
    return events, grant_hits, prompter.calls


def _dispatched(events: list, skill_id: str) -> int:
    return sum(1 for m in events
               if m.kind == "skill_dispatched"
               and m.data.get("skill_id") == skill_id)


def _denied(events: list, skill_id: str) -> int:
    return sum(1 for m in events
               if m.kind == "skill_dispatch_permission_denied"
               and m.data.get("target_skill_id") == skill_id)


async def verify_inherit(root: Path) -> None:
    print("\n=== A. inherit（对照）：子 turn 内 grant(leaf) 应**命中** ===")
    events, hits, pcalls = await _run("inherit", root)
    mid_d, leaf_d = _dispatched(events, "mid-specialist"), _dispatched(events, "leaf-expert")
    print(f"[1] mid 派发 = {mid_d}  leaf 派发 = {leaf_d}")
    print(f"[2] grant 命中 = {hits}  prompter 调用 = {pcalls}")
    if mid_d == 0 or leaf_d == 0:
        print("==> 真实 LLM 未完成嵌套派发（遵循度）；inherit 生效由 sim 覆盖。")
    elif "leaf-expert" in hits and pcalls == 0:
        print("==> ✅ inherit：子 turn 内 grant(leaf) 命中、leaf 被派发（prompter 未介入）")
    else:
        print("==> 未确证（遵循度）；机制由 sim 覆盖。")


async def verify_auto_deny(root: Path) -> None:
    print("\n=== B. auto_deny（焦点）：子 turn 内 grant(leaf) 应**被硬墙挡住** ===")
    events, hits, pcalls = await _run("auto_deny", root)
    mid_d, leaf_d = _dispatched(events, "mid-specialist"), _dispatched(events, "leaf-expert")
    leaf_denied = _denied(events, "leaf-expert")
    print(f"[1] mid 派发 = {mid_d}（root 锚点）  leaf 派发 = {leaf_d}  leaf 被拒 = {leaf_denied}")
    print(f"[2] grant 命中 = {hits}  prompter 调用 = {pcalls}")
    if mid_d == 0:
        print("==> 真实 LLM 未发起首层 call_skill（遵循度）；硬墙由 sim 覆盖。")
    elif leaf_denied >= 1 and leaf_d == 0 and "leaf-expert" not in hits:
        print("==> ✅ auto_deny 硬墙确证：mid 真发起了下派、但子 turn 内 grant(leaf) "
              "**未命中**、leaf 被拒（root 锚点 grant(mid) 却命中）—— 没写错，auto 不消费 grant")
    elif leaf_d >= 1 or "leaf-expert" in hits:
        print(f"==> ❌ 异常：auto_deny 下 leaf 竟被派发 / grant(leaf) 命中（hits={hits}）"
              "—— 需排查硬墙写错！")
    else:
        print(f"==> mid 未真正下派 leaf（遵循度），判别点未触达（leaf_denied={leaf_denied}）；"
              "硬墙由 sim 覆盖（test_grant_hard_wall_under_auto_deny_subagent）。")


async def main() -> None:
    try:
        _, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        await verify_inherit(root)
        await verify_auto_deny(root)


if __name__ == "__main__":
    asyncio.run(main())
