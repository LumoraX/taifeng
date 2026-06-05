"""Submission / Op —— 业务侧入队的用户意图。

参照：codex codex-rs/codex-protocol/src/protocol.rs::Op
"""

from __future__ import annotations

import secrets
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


def _gen_sub_id() -> str:
    return f"sub_{secrets.token_hex(6)}"


class UserMessage(BaseModel):
    kind: Literal["user_message"] = "user_message"
    text: str
    attachments: list[dict] = Field(default_factory=list)


class CancelTurn(BaseModel):
    """中止指定 submission 的 turn。

    别名：Interrupt（codex 风格）。
    """

    kind: Literal["cancel_turn"] = "cancel_turn"
    submission_id: str


# 别名：codex 用 Interrupt
Interrupt = CancelTurn


class CompactNow(BaseModel):
    """业务侧主动触发压缩。

    参数：
        - ``target_tokens``: 压缩后目标 token 数（None = 用 budget.soft_limit）
        - ``preserve_tail``: 保留尾部消息数（None = 用 budget.preserve_tail_messages）
        - ``strategy``: 指定策略名（None = 让 orchestrator 自选）
        - ``force``: 即使未达阈值也强制执行
    """

    kind: Literal["compact_now"] = "compact_now"
    target_tokens: int | None = None
    preserve_tail: int | None = None
    strategy: str | None = None
    force: bool = True
    """业务主动触发默认 force=True；自动触发的内部 path 用 False。"""


class InjectSystemMessage(BaseModel):
    kind: Literal["inject_system"] = "inject_system"
    text: str
    source: str = "business"


class ThreadRollback(BaseModel):
    """回滚最近 N 轮对话。

    一"轮" = 一个 user_message 起到下一个 user_message 之前的所有 items。
    回滚会：
        1. 从 history_buffer 截掉最后 N 轮的 items
        2. 在 store 里追加一个 system_injection 标记 rollback 事件
        3. 重置 cache_anchor_index 到截断点
        4. cache 失效 expected（不计入 unexpected_breaks）
    """

    kind: Literal["thread_rollback"] = "thread_rollback"
    num_turns: int = 1


class UpdateBudget(BaseModel):
    """运行时调整 ContextBudget。

    None 字段表示保持原值。
    """

    kind: Literal["update_budget"] = "update_budget"
    context_window: int | None = None
    soft_limit_ratio: float | None = None
    hard_limit_ratio: float | None = None
    preserve_tail_messages: int | None = None


class RefreshSnapshot(BaseModel):
    """业务侧通知 engine 拉取最新 SkillSnapshot（用于热更后生效）。"""

    kind: Literal["refresh_snapshot"] = "refresh_snapshot"


class UpdateInstructions(BaseModel):
    """运行时热更指令 layer —— 业务侧通过此 Op 替换某 layer 的 source。

    被消费后：
        1. 替换内部 layers 中 name==layer_name 的条目
        2. 该 layer 缓存失效（下次 resolve 强制 fetch）
        3. 发 EventMsg ``instruction_updated``
        4. 未知 name 拒绝并发 ``instruction_update_rejected``

    实现：``engine.py::_handle_update_instructions``
    （已落地，见 spec instructions-injection）。
    """

    kind: Literal["update_instructions"] = "update_instructions"
    layer_name: str
    new_source: Any
    """``str`` 静态文本 或 ``InstructionSource`` 实例。"""

    model_config = {"arbitrary_types_allowed": True}


class Resume(BaseModel):
    """续跑一个挂起的 thread。

    Attributes:
        thread_id: 要续跑的 thread。
        resolutions: {request_id: payload}；必须一次补齐该挂起 record 的全部 pending
            （不允许部分 resume）。payload 形状由对应 PendingRequest.reason 决定：
            - permission: {"granted": bool, "reason"?: str, "remember_until"?: str}
            - form / data: 任意 JSON（直接成 function_call_output）
            - system_retry: {"action": "retry" | "abort"}
    """

    kind: Literal["resume"] = "resume"
    thread_id: str
    resolutions: dict[str, Any]


class Rewind(BaseModel):
    """回退到 turn 内任一回访节点并主动重推(turn-rewind 能力)。

    节点由 ``node_id`` 指向 root turn 录下的 ``RewindCheckpoint``;``kind`` 决定截点,
    ``mode`` 决定语义(见设计 spec 2026-06-05-addressable-dispatch-rewind):

    - ``re_reason``(默认):截到节点采样前 → 重采样,LLM 重新决定下游。
      turn_root / iteration / dispatch 三类节点都支持。
    - ``retry_tool``:仅 dispatch 节点。保留 assistant「决定调它」的 function_call,
      只用 ``new_args``(或原 args)重跑该工具/子 skill、替换其 output;若 ``new_args``
      改了入参,同步改写 function_call.arguments 保持历史自洽。

    ``new_args`` 仅 ``retry_tool`` + dispatch 节点有意义,其余模式忽略。
    """

    kind: Literal["rewind"] = "rewind"
    node_id: str
    mode: Literal["retry_tool", "re_reason"] = "re_reason"
    new_args: dict[str, Any] | None = None


class Shutdown(BaseModel):
    kind: Literal["shutdown"] = "shutdown"


Op = Union[
    UserMessage,
    CancelTurn,
    CompactNow,
    InjectSystemMessage,
    ThreadRollback,
    UpdateBudget,
    RefreshSnapshot,
    UpdateInstructions,
    Resume,
    Rewind,
    Shutdown,
]


class Submission(BaseModel):
    id: str = Field(default_factory=_gen_sub_id)
    op: Op = Field(discriminator="kind")
