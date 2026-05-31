"""Web prompter 示例 —— 模拟 Web SSE 业务层接 PermissionPolicy + CallbackPrompter。

展示 permission-gate-completeness 引入的能力：
    - typed PermissionRequest 字段（thread_id / call_chain / entry_skill_id / ...）
    - PermissionPolicy.prompter_timeout_seconds（防止 prompter 卡死）
    - pre_skill_dispatch / post_skill_dispatch hook lifecycle
    - skill_dispatch_permission_denied / skill_dispatch_hook_denied 事件

业务侧场景：在医疗后端，free tier 用户不允许 dispatch 到 oncology-deep-analysis；
prompter 通过 Web SSE 把请求推给前端，前端弹审批框；用户 60s 没响应 → timeout deny。

运行：
    cd taifeng
    PYTHONPATH=src python examples/permission/web_prompter.py
"""

from __future__ import annotations

import asyncio

import anyio

from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)


async def web_callback(request: PermissionRequest) -> PermissionDecision:
    """模拟 Web SSE prompter：把 request 推给前端 + 等回调。

    生产实现：
        1. 通过 SSE / WebSocket 把 typed 字段渲染成审批 UI
        2. 创建 asyncio.Future；前端点击"允许 / 拒绝"通过 HTTP POST 触发 Future
        3. await future 返回 PermissionDecision
    """
    print("\n========== Web 审批面板 ==========")
    print(f"  scope:           {request.scope}")
    print(f"  target:          {request.target}")
    print(f"  reason:          {request.reason or '(LLM 未提供)'}")
    print("  --- typed 上下文 ---")
    print(f"  thread_id:       {request.thread_id}")
    print(f"  submission_id:   {request.submission_id}")
    print(f"  entry_skill_id:  {request.entry_skill_id}")
    print(f"  call_chain:      {list(request.call_chain)}")
    print(f"  turn_index:      {request.turn_index}")
    # 业务侧领域上下文（如租户 / 用户）全部在 metadata 里，taifeng 不解析（R1）
    print(f"  metadata:        {request.metadata}")
    print("==================================")
    # 模拟用户思考 200ms 后允许
    await anyio.sleep(0.2)
    return PermissionDecision.allow(
        reason="用户点击允许（mock）", remember="session"
    )


async def telemetry_sink(kind: str, payload: dict) -> None:
    """模拟业务侧 telemetry —— 写入监控系统（这里只打 console）。"""
    print(f"[telemetry] {kind}: {payload}")


async def main() -> None:
    # 1. 业务侧构造 PermissionPolicy
    #    - rules：黑名单 'oncology-deep-analysis'（free tier）
    #    - default_mode=ask：未知 target 走 prompter
    #    - prompter_timeout_seconds=2：2 秒未响应自动 deny
    #    - telemetry：超时事件推到业务监控
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                scope="skill_dispatch",
                target_pattern="oncology-deep-analysis",
                mode="deny",
                reason="free_tier_blocked",
            ),
        ],
        default_mode="ask",
        prompter=CallbackPrompter(web_callback),
        prompter_timeout_seconds=2.0,
        telemetry=telemetry_sink,
    )

    # 2. 演示三种典型请求
    print("\n--- 案例 1：白名单内的 child skill (走 prompter，用户允许) ---")
    req1 = PermissionRequest.for_skill_dispatch(
        "metabolic-detailed",
        caller_skill_id="general-fm",
        call_chain=("general-fm",),
        thread_id="th-001",
        submission_id="sub-aaa",
        entry_skill_id="general-fm",
        turn_index=2,
        extra_metadata={"tenant": "demo-7"},  # 业务侧不透明上下文，合并进 metadata
    )
    d1 = await policy.check(req1)
    print(f"  → granted={d1.granted} reason={d1.reason} remember={d1.remember_until}")

    print("\n--- 案例 2：黑名单内的 child skill (规则 deny，不走 prompter) ---")
    req2 = PermissionRequest.for_skill_dispatch(
        "oncology-deep-analysis",
        caller_skill_id="general-fm",
        call_chain=("general-fm",),
        thread_id="th-001",
        submission_id="sub-bbb",
        entry_skill_id="general-fm",
        turn_index=3,
        extra_metadata={"tenant": "demo-7"},  # 业务侧不透明上下文，合并进 metadata
    )
    d2 = await policy.check(req2)
    print(f"  → granted={d2.granted} reason={d2.reason}")

    print("\n--- 案例 3：prompter 慢响应触发 timeout ---")

    async def slow_callback(request: PermissionRequest) -> PermissionDecision:
        # 模拟前端用户睡着了（5 秒不响应）
        await anyio.sleep(5.0)
        return PermissionDecision.allow(reason="should_never_be_returned")

    policy_with_slow = PermissionPolicy(
        default_mode="ask",
        prompter=CallbackPrompter(slow_callback),
        prompter_timeout_seconds=0.5,  # 500ms 超时
        telemetry=telemetry_sink,
    )
    req3 = PermissionRequest.for_tool_call(
        "shell_exec",
        {"cmd": "ls /"},
        thread_id="th-002",
        submission_id="sub-ccc",
        entry_skill_id="ops",
        turn_index=1,
    )
    d3 = await policy_with_slow.check(req3)
    print(f"  → granted={d3.granted} reason={d3.reason}")


if __name__ == "__main__":
    asyncio.run(main())
