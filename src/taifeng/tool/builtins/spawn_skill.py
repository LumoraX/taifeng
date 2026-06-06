"""detached-spawn 的 4 个 LLM 入口工具 —— 让 LLM 在一个 turn 内主动发起 /
等待 / 查询 / 终止分离式子 skill。

工具本身不实现任何编排逻辑，只把调用透明转发到 AgentEngine 上已实现的 spawn API：

    - ``spawn_skill``  → ``engine.spawn_skill``     分离发起子 skill，立即返回句柄
    - ``await_skills`` → ``engine.set_join_barrier`` 登记 join-barrier（全终态后起聚合）
    - ``join_skill``   → ``engine.spawn_status``     非阻塞查询一批句柄当前状态 / 结果
    - ``kill_skill``   → ``engine.kill_spawn``       主动终止一个 spawn 子树

**wiring（工具如何拿到 engine）**：tool handler 只能拿到 ``ToolContext``，其
``extras['dispatcher']`` 是 *TurnRunner* 而非 engine。spawn API 全部在 engine 上，
故 engine 在构造每个 TurnRunner 时把自己注入到 ``TurnRunner.spawn_coordinator``，
``_build_tool_context`` 再把它放进 ``ctx.extras['spawn_coordinator']``。handler 据此
取到 coordinator（= engine）；缺失（无 engine 上下文，例如裸 TurnRunner 单测）时返回
``spawn_unavailable`` 错误，**不静默吞**。

4 个工具均 ``parallel_safe=True``：它们只是登记 + 启动（spawn 的真实计算在后台
detached task 跑），或纯只读查询（join），不独占父 turn 资源。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec


class SpawnCoordinator(Protocol):
    """spawn 协调器协议 —— 由 AgentEngine 实现并注入 TurnRunner.spawn_coordinator。

    只声明 4 个工具实际转发到的方法，避免工具层直接依赖 engine 具体类型。
    """

    async def spawn_skill(
        self, *, skill_id: str, args: dict[str, Any], reason: str
    ) -> dict[str, str]:
        ...

    async def set_join_barrier(
        self,
        handle_ids: list[str],
        then_skill_id: str,
        then_args_template: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        ...

    def spawn_status(self, handle_ids: list[str]) -> dict[str, dict[str, Any]]:
        ...

    async def kill_spawn(self, handle_id: str) -> None:
        ...


def _coordinator(ctx: ToolContext) -> SpawnCoordinator | None:
    """从 ctx.extras 取 spawn 协调器（engine 注入）；缺失返回 None。"""
    return ctx.extras.get("spawn_coordinator")


# ===========================================================================
# spawn_skill —— 分离发起一个子 skill
# ===========================================================================


def make_spawn_skill_tool() -> ToolSpec:
    """构造 spawn_skill ToolSpec —— LLM 分离发起一个子 skill（非阻塞）。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 边界校验：skill_id / reason 必填且为字符串（系统边界，禁静默占位）
        skill_id = args.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            return ToolResult.error(
                "missing_or_invalid_argument: skill_id", reason="bad_args"
            )
        raw_reason = args.get("reason")
        if not isinstance(raw_reason, str) or not raw_reason.strip():
            return ToolResult.error(
                "missing_or_invalid_argument: reason", reason="bad_args"
            )
        sub_args = args.get("args") or {}
        if not isinstance(sub_args, dict):
            return ToolResult.error(
                "invalid_argument: args must be object", reason="bad_args"
            )
        coordinator = _coordinator(ctx)
        if coordinator is None:
            return ToolResult.error("spawn_unavailable", reason="config_error")
        # 转发到 engine.spawn_skill（门控 / 配额 / detached task 启动均由 engine 负责）。
        # engine 在准入失败时抛 ValueError / SpawnLimitError —— 让其上抛，由 tool_batch
        # 统一捕获落 error（与 call_skill 一致，不在此层吞错）。
        out = await coordinator.spawn_skill(
            skill_id=skill_id, args=sub_args, reason=raw_reason
        )
        return ToolResult.ok(json.dumps(out, ensure_ascii=False))

    return ToolSpec(
        name="spawn_skill",
        description=(
            "分离式发起一个子 skill：立即返回句柄(handle_id / child_thread_id)，"
            "子 skill 在后台独立跑完，**不阻塞**你当前的 turn。适合并发铺开多个独立"
            "子任务（如同时让多个专科 skill 分析同一份资料）。\n\n"
            "与 call_skill 的区别：call_skill 同步阻塞等子 skill 跑完拿结果；"
            "spawn_skill 立即返回句柄，之后用 join_skill 查状态 / 取结果，或用"
            "await_skills 登记「全部跑完后自动起聚合 skill」。\n\n"
            "必须附带 reason（1-2 句第一人称说明为什么分离发起这个子 skill）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "要分离发起的子 skill id；须在当前 skill 的 child_skills 白名单内"
                    ),
                },
                "args": {
                    "type": "object",
                    "description": "传给子 skill 的种子参数（任意结构，由子 skill 自行解释）",
                    "additionalProperties": True,
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "1-2 句第一人称说明*为什么*要分离发起这个子 skill。"
                        "会出现在审批方 / 审计里。"
                    ),
                },
            },
            "required": ["skill_id", "reason"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 仅登记 + 启动后台 task，不独占父 turn
    )


# ===========================================================================
# await_skills —— 登记 join-barrier（全终态后起聚合 skill）
# ===========================================================================


def make_await_skills_tool() -> ToolSpec:
    """构造 await_skills ToolSpec —— 登记一个 join-barrier。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handle_ids = args.get("handle_ids")
        if not isinstance(handle_ids, list) or not all(
            isinstance(h, str) for h in handle_ids
        ):
            return ToolResult.error(
                "invalid_argument: handle_ids must be string array",
                reason="bad_args",
            )
        then_skill_id = args.get("then_skill_id")
        if not isinstance(then_skill_id, str) or not then_skill_id:
            return ToolResult.error(
                "missing_or_invalid_argument: then_skill_id", reason="bad_args"
            )
        then_args_template = args.get("then_args_template")
        if then_args_template is not None and not isinstance(
            then_args_template, dict
        ):
            return ToolResult.error(
                "invalid_argument: then_args_template must be object",
                reason="bad_args",
            )
        coordinator = _coordinator(ctx)
        if coordinator is None:
            return ToolResult.error("spawn_unavailable", reason="config_error")
        # engine 校验 handle / skill 存在性，失败抛 ValueError → 由 tool_batch 落 error
        out = await coordinator.set_join_barrier(
            handle_ids, then_skill_id, then_args_template
        )
        return ToolResult.ok(json.dumps(out, ensure_ascii=False))

    return ToolSpec(
        name="await_skills",
        description=(
            "登记一个 join-barrier：等给定的一批 spawn 句柄**全部进入终态**"
            "(done/error/cancelled)后，自动起一个聚合 skill 做联合会诊 / 汇总。"
            "失败或被取消的句柄终态也会带入聚合输入（不丢弃）。返回 barrier_id。\n\n"
            "用于「先 spawn_skill 铺开多个专家，再 await_skills 让它们全跑完后自动汇总」。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本 barrier 等待的全部 spawn 句柄 id（须均已注册）",
                },
                "then_skill_id": {
                    "type": "string",
                    "description": "全终态后自动起的聚合 skill id（须存在）",
                },
                "then_args_template": {
                    "type": "object",
                    "description": (
                        "聚合 skill 的自定义参数模板；省略则用默认（各句柄终态）"
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["handle_ids", "then_skill_id"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 仅登记 barrier，不独占父 turn
    )


# ===========================================================================
# join_skill —— 非阻塞查询一批句柄状态 / 结果
# ===========================================================================


def make_join_skill_tool() -> ToolSpec:
    """构造 join_skill ToolSpec —— 非阻塞查询一批 spawn 句柄当前状态 / 结果。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handle_ids = args.get("handle_ids")
        if not isinstance(handle_ids, list) or not all(
            isinstance(h, str) for h in handle_ids
        ):
            return ToolResult.error(
                "invalid_argument: handle_ids must be string array",
                reason="bad_args",
            )
        coordinator = _coordinator(ctx)
        if coordinator is None:
            return ToolResult.error("spawn_unavailable", reason="config_error")
        # 纯只读、非阻塞 —— 只报告当前状态 / 结果，**不**等待句柄终态（轮询由 LLM 决策）。
        # mode 仅作语义提示透传给 LLM 视野的 output，不在内核做阻塞语义区分。
        status = coordinator.spawn_status(handle_ids)
        return ToolResult.ok(json.dumps(status, ensure_ascii=False))

    return ToolSpec(
        name="join_skill",
        description=(
            "非阻塞查询一批 spawn 句柄的当前状态与结果（status: "
            "running/suspended/done/error/cancelled/unknown，及 result）。"
            "**不会阻塞**——只返回此刻的快照；若尚未跑完，过一会再查或用 await_skills "
            "登记自动聚合。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要查询的 spawn 句柄 id 列表",
                },
                "mode": {
                    "type": "string",
                    "enum": ["all", "any", "ready"],
                    "description": (
                        "语义提示（all=关心是否全终态 / any=任一终态 / ready=取已就绪结果）；"
                        "本工具始终非阻塞返回当前快照，mode 不改变阻塞行为"
                    ),
                },
            },
            "required": ["handle_ids"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 纯只读查询
    )


# ===========================================================================
# kill_skill —— 主动终止一个 spawn 子树
# ===========================================================================


def make_kill_skill_tool() -> ToolSpec:
    """构造 kill_skill ToolSpec —— 主动终止一个 detached spawn。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handle_id = args.get("handle_id")
        if not isinstance(handle_id, str) or not handle_id:
            return ToolResult.error(
                "missing_or_invalid_argument: handle_id", reason="bad_args"
            )
        coordinator = _coordinator(ctx)
        if coordinator is None:
            return ToolResult.error("spawn_unavailable", reason="config_error")
        # 未知句柄是调用方明确错误：engine.kill_spawn 抛 KeyError，转成可识别 error，
        # 不静默 no-op（与 engine.kill_spawn 的契约一致）。
        try:
            await coordinator.kill_spawn(handle_id)
        except KeyError:
            return ToolResult.error(
                f"unknown_handle: {handle_id}", reason="unknown_handle"
            )
        return ToolResult.ok(
            json.dumps({"handle_id": handle_id, "killed": True}, ensure_ascii=False)
        )

    return ToolSpec(
        name="kill_skill",
        description=(
            "主动终止一个 detached spawn（精确取消该 spawn 子树，不波及兄弟 / 父 turn）。"
            "已终态的句柄是良性 no-op；未知句柄返回 unknown_handle 错误。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle_id": {
                    "type": "string",
                    "description": "要终止的 spawn 句柄 id",
                },
            },
            "required": ["handle_id"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 单点取消写操作，不独占父 turn 采样
    )
