"""request_user_input —— 采集型 HITL 触发工具。

参照 openspec change hitl-user-input-suspend-resume 的工具定义。差异:不引入独立
provide_tool_result Op,而是统一走 SuspendSignal(reason=DATA)→ TurnSuspended →
Resume({request_id: payload}),payload 经 resolver 回填成本次 call 的
function_call_output。

LLM 调用本工具向人类发问;response_schema 为不透明 passthrough(R1,内核不解析)。
本工具 parallel_safe=False。被调用时不同步返回 result,而是抛 SuspendSignal 让
turn 挂起。
"""

from __future__ import annotations

from typing import Any

from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.signal import SuspendSignal
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec


def make_request_user_input_tool() -> ToolSpec:
    """构造 request_user_input ToolSpec(注册进内置工具表)。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 边界校验:prompt 必填非空(系统边界,禁静默占位)
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                "request_user_input_empty_prompt: prompt 必须为非空字符串",
            )
        # response_schema 不透明透传;缺省视作空 schema(无结构约束)
        response_schema = args.get("response_schema") or {}
        # 不同步返回 → 抛挂起信号;related_call_id 锚定本次 call,
        # resume 时该 request_id 的 payload 回填成本次 call 的 function_call_output
        raise SuspendSignal(
            PendingRequest(
                request_id=ctx.call_id,  # form/data 场景 request_id 直接用 call_id
                reason=SuspendReason.DATA,
                payload_schema=response_schema,  # 不透明透传
                related_call_id=ctx.call_id,
                detail={"prompt": prompt, "response_schema": response_schema},
            ),
        )

    return ToolSpec(
        name="request_user_input",
        description=(
            "向人类发起一个结构化问询并等待回答。调用本工具会挂起当前 turn 直到收到回答。"
            "应作为该 step 唯一的工具调用发起。参数:prompt(问询文本)、"
            "response_schema(回答的 JSON Schema)。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "向人类展示的问询文本",
                },
                "response_schema": {
                    "type": "object",
                    "description": "期望回答的 JSON Schema(不透明)",
                },
            },
            "required": ["prompt"],
        },
        handler=handler,
        parallel_safe=False,
    )
