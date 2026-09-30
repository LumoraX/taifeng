"""hook 与权限裁决的 durable 记录（session-journal，ADR 0096）。

hook 与权限策略是业务注入的裁决点：它们能拒绝一次调用、改写参数或输出。审计模式下每一次裁决
都是事实，且必须先于它所约束的动作落账：

```text
hook_evaluated        一个 hook handler 对一次事件给出的结论（放行 / 拒绝 / 改写）
permission_decided    权限策略对一次请求给出的结论
```
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from taifeng.conversation.journal.models import NonEmptyStr  # noqa: TC001  # Pydantic 运行期需要
from taifeng.conversation.journal.records import CanonicalMapping, PayloadModel

HOOK_EVALUATED_RECORD_TYPE = "hook_evaluated"
PERMISSION_DECIDED_RECORD_TYPE = "permission_decided"

HookKindV1 = Literal[
    "pre_tool_use", "post_tool_use", "pre_compact", "pre_turn", "post_turn",
    "pre_skill_dispatch", "post_skill_dispatch", "pre_script_use", "post_script_use",
    "outbound_message",
]

HOOK_OVERRIDE_KEYS = ("args_override", "output_override", "text_override")
"""内核会据以改写执行的 hook metadata 键；只有这些键的值进 Journal。"""


class HookEvaluatedV1(PayloadModel):
    """一个 hook handler 的一次裁决。

    Attributes:
        hook_kind: hook 类型。
        handler_index: 该类型下第几个 handler（注册顺序，从 0 起）。
        allow: 是否放行。
        reason: 拒绝理由；放行时为 None。
        subject: 这次裁决针对的对象（工具调用的 ``call_id`` / ``tool_name``、派发目标等）。
        overrides: handler 要求的改写，键取自 ``HOOK_OVERRIDE_KEYS``。
        metadata_keys: handler 给出的其余 metadata 键名（只记键名，值由业务自行留存）。
        error_class: handler 抛出异常时的异常类名；正常返回为 None。
    """

    hook_kind: HookKindV1
    handler_index: int = Field(ge=0)
    allow: bool
    reason: str | None = None
    subject: CanonicalMapping = Field(default_factory=dict)
    overrides: CanonicalMapping = Field(default_factory=dict)
    metadata_keys: tuple[str, ...] = ()
    error_class: NonEmptyStr | None = None


class PermissionDecidedV1(PayloadModel):
    """权限策略对一次请求的裁决。

    Attributes:
        scope: 权限范围（``shell_exec`` / ``file_write`` / ``skill_dispatch`` …）。
        target: 请求的对象（命令、路径、skill id …）。
        request_reason: 请求方（模型）自陈的理由。
        call_id: 所属工具调用；不属于某次调用时为 None。
        call_chain: skill 调用栈，最深的在最后。
        request_metadata: 请求携带的上下文（工具参数、业务透传内容）。
        granted: 是否放行。
        mode: 裁决方式。
        decision_reason: 策略给出的依据（命中的规则、授权 id、审批人的说明）。
        remember_until: 审批人声明的记忆范围；未声明为 None。
        minted_grant: 随这次裁决签发的可复用授权的匹配条件；没有签发为 None。
    """

    scope: NonEmptyStr
    target: str
    request_reason: str = ""
    call_id: NonEmptyStr | None = None
    call_chain: tuple[str, ...] = ()
    request_metadata: CanonicalMapping = Field(default_factory=dict)
    granted: bool
    mode: Literal["allow", "deny", "ask"]
    decision_reason: str = ""
    remember_until: Literal["once", "session", "always"] | None = None
    minted_grant: CanonicalMapping | None = None


__all__ = [
    "HOOK_EVALUATED_RECORD_TYPE",
    "HOOK_OVERRIDE_KEYS",
    "PERMISSION_DECIDED_RECORD_TYPE",
    "HookEvaluatedV1",
    "HookKindV1",
    "PermissionDecidedV1",
]
