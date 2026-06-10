"""挂起原因分类 + 单个待办请求的数据契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class SuspendReason(StrEnum):
    """挂起原因 —— 决定 resume 时的续跑语义。"""

    PERMISSION = "permission"      # 等权限审批 → decision 回填 gate 结果
    FORM = "form"                  # 等用户填表 → payload 成 tool output
    DATA = "data"                  # 等外部数据 → payload 成 tool output
    SYSTEM_RETRY = "system_retry"  # 限流/余额/key/LLM 错 → resume 即重试同次 sample
    # 资源护栏触顶(max_iterations / resource_limit / denial_circuit_open)被
    # FailureDispositionPolicy 裁决为挂起 → resume retry 即重建 runner 在迭代
    # 边界继续采样循环(与 SYSTEM_RETRY 的"重跑同次 sample"不同:无悬空 fc、不
    # resample);abort 即在挂起点落失败终态。detail 携带 end_reason + 护栏快照。
    RESOURCE_LIMIT = "resource_limit"
    # call_skill 派发的子 skill 内部挂起 → 父 turn 的 call_skill 随之挂起。
    # 这是纯内核派发态（非用户可直接 resolve）：resume 时由 engine 续跑链
    # 内部核销 —— 先续跑子 thread 拿到结果，再回填父 call_skill 的 output。
    # detail 携带 sub_thread_id（子 thread）；related_call_id = 父 call_skill 的 call_id。
    CHILD_SKILL = "child_skill"


# 到期可自动 retry 的 reason —— 其余(人类输入类 / 内核派发态)到期只能 abort:
# 无法替用户造数据,retry 无意义只会原地再挂(suspension-ttl 契约)。
_RETRY_ON_EXPIRE_REASONS = frozenset(
    {SuspendReason.SYSTEM_RETRY, SuspendReason.RESOURCE_LIMIT}
)


@dataclass(frozen=True)
class PendingRequest:
    """一个挂起点的待办请求。

    Attributes:
        request_id: 关联 id(对标 codex call_id);Resume.resolutions 的 key。
        reason: 挂起原因分类。
        payload_schema: JSON Schema —— 业务/前端据此渲染表单或审批 UI。
        related_call_id: 关联的 function_call call_id;人类输入类必有,系统态为 None。
        detail: 不透明上下文(scope/target/command/failure_class 等);taifeng 不解析(R1)。
        ttl_seconds: 挂起存活期(秒)。None(默认)= 永不过期;正整数 = 自 record
            created_at 起的过期秒数。**禁 ≤0(含 -1 哨兵)**——魔法值红线,构造期抛错。
        on_expire: 到期自动裁决动作。"abort"(默认)= 在挂起点终止;"retry" 仅
            SYSTEM_RETRY / RESOURCE_LIMIT 合法(等外部条件清除后自动续跑)。

    Raises:
        ValueError: ttl_seconds <= 0,或 on_expire="retry" 配在不可 retry 的 reason 上。
    """

    request_id: str
    reason: SuspendReason
    payload_schema: dict[str, Any] = field(default_factory=dict)
    related_call_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    ttl_seconds: int | None = None
    on_expire: Literal["abort", "retry"] = "abort"

    def __post_init__(self) -> None:
        """构造期校验 TTL 字段(禁 silent fallback,非法组合立即抛错)。"""
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError(
                f"ttl_seconds 必须为正整数或 None(永不过期),实得 {self.ttl_seconds};"
                "禁用 -1 等哨兵值"
            )
        if self.on_expire == "retry" and self.reason not in _RETRY_ON_EXPIRE_REASONS:
            raise ValueError(
                f"on_expire='retry' 仅 system_retry / resource_limit 合法,"
                f"reason={self.reason.value!r} 到期只能 abort(无法替用户造数据)"
            )
