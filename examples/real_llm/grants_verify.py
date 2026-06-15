"""真实 LLM 验证：可复用审批 grant（permission-grants，ADR 0022）。

整链真实：真实 LLM 发起真 `call_skill`（skill_dispatch scope）→ 命中预先签发的
grant → **绕过 prompter**（prompter 一次都不调）→ 子 skill 真实派发执行。对照组：
`revoke_grant` 后同一请求回落 prompter（deny）→ 派发被拒。

证明点：
    1. grant 命中（`permission_grant_hit` 经 PolicyTelemetryCallback 上报）；
    2. prompter 调用次数 = 0（grant 绕过弹窗，而非靠 prompter 放行）；
    3. specialist 真实 dispatched（事件总线 `skill_dispatched`）；
    4. 对照：revoke 后回落 prompter → deny（grant 撤销生效）。

读 .env 的 LLM_BOOTSTRAP_*（见 examples/_provider_bootstrap.py）。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/grants_verify.py
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

# ── 验证用 skill：composite 入口 + 一个专科子 skill（call_skill 受 skill_dispatch 门控）──
_ENTRY = """---
name: grant-entry
description: grant 验证入口
version: 1.0.0
type: composite
entry: true
child_skills: [specialist]
max_call_depth: 3
---
# grant 验证入口
用户要求「会诊」时：**必须调用 `call_skill`**，参数
`{"skill_id": "specialist", "args": {}}`；拿到专科结论后用一句话综合。
"""

_SPECIALIST = """---
name: specialist
description: 专科
version: 1.0.0
type: atomic
---
# 专科
直接给一句结论。
"""


class _CountingDenyPrompter:
    """计数 prompter：被调用即记一次、并返回 deny（用于证明 grant 是否绕过它）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        self.calls += 1
        return PermissionDecision.deny(reason="prompter_deny")


def _write(skills: Path, spec: dict[str, str]) -> None:
    for sub, body in spec.items():
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


async def _drive(engine, text: str, events: list) -> None:
    """提交一条消息并等 root 终态（计数式，防上轮残留误判）。"""
    before = sum(1 for m in events if m.kind in ("turn_completed", "turn_failed"))
    await engine.submit(taifeng.UserMessage(text=text))
    for _ in range(1800):
        if sum(1 for m in events
               if m.kind in ("turn_completed", "turn_failed")) > before:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.2)


async def verify_grant(client, root: Path) -> None:
    print("\n=== grant：真实 LLM call_skill → 命中 grant → 绕过 prompter ===")
    skills = root / "s"
    _write(skills, {"grant-entry": _ENTRY, "specialist": _SPECIALIST})

    grant_events: list[tuple[str, dict]] = []

    async def _tel(kind: str, payload: dict) -> None:
        grant_events.append((kind, payload))

    prompter = _CountingDenyPrompter()
    # default ask → skill_dispatch 走 prompter（deny）；但预签发 grant 应抢先放行。
    policy = PermissionPolicy(default_mode="ask", prompter=prompter, telemetry=_tel)
    policy.issue_grant(
        PermissionGrant(scope="skill_dispatch", target_pattern="specialist",
                        grant_id="g-specialist")
    )

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t", model_client=client,
        compressors=[], permission_policy=policy,
    )
    engine = await pool.get_or_create(session_id="grant", entry_skill_id="grant-entry")
    events: list = []
    task = await _watch(engine, events)
    await _drive(engine, "请发起会诊。", events)

    dispatched = [m for m in events if m.kind == "skill_dispatched"]
    hits = [p for k, p in grant_events if k == "permission_grant_hit"]
    called_skill = any(m.kind == "tool_call_completed" for m in events) or dispatched
    print(f"[1] 真实 LLM 发起 call_skill = {bool(called_skill)}")
    print(f"[2] permission_grant_hit 次数 = {len(hits)}  prompter 调用 = {prompter.calls}")
    print(f"[3] specialist dispatched = {len(dispatched)}")
    if not called_skill:
        print("==> 真实 LLM 未发起 call_skill（遵循度）；grant 机制由 sim 单测覆盖"
              "（tests/permission/test_grant*.py）。")
    else:
        ok = len(hits) >= 1 and prompter.calls == 0 and len(dispatched) >= 1
        print(f"==> grant 绕过 prompter 真实链路{'确证 ✅' if ok else '未确证 ❌'}")

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def verify_revoke(client, root: Path) -> None:
    print("\n=== 对照：revoke 后回落 prompter（deny）===")
    skills = root / "s2"
    _write(skills, {"grant-entry": _ENTRY, "specialist": _SPECIALIST})

    prompter = _CountingDenyPrompter()
    policy = PermissionPolicy(default_mode="ask", prompter=prompter)
    policy.issue_grant(
        PermissionGrant(scope="skill_dispatch", target_pattern="specialist",
                        grant_id="g2")
    )
    policy.revoke_grant("g2")  # 撤销 → 应回落 prompter

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t2", model_client=client,
        compressors=[], permission_policy=policy,
    )
    engine = await pool.get_or_create(session_id="grant2", entry_skill_id="grant-entry")
    events: list = []
    task = await _watch(engine, events)
    await _drive(engine, "请发起会诊。", events)

    denied = [m for m in events if m.kind == "skill_dispatch_permission_denied"]
    called = any(m.kind == "tool_call_completed" for m in events) or denied
    print(f"[1] 真实 LLM 发起 call_skill = {bool(called)}")
    print(f"[2] revoke 后 prompter 调用 = {prompter.calls}  派发被拒 = {len(denied)}")
    if not called:
        print("==> 真实 LLM 未发起 call_skill（遵循度）；对照由 sim 单测覆盖。")
    else:
        ok = prompter.calls >= 1
        print(f"==> revoke 后回落 prompter{'确证 ✅' if ok else '未确证 ❌'}")

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        await verify_grant(client, root)
        await verify_revoke(client, root)


if __name__ == "__main__":
    asyncio.run(main())
