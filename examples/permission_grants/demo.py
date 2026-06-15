"""可复用审批 grant demo —— 「预先答好的 ask」（纯 SimClient，无需 API key，ADR 0022）。

场景：

    grant-entry (composite) ──call_skill──▶ specialist
                                    ▲
                                    │ skill_dispatch 默认 ask（会弹 prompter）
                                    │
            policy.issue_grant(scope=skill_dispatch, target=specialist)
                                    │
                                    ▼
                 命中 grant → 绕过 prompter → 静默放行

演示价值：
    1. 预签发一张 grant 后，匹配的 `ask` 请求**直接放行、prompter 一次都不调**
       （grant = 缓存的「人会点的 yes」）。
    2. `revoke_grant` 后同一请求**回落 prompter**（deny）→ 派发被拒。
    3. grant **绝不越过 `deny` 规则**（本 demo prompter 返回 deny，靠的是 grant 抢在
       prompter 之前；若有 deny 规则，grant 顶不翻——见 tests/permission/test_grant_policy.py）。

真实 LLM 版（同链路、真模型发起 call_skill）：examples/real_llm/grants_verify.py

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/permission_grants/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.permission import (
    PermissionDecision,
    PermissionGrant,
    PermissionPolicy,
    PermissionRequest,
)

_ENTRY = """---
name: grant-entry
description: grant demo 入口
version: 1.0.0
type: composite
entry: true
child_skills: [specialist]
max_call_depth: 3
---
# grant demo 入口
收到「会诊」请求时调用 `call_skill` 派发 specialist，再综合结论。
"""

_SPECIALIST = """---
name: specialist
description: 专科
version: 1.0.0
type: atomic
---
# 专科
给一句结论。
"""


class _CountingDenyPrompter:
    """计数 prompter：被调用即 +1 并返回 deny —— 用来证明 grant 是否绕过它。"""

    def __init__(self) -> None:
        self.calls = 0

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        self.calls += 1
        return PermissionDecision.deny(reason="prompter_deny")


def _client() -> SimClient:
    """脚本：parent 发起 call_skill → child 给结论 → parent 综合。"""
    return SimClient(turns=[
        SimTurn(
            text="我来发起会诊。",
            tool_calls=[{
                "id": "c1", "name": "call_skill",
                "arguments": '{"skill_id": "specialist", "args": {}}',
            }],
        ),
        SimTurn(text="结论：各项指标正常。"),   # child specialist
        SimTurn(text="综合：会诊完成。"),       # parent 收尾
    ])


def _write(skills: Path) -> None:
    for sub, body in {"grant-entry": _ENTRY, "specialist": _SPECIALIST}.items():
        (skills / sub).mkdir(parents=True)
        (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")


async def _run(policy: PermissionPolicy, skills: Path, threads: Path) -> list:
    """跑一轮「请发起会诊」，返回事件列表。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=_client(),
        compressors=[], permission_policy=policy,
    )
    engine = await pool.get_or_create(session_id="g", entry_skill_id="grant-entry")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="请发起会诊。"))
    for _ in range(200):
        if any(m.kind in ("turn_completed", "turn_failed") for m in events):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()
    return events


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        _write(skills)

        # ── 场景 A：预签发 grant → 绕过 prompter ──
        print("=" * 60)
        print("场景 A：预签发 grant —— 匹配的 ask 直接放行、不弹 prompter")
        print("=" * 60)
        grant_events: list[tuple[str, dict]] = []

        async def tel(kind: str, payload: dict) -> None:
            grant_events.append((kind, payload))

        prompter_a = _CountingDenyPrompter()
        policy_a = PermissionPolicy(
            default_mode="ask", prompter=prompter_a, telemetry=tel
        )
        policy_a.issue_grant(
            PermissionGrant(scope="skill_dispatch", target_pattern="specialist")
        )
        ev_a = await _run(policy_a, skills, root / "ta")
        hits = [p for k, p in grant_events if k == "permission_grant_hit"]
        dispatched = [m for m in ev_a if m.kind == "skill_dispatched"]
        print(f"  permission_grant_hit = {len(hits)}")
        print(f"  prompter 被调用       = {prompter_a.calls}  (期望 0)")
        print(f"  specialist 派发成功   = {len(dispatched)}")
        ok_a = bool(hits) and prompter_a.calls == 0 and bool(dispatched)
        print(f"  ==> {'✅ grant 绕过 prompter' if ok_a else '❌'}")

        # ── 场景 B：revoke 后回落 prompter（deny）──
        print("\n" + "=" * 60)
        print("场景 B：revoke_grant 后 —— 同请求回落 prompter（本 demo prompter=deny）")
        print("=" * 60)
        prompter_b = _CountingDenyPrompter()
        policy_b = PermissionPolicy(default_mode="ask", prompter=prompter_b)
        g = policy_b.issue_grant(
            PermissionGrant(scope="skill_dispatch", target_pattern="specialist")
        )
        policy_b.revoke_grant(g.grant_id)  # 撤销
        ev_b = await _run(policy_b, skills, root / "tb")
        denied = [m for m in ev_b if m.kind == "skill_dispatch_permission_denied"]
        print(f"  prompter 被调用       = {prompter_b.calls}  (期望 ≥1)")
        print(f"  派发被拒              = {len(denied)}")
        print(f"  ==> {'✅ revoke 生效，回落 prompter 并被拒' if prompter_b.calls >= 1 else '❌'}")


if __name__ == "__main__":
    asyncio.run(main())
