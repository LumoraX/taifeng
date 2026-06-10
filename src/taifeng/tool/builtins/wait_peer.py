"""wait_peer —— turn 内阻塞等待一个 spawn 句柄到终态。

与 ``await_skills``(join-barrier)的分工:barrier 是「登记后 turn 结束、全终态
自动起聚合」;wait_peer 是「turn 内原地等**单个**句柄」。两个 agent 互相 wait 的
死锁无法静态防 → ``timeout_seconds`` 必填(schema required,对标 codex
``wait_agent``)。等待经 ``ctx.cancel`` 级联取消(R4)。

超时返回 error 结果(turn 不失败);终态返回 {status, result}。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken


class WaitCoordinator(Protocol):
    """等待协调器协议 —— 由 AgentEngine 实现(spawn_coordinator 同一对象)。"""

    async def wait_spawn_terminal(
        self,
        *,
        handle_id: str,
        timeout_seconds: float,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        ...


def make_wait_peer_tool() -> ToolSpec:
    """构造 wait_peer ToolSpec —— turn 内阻塞等单个 spawn 句柄终态。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handle_id = args.get("handle_id")
        if not isinstance(handle_id, str) or not handle_id:
            return ToolResult.error(
                "missing_or_invalid_argument: handle_id", reason="bad_args"
            )
        timeout = args.get("timeout_seconds")
        if not isinstance(timeout, int | float) or timeout <= 0:
            return ToolResult.error(
                "missing_or_invalid_argument: timeout_seconds(必填正数)",
                reason="bad_args",
            )
        coordinator: WaitCoordinator | None = ctx.extras.get("spawn_coordinator")
        if coordinator is None:
            return ToolResult.error("peer_unavailable", reason="config_error")
        try:
            out = await coordinator.wait_spawn_terminal(
                handle_id=handle_id, timeout_seconds=float(timeout),
                cancel=ctx.cancel)
        except ValueError as e:
            return ToolResult.error(str(e), reason="unknown_handle")
        if out["outcome"] == "timeout":
            # 超时是预期可达状态:error 结果让 LLM 决定重试/放弃,turn 不失败
            return ToolResult.error(
                f"wait_peer_timeout: {handle_id} 在 {timeout}s 内未到终态"
                f"(当前 {out['status']})",
                reason="timeout",
            )
        return ToolResult.ok(json.dumps(
            {"status": out["status"], "result": out["result"]},
            ensure_ascii=False))

    return ToolSpec(
        name="wait_peer",
        description=(
            "在当前 turn 内**阻塞等待**一个 spawn 句柄进入终态"
            "(done/error/cancelled),返回 {status, result}。\n\n"
            "timeout_seconds 必填:两个 agent 互相等待会死锁,超时是唯一保底"
            "(超时返回 timeout 错误,你的 turn 继续)。\n"
            "与 await_skills 的区别:await_skills 登记 barrier 后本 turn 结束、"
            "全终态自动起聚合 skill;wait_peer 在本 turn 内原地等单个句柄。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle_id": {
                    "type": "string",
                    "description": "要等待的 spawn 句柄 id",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "最长等待秒数(必填;超时返回 timeout 错误)",
                },
            },
            "required": ["handle_id", "timeout_seconds"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 纯等待不写状态
    )
