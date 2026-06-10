"""挂起原因分类 + 单个待办请求的数据契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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


@dataclass(frozen=True)
class PendingRequest:
    """一个挂起点的待办请求。

    Attributes:
        request_id: 关联 id(对标 codex call_id);Resume.resolutions 的 key。
        reason: 挂起原因分类。
        payload_schema: JSON Schema —— 业务/前端据此渲染表单或审批 UI。
        related_call_id: 关联的 function_call call_id;人类输入类必有,系统态为 None。
        detail: 不透明上下文(scope/target/command/failure_class 等);taifeng 不解析(R1)。
    """

    request_id: str
    reason: SuspendReason
    payload_schema: dict[str, Any] = field(default_factory=dict)
    related_call_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
