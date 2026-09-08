"""turn 的模块级纯函数 helper：摘要哈希 / 孤儿 call_id / 末条用户文本 / 失败上下文 /
Responses 采样 id 与会话条目。

从 ``turn.py`` 原样下沉（Wave 4 模块切分，行为零变化）。独立成模块的直接原因是
**打破循环 import**：`turn_tooling` 等协作器要用这些 helper，而它们原本定义在
`turn.py` 里，协作器反向 import `turn` 会成环。这些函数无状态、不依赖 TurnRunner，
本就该在模块层。
"""

from __future__ import annotations

import hashlib
from typing import Any

from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
)
from taifeng.llm.errors import InvalidResponseError
from taifeng.llm.responses_types import (
    NormalizedFunctionCallItem,
    NormalizedMessageItem,
    NormalizedOutputItem,
    NormalizedReasoningItem,
)
from taifeng.loop.failure_policy import FailureContext


def _sha1_short(text: str) -> str:
    """短 sha1（16 hex）—— 用于 prompt 结构指纹，仅做相等性比较。"""
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _history_orphan_call_ids(history: list[ResponseItem]) -> set[str]:
    """返回未配对的 function_call / function_call_output 的 call_id 集合（孤儿）。

    function_call 与 function_call_output 应一一对应（同 call_id）。对称差即
    未配对项；compacted 摘要吃掉成对段属正常（两侧同时消失，不算孤儿）。
    """
    fc = {
        h.payload.get("call_id") for h in history if h.kind == "function_call"
    }
    fco = {
        h.payload.get("call_id")
        for h in history
        if h.kind == "function_call_output"
    }
    return {cid for cid in (fc ^ fco) if cid is not None}


def _latest_user_text(history: list[ResponseItem]) -> str:
    """取 history 中最近一条 user_message 的文本（无则空串）。

    用途：① 长期记忆 prefetch 的默认检索 query；② 注入 ToolContext 的 ``current_task``，
    供 ``search_skills`` 把「原始任务」（含输入上下文）喂给验证门——验证要判「当前任务给
    的输入是否满足该 skill 的输入要求」，必须看到用户原话，不能用为词面匹配优化、剥离了
    输入上下文的关键词 query（详情五根因）。

    Args:
        history: 当前 in-memory 历史视图。

    Returns:
        最近一条用户消息的文本；history 中无 user_message 时返回空串。
    """
    for it in reversed(history):
        if it.kind == "user_message":
            return str(it.payload.get("text", ""))
    return ""


def _llm_failure_context(
    err: Exception, *, is_root: bool, iteration: int
) -> FailureContext:
    """从采样阶段的 LLMError 构造失败裁决上下文(origin=llm_error)。

    原 ``_should_suspend_on_error`` 的硬编码判据已收编进
    :class:`~taifeng.loop.failure_policy.ConservativeFailurePolicy`(内核默认,
    零行为变化);turn 层只负责构造上下文,裁决交注入的 policy。

    Args:
        err: 采样阶段重试耗尽后的异常(调用方保证为 LLMError)。
        is_root: 当前 runner 是否 root turn。
        iteration: 失败发生时的迭代序号(1-based)。

    Returns:
        供 FailureDispositionPolicy.decide 的不可变上下文。
    """
    return FailureContext(
        origin="llm_error",
        failure_class=getattr(err, "failure_class", None),
        end_reason=None,
        error_kind=type(err).__name__,
        retryable=bool(getattr(err, "retryable", False)),
        is_root=is_root,
        iteration=iteration,
    )


def _responses_sample_id(
    *, thread_id: str, submission_id: str, turn_index: int, iteration: int
) -> str:
    """构造跨热/冷恢复稳定的 logical Responses sample id。"""
    return f"{thread_id}:{submission_id}:turn:{turn_index}:llm:{iteration}"


def _responses_conversation_items(
    raw_items: list[dict[str, Any]],
    *,
    thread_id: str,
    model: str,
    sample_id: str,
) -> tuple[list[ResponseItem], str, list[dict[str, Any]]]:
    """把已验证 terminal normalized items 投影为 durable conversation items。"""
    from pydantic import TypeAdapter

    normalized = TypeAdapter(list[NormalizedOutputItem]).validate_python(raw_items)
    response_items: list[ResponseItem] = []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in normalized:
        metadata = {
            "llm_sample_id": sample_id,
            "provider_output_index": item.output_index,
        }
        if isinstance(item, NormalizedReasoningItem):
            payload: dict[str, Any] = {"text": "", "summary": item.visible_text}
            if item.state is not None:
                payload["provider_state"] = item.state.model_dump(mode="json")
            response_items.append(
                ResponseItem(
                    kind="reasoning",
                    thread_id=thread_id,
                    payload=payload,
                    metadata=metadata,
                )
            )
        elif isinstance(item, NormalizedMessageItem):
            text_parts.append(item.text)
            response_items.append(
                assistant_message(item.text, thread_id=thread_id, model=model).model_copy(
                    update={"metadata": metadata}
                )
            )
        elif isinstance(item, NormalizedFunctionCallItem):
            response_items.append(
                function_call(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=item.arguments,
                    thread_id=thread_id,
                ).model_copy(update={"metadata": metadata})
            )
            tool_calls.append(
                {
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments": item.arguments,
                    "origin_sample_id": sample_id,
                }
            )
    if not response_items:
        raise InvalidResponseError("Responses normalized output is empty")
    return response_items, "".join(text_parts), tool_calls
