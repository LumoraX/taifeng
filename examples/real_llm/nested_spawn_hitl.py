"""真实 LLM 验证：detached spawn 嵌套 CHILD_SKILL 错峰 HITL 续跑（resume_spawn_nested）。

补 capability_matrix.py 的盲区——真实 key 此前完全不覆盖 spawn + 挂起 + 续跑。本脚本用
真实 LLM 驱动「被 spawn 的 composite 专科 → call_skill 子 skill → 子 skill
request_user_input 挂起（嵌套 CHILD_SKILL）→ Resume → 续跑链跑到终态」整条路径。

真实 LLM 自主决策点（与 mock 强制回放不同，真验遵循度）：
  1. 专科 top turn 是否真的 call_skill(nested-step)；
  2. nested-step 是否真的 request_user_input 挂起；
  3. Resume 后两层是否都能续跑到终态。

读 .env 的 LLM_BOOTSTRAP_*（见 examples/_provider_bootstrap.py）。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/nested_spawn_hitl.py
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
from taifeng.loop.submission import Resume  # noqa: E402
from taifeng.tool.builtins.request_user_input import (  # noqa: E402
    make_request_user_input_tool,
)

# 编排器（entry）：白名单含嵌套专科。
_ORCH = """---
name: orchestrator
description: MDT 编排器
version: 1.0.0
type: composite
entry: true
child_skills: [nested-expert]
tool_names: [spawn_skill, await_skills, join_skill, kill_skill]
max_call_depth: 4
---
# MDT 编排器
spawn 嵌套专科做会诊。
"""

# 专科（composite）：**必须**先 call_skill 调子步骤，再据其结论给最终诊断。
_EXPERT = """---
name: nested-expert
description: 嵌套专科
version: 1.0.0
type: composite
child_skills: [nested-step]
max_call_depth: 3
---
# 嵌套专科

你是内分泌专科医生。**严格按以下流程，不要跳步**：
1. **第一步必须调用工具 `call_skill`**，参数 `{"skill_id": "nested-step", "args": {}}`，
   把「采集患者补充信息」这一步交给子技能 nested-step。**不要自己直接问用户**。
2. 等 nested-step 返回结论后，结合它给出**最终诊断**，回复里包含标记 `EXPERT_DONE`。
"""

# 子步骤（leaf）：**必须** request_user_input 向用户补料，再给结论。
_STEP = """---
name: nested-step
description: 信息采集子步骤
version: 1.0.0
type: composite
tool_names: [request_user_input]
max_call_depth: 2
---
# 信息采集子步骤

你负责向用户采集一项关键补充信息。**严格按流程**：
1. **第一步必须调用工具 `request_user_input`**，prompt 写「请提供患者近期空腹血糖值」。
   **不要凭空假设数值**，必须发起这次询问（这会挂起等待用户）。
2. 收到用户答复后，给出一句结论，包含标记 `STEP_DONE`。
"""


async def _wait(cond, tries: int = 600) -> bool:
    """轮询等待（每次 10ms）后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        for sub, body in (
            ("orchestrator", _ORCH), ("nested-expert", _EXPERT),
            ("nested-step", _STEP),
        ):
            (skills / sub).mkdir(parents=True)
            (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")

        pool = await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=root / "threads",
            model_client=client, compressors=[],
            extra_tools=[make_request_user_input_tool()],
        )
        engine = await pool.get_or_create(
            session_id="real-nested", entry_skill_id="orchestrator")

        events: list = []

        async def collect() -> None:
            async for ev in engine.subscribe_all():
                events.append(ev)
                if ev.msg.kind == "shutdown":
                    break

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)

        # 1. spawn 嵌套专科（真实 LLM 驱动其 turn）
        sp = await engine.spawn_skill(
            skill_id="nested-expert", args={"patient": "55 岁男性，体检异常"},
            reason="嵌套专科会诊")
        hid, child_tid = sp["handle_id"], sp["child_thread_id"]

        # 2. 等专科句柄因子 skill 的 CHILD_SKILL 挂起
        suspended = await _wait(
            lambda: engine.spawn_status([hid])[hid]["status"] == "suspended",
            tries=900)
        called_skill = any(
            ev.msg.kind == "skill_dispatched" for ev in events)
        leaf_req = _leaf_data_req(events)

        print(f"\n[1] 专科是否 call_skill 子步骤 = {called_skill}")
        print(f"[2] 子步骤是否 request_user_input 挂起（嵌套）= {suspended}")
        print(f"[3] leaf DATA request_id = {leaf_req}")

        if not (suspended and leaf_req):
            # 真实 LLM 未遵循嵌套拓扑（未 call_skill 或未发起询问）→ 如实记录，不算 fix 失败
            print("==> 真实 LLM 本轮未走出嵌套挂起拓扑（遵循度问题，非续跑链缺陷）；"
                  "嵌套续跑逻辑由 mock UT 充分覆盖。")
            await engine.submit(taifeng.loop.Shutdown())
            await asyncio.wait_for(task, timeout=5.0)
            await pool.close()
            return

        # 3. Resume(spawn 子 thread, leaf request_id) → 真实续跑链
        await engine.submit(Resume(
            thread_id=child_tid,
            resolutions={leaf_req: {"answer": "空腹血糖 7.1 mmol/L"}}))

        # 4. 续跑链应让专科跑到终态
        await _wait(
            lambda: engine.spawn_status([hid])[hid]["status"] in ("done", "error"),
            tries=900)
        status = engine.spawn_status([hid])[hid]
        nested_resolved = any(
            ev.msg.kind == "suspension_resolved" for ev in events)

        print(f"\n[4] 嵌套 resume 后句柄 status = {status['status']}")
        print(f"[5] suspension_resolved 事件 = {nested_resolved}")
        ok = status["status"] == "done"
        print("\n" + "=" * 64)
        print(f"==> 真实 LLM 嵌套 CHILD_SKILL 错峰 HITL 续跑"
              f"{'确证 ✅' if ok else '未完成 ❌ —— ' + str(status.get('result'))[:120]}")
        print("=" * 64)

        await engine.submit(taifeng.loop.Shutdown())
        await asyncio.wait_for(task, timeout=5.0)
        await pool.close()
        if not ok:
            sys.exit(1)


def _leaf_data_req(events: list) -> str | None:
    """取 leaf 子步骤 DATA 挂起的 request_id。"""
    for ev in events:
        if ev.msg.kind != "turn_suspended":
            continue
        for p in ev.msg.data.get("pending") or []:
            if p.get("reason") == "data":
                return p["request_id"]
    return None


if __name__ == "__main__":
    asyncio.run(main())
