"""turn 内阻塞等待 spawn 句柄的两个工具:``wait_peer``(等一个)/ ``wait_any``(等任一)。

三档等待语义,按「等几个」划分:

- ``wait_peer``  —— turn 内原地等**单个**句柄到终态
- ``wait_any``   —— turn 内等一组句柄中的**任一个**,唤醒时收走当时全部已终态
- ``await_skills``(join-barrier,在 spawn_skill.py)—— 登记后 turn 结束、**全部**
  终态自动起聚合

中间那档(any-of-N)对标 codex ``wait_agent``:缺了它,面对错峰完成的 N 个子任务
只能盯死一个或等最慢的,想「谁先好先处理谁」只能让 LLM 轮询 join_skill,每轮烧一
次采样。

两个 agent 互相 wait 的死锁无法静态防 → ``timeout_seconds`` 一律必填
(schema required,对标 codex ``wait_agent``)。等待经 ``ctx.cancel`` 级联取消(R4)。

超时返回 error 结果(turn 不失败);终态返回句柄的 {status, result}。
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

    async def wait_spawn_any(
        self,
        *,
        handle_ids: list[str],
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


def make_wait_any_tool() -> ToolSpec:
    """构造 wait_any ToolSpec —— turn 内等一组 spawn 句柄中的**任一个**到终态。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handle_ids = args.get("handle_ids")
        if not isinstance(handle_ids, list) or not handle_ids or not all(
                isinstance(h, str) and h for h in handle_ids):
            return ToolResult.error(
                "missing_or_invalid_argument: handle_ids(必填非空字符串列表)",
                reason="bad_args",
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
            out = await coordinator.wait_spawn_any(
                handle_ids=handle_ids, timeout_seconds=float(timeout),
                cancel=ctx.cancel)
        except ValueError as e:
            return ToolResult.error(str(e), reason="unknown_handle")
        if out["outcome"] == "timeout":
            # 超时是预期可达状态:error 结果让 LLM 决定继续等/放弃,turn 不失败
            return ToolResult.error(
                f"wait_any_timeout: {len(out['pending'])} 个句柄在 {timeout}s 内"
                f"无一到终态({', '.join(out['pending'])})",
                reason="timeout",
            )
        return ToolResult.ok(json.dumps(
            {"settled": out["settled"], "pending": out["pending"]},
            ensure_ascii=False))

    return ToolSpec(
        name="wait_any",
        description=(
            "在当前 turn 内**阻塞等待**一组 spawn 句柄,其中**任一个**进入终态"
            "(done/error/cancelled)即返回,不等其余。\n\n"
            "返回 {settled, pending}:settled 是唤醒当时**已终态**的全部句柄"
            "(可能不止一个),pending 是仍在跑的。想继续等剩下的,把 pending "
            "再传一次即可。\n"
            "用它做错峰处理:N 个专家谁先出结论就先处理谁,不必等最慢的那个。\n\n"
            "timeout_seconds 必填:互相等待会死锁,超时是唯一保底(超时返回 "
            "timeout 错误,你的 turn 继续)。\n"
            "与 wait_peer 的区别:wait_peer 只等你指定的那一个句柄;"
            "与 await_skills 的区别:await_skills 等**全部**跑完才起聚合 skill。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要等待的 spawn 句柄 id 列表(非空)",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "最长等待秒数(必填;零个终态即到期返回 timeout 错误)",
                },
            },
            "required": ["handle_ids", "timeout_seconds"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 纯等待不写状态
    )
