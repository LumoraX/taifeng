"""capability_matrix 驱动逻辑自检 —— SimClient 干跑，不花真实 key。

烧 key 前的 pre-flight：用 conformance 模拟器验证 driver 编排（事件轮询 / Resume /
Rewind 提交时序）本身无 bug。**只覆盖可静态剧本化的 driver**：

- ``suspend_resume``：HITL 挂起 → Resume 续跑；
- ``turn_rewind``：完成 → Rewind(re_reason) → 二次完成。

``spawn_join`` / ``peer_messaging`` 的脚本需要动态句柄（spawn 返回的 handle_id /
child_thread_id 进 await_skills / send_message 参数），静态剧本无法表达——其 driver
与 suspend_resume 同构（事件轮询 + 按 child thread Resume），动态参数路径由真实
回归（capability_matrix.py 全量）首验。

运行：
    PYTHONPATH=src uv run python examples/real_llm/selfcheck.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capability_matrix import SCENARIOS, _verdict, run_scenario  # noqa: E402
from test_openai_image_matrix import preflight_openai_image_matrix  # noqa: E402

from taifeng.llm.providers import RoutingSimClient, SimTurn  # noqa: E402

# 各场景的静态剧本（标记 = 对应 entry skill body 中的稳定文案）
SIM_ROUTES = {
    "suspend_resume": {
        # intake-assistant body 标记
        "信息采集助手": [
            SimTurn(text="先确认信息。", tool_calls=[{
                "id": "ask1", "name": "request_user_input",
                "arguments": '{"prompt": "请提供年龄、慢性病史与近期不适"}'}]),
            SimTurn(text="收到，根据您的情况建议规律作息并复查。"),
        ],
    },
    "turn_rewind": {
        # turn_rewind orchestrator body 标记
        "编排器": [
            SimTurn(text="结论 v1：远程办公利大于弊。"),
            SimTurn(text="结论 v2（重推）：需配套异步协作机制。"),
        ],
    },
    "thread_rewind": {
        # turn_rewind analyzer body 标记(被 spawn 的子 thread:首跑 + 重推各一)
        "专科分析": [
            SimTurn(text="子结论 v1：影响轻微。"),
            SimTurn(text="子结论 v2（重推）：存在显著个体差异。"),
        ],
    },
}


async def main() -> None:
    """对可静态化的 driver 场景逐个 sim 干跑，断言 PASS。"""
    failures: list[str] = []
    try:
        preflight_openai_image_matrix()
        print("  ✅  openai_image    双协议图片 wire/脱敏预检")
    except Exception as exc:  # noqa: BLE001 —— 汇总所有零消耗预检失败
        failures.append(f"openai_image: {type(exc).__name__}: {exc}")
    for sc in SCENARIOS:
        routes = SIM_ROUTES.get(sc.demo_id)
        if routes is None:
            continue
        client = RoutingSimClient(routes=routes)
        with tempfile.TemporaryDirectory() as td:
            res = await run_scenario(client, sc, Path(td) / "logs")
        tag, note = _verdict(res)
        print(f"  {tag}  {sc.demo_id:16s} {note}")
        if not tag.startswith("✅"):
            failures.append(f"{sc.demo_id}: {note}")
        if client.ledger.violations:
            failures.append(f"{sc.demo_id}: sim 合同违规 {client.ledger.violations}")
    if failures:
        print(f"\n❌ selfcheck 未过: {failures}")
        sys.exit(1)
    print("\n✅ driver 编排自检通过（sim 干跑，零 key 消耗）")


if __name__ == "__main__":
    asyncio.run(main())
