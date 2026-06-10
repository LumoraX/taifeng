"""send_message —— 谱系内 peer 点对点投递的 LLM 入口工具。

与 spawn 四工具同范式:handler 经 ``ctx.extras['spawn_coordinator']`` 取协调器
(= AgentEngine),转发到 ``deliver_peer_message``(``SendToPeer`` Op 共用同一
路径)。寻址 = thread_id / handle_id / "parent";双模式 queue_only / trigger_turn
(语义详见 docs/architecture/capabilities/peer-mailbox-messaging.md)。

寻址失败 / TriggerTurn 打 root → 返回 error 结果(turn 不失败,不静默丢弃)。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec


class PeerCoordinator(Protocol):
    """peer 投递协调器协议 —— 由 AgentEngine 实现(spawn_coordinator 同一对象)。"""

    async def deliver_peer_message(
        self,
        *,
        target: str,
        text: str,
        mode: str = "queue_only",
        from_thread_id: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        ...


def make_send_message_tool() -> ToolSpec:
    """构造 send_message ToolSpec —— LLM 向谱系内 peer(兄弟专家/parent)发消息。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 系统边界校验:target / text 必填字符串(禁静默占位)
        target = args.get("target")
        if not isinstance(target, str) or not target:
            return ToolResult.error(
                "missing_or_invalid_argument: target", reason="bad_args"
            )
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult.error(
                "missing_or_invalid_argument: text", reason="bad_args"
            )
        mode = args.get("mode", "queue_only")
        if mode not in ("queue_only", "trigger_turn"):
            return ToolResult.error(
                f"invalid_argument: mode {mode!r}", reason="bad_args"
            )
        coordinator: PeerCoordinator | None = ctx.extras.get("spawn_coordinator")
        if coordinator is None:
            return ToolResult.error("peer_unavailable", reason="config_error")
        # 发送者 = 当前 turn 的 thread(child 内调用即 child_thread_id)
        try:
            out = await coordinator.deliver_peer_message(
                target=target, text=text, mode=mode,
                from_thread_id=ctx.thread_id)
        except ValueError as e:
            # 未知目标 / TriggerTurn 打 root:显式 error 结果,turn 继续
            return ToolResult.error(str(e), reason="peer_delivery_rejected")
        return ToolResult.ok(json.dumps(out, ensure_ascii=False))

    return ToolSpec(
        name="send_message",
        description=(
            "向同一谱系内的另一个活体 agent(兄弟专家 / 协调者)点对点发消息。\n\n"
            "target 寻址:对方的 child_thread_id、spawn 句柄 handle_id,或特殊值 "
            "\"parent\"(上行给协调者)。\n"
            "mode:queue_only=入队/落史(对方下次采样可见,不打扰);"
            "trigger_turn=对方空闲时立即唤醒其新 turn 处理消息"
            "(对方正在跑则自动降级为 queue_only,mode_downgraded=true;"
            "root 协调者不可被唤醒)。\n\n"
            "返回 {target_thread_id, delivered_via, mode_downgraded, woken}。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "目标寻址:child_thread_id / handle_id / \"parent\""
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "消息正文(对方以 peer 来源的用户消息形态看到)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["queue_only", "trigger_turn"],
                    "description": "投递模式(默认 queue_only)",
                },
            },
            "required": ["target", "text"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 仅入队/落史/触发后台 task,不独占父 turn
    )
