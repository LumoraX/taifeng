"""capability_matrix 驱动逻辑自检 —— SimClient 干跑，不花真实 key。

烧 key 前的 pre-flight：用 conformance 模拟器验证 driver 编排（事件轮询 / Resume /
Rewind 提交时序）本身无 bug。**只覆盖可静态剧本化的 driver**：

- ``suspend_resume``：HITL 挂起 → Resume 续跑；
- ``turn_rewind``：完成 → Rewind(re_reason) → 二次完成；
- ADR 0056–0065 六个新场景（``skill_inference`` / ``tool_output_guard`` /
  ``pinned_periodic`` / ``file_search`` / ``read_skill_path`` / ``compaction_continuity``）：
  剧本按 driver 断言的取证点作答（token 与标记取自 ``_setups``），同时验证 driver 的
  区分断言本身在「能力生效」时能通过。

``spawn_join`` / ``peer_messaging`` / ``wait_any`` 的脚本需要动态句柄（spawn 返回的
handle_id / child_thread_id 进 await_skills / send_message / wait_any 参数），静态剧本
无法表达——其 driver 与 suspend_resume 同构（事件轮询 + 按 child thread Resume），
动态参数路径由真实回归（capability_matrix.py 全量）首验。

运行：
    PYTHONPATH=src uv run python examples/real_llm/selfcheck.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _setups import (  # noqa: E402
    COMPACT_CODE,
    COMPACT_MARK,
    GUARD_HEAD_CODE,
    GUARD_MARK,
    GUARD_TAIL_CODE,
    GUARD_TOOL,
    INFERENCE_CHILD_MARK,
    INFERENCE_ENTRY_MARK,
    PINNED_FIRST_CODE,
    PINNED_HOST_CODE,
    PINNED_MARK,
    READ_PATH_CODE,
    READ_PATH_FILE,
    READ_PATH_MARK,
    READ_PATH_SKILL,
    SEARCH_CODE,
    SEARCH_MARK,
    SEARCH_TARGET,
)
from capability_matrix import SCENARIOS, _verdict, run_scenario  # noqa: E402
from test_codex_image_matrix import preflight_codex_image_matrix  # noqa: E402
from test_openai_image_matrix import preflight_openai_image_matrix  # noqa: E402

from taifeng.llm.providers import RoutingSimClient, SimTurn  # noqa: E402


def _call(call_id: str, name: str, **arguments: object) -> dict[str, str]:
    """构造 SimTurn 的一次工具调用（arguments 序列化为 JSON 字符串）。"""
    return {"id": call_id, "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False)}


# ADR 0056–0065 新场景的静态剧本。路由按插入序取首个命中标记：子 skill / 压缩摘要的
# 标记排在 entry 之前（它们的请求里可能夹带 entry 侧文本，反之不会）。
_TODO_ITEMS = [
    {"content": f"盘点机柜设备（资产编号 {PINNED_FIRST_CODE}）", "status": "pending"},
    {"content": "预约搬运车辆", "status": "pending"},
    {"content": "通知各部门搬迁时间", "status": "pending"},
]
EXTRA_SIM_ROUTES = {
    "skill_inference": {
        INFERENCE_CHILD_MARK: [SimTurn(text="修复登录页按钮错位 → 修复\n新增订单导出 → 新功能\n"
                                            "升级日志组件 → 维护")],
        INFERENCE_ENTRY_MARK: [
            SimTurn(text="先分类。", tool_calls=[_call(
                "cs1", "call_skill", skill_id="change-classifier",
                args={"items": "修复登录页按钮错位；新增订单导出；升级日志组件"},
                reason="需要先把变更条目分类")]),
            SimTurn(text="本次发布以一项修复和一项新功能为主，另含依赖维护。"),
        ],
    },
    "tool_output_guard": {
        GUARD_MARK: [
            SimTurn(text="先取日志。", tool_calls=[_call("g1", GUARD_TOOL)]),
            SimTurn(text=f"HEAD-TOKEN 是 {GUARD_HEAD_CODE}，TAIL-TOKEN 是 {GUARD_TAIL_CODE}。"),
        ],
    },
    "pinned_periodic": {
        PINNED_MARK: [
            SimTurn(text="建清单。", tool_calls=[_call("t1", "todo_write", items=_TODO_ITEMS)]),
            SimTurn(text="已建立"),
            SimTurn(text="确认新址的网络与门禁已开通。"),
            SimTurn(text="大约需要六个人。"),
            SimTurn(text=f"[ ] 领取门禁临时卡（凭证号 {PINNED_HOST_CODE}）"),
        ],
    },
    "file_search": {
        SEARCH_MARK: [
            SimTurn(text="按内容搜索。", tool_calls=[_call("s1", "grep", pattern=SEARCH_CODE)]),
            SimTurn(text=f"记录在 {SEARCH_TARGET}。"),
        ],
    },
    "read_skill_path": {
        READ_PATH_MARK: [
            SimTurn(text="先读正文。", tool_calls=[_call("r1", "read_skill",
                                                        skill_id=READ_PATH_SKILL)]),
            SimTurn(text="再读细则。", tool_calls=[_call("r2", "read_skill",
                                                        skill_id=READ_PATH_SKILL,
                                                        path=READ_PATH_FILE)]),
            SimTurn(text=f"复核口令是 {READ_PATH_CODE}。"),
        ],
    },
    "compaction_continuity": {
        # handoff 摘要请求的 system prompt 标记（HANDOFF_SYSTEM_PROMPT_ZH）
        "接力提示词": [SimTurn(text="## 进度\n已介绍 3-2-1 原则与增量 / 全量备份的区别。")],
        COMPACT_MARK: [
            SimTurn(text=f"明白。\n签名：{COMPACT_CODE}"),
            SimTurn(text=f"3 份副本、2 种介质、1 份异地。\n签名：{COMPACT_CODE}"),
            SimTurn(text=f"增量只备变化部分，全量每次全备。\n签名：{COMPACT_CODE}"),
            SimTurn(text=f"照片按 3-2-1 原则多地多介质保存。\n签名：{COMPACT_CODE}"),
        ],
    },
}

# 各场景的静态剧本（标记 = 对应 entry skill body 中的稳定文案）
SIM_ROUTES = {
    "suspend_resume": {
        # intake-assistant body 标记
        "信息采集助手": [
            SimTurn(text="先确认信息。", tool_calls=[{
                "id": "ask1", "name": "request_user_input",
                "arguments": '{"prompt": "请提供出发城市、预算范围与出行日期"}'}]),
            SimTurn(text="收到，建议提前两周订票并预留机动预算。"),
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
        "专项分析": [
            SimTurn(text="子结论 v1：影响轻微。"),
            SimTurn(text="子结论 v2（重推）：存在显著个体差异。"),
        ],
    },
    **EXTRA_SIM_ROUTES,
}


async def main() -> None:
    """对可静态化的 driver 场景逐个 sim 干跑，断言 PASS。"""
    failures: list[str] = []
    try:
        preflight_openai_image_matrix()
        print("  ✅  openai_image    双协议图片 wire/脱敏预检")
    except Exception as exc:  # noqa: BLE001 —— 汇总所有零消耗预检失败
        failures.append(f"openai_image: {type(exc).__name__}: {exc}")
    try:
        preflight_codex_image_matrix()
        print("  ✅  codex_image     instructions/list/done/state/脱敏预检")
    except Exception as exc:  # noqa: BLE001 —— 汇总所有零消耗预检失败
        failures.append(f"codex_image: {type(exc).__name__}: {exc}")
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
