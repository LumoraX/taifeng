"""SuspensionResolver —— 把 Resume.resolutions 配回 SuspensionRecord.pending。

产出 ResolvePlan 告诉 turn/engine 续跑时该做什么:执行哪些 tool call(permission allow)、
哪些 call_id 直接回填 output(form/data)、哪些 call_id 回填 error output(permission deny)、
是否重跑 sample(system_retry)、是否中止(system_retry abort)。
不允许部分 resume:resolutions 必须与 record.request_ids() 精确相等(见 spec §6)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from taifeng.suspend.reason import SuspendReason

if TYPE_CHECKING:
    from taifeng.suspend.record import SuspensionRecord


class ResolveError(Exception):
    """resolution 不合法(不全 / 多余 / 缺 related_call_id)。"""


@dataclass
class ResolvePlan:
    """续跑计划。"""

    execute_tool_call_ids: list[str] = field(default_factory=list)  # permission allow → 执行 tool
    direct_outputs: dict[str, Any] = field(default_factory=dict)  # call_id → output(form/data)
    deny_outputs: dict[str, str] = field(default_factory=dict)  # call_id → deny reason(permission)
    resample: bool = False  # system_retry → 重跑 sample
    abort: bool = False  # system_retry action=abort


class SuspensionResolver:
    """挂起配对器(无状态)。"""

    def validate(self, record: SuspensionRecord, resolutions: dict[str, Any]) -> None:
        """校验 resolutions 与 record 精确匹配;不匹配抛 ResolveError(禁部分 resume)。"""
        want = record.request_ids()
        got = set(resolutions.keys())
        if got != want:
            missing = want - got
            extra = got - want
            raise ResolveError(
                f"incomplete_or_extra_resolutions: missing={sorted(missing)} extra={sorted(extra)}"
            )

    def plan(self, record: SuspensionRecord, resolutions: dict[str, Any]) -> ResolvePlan:
        """校验后产出续跑计划。

        Raises:
            ResolveError: resolutions 不匹配,或人类输入类 pending 缺 related_call_id。
        """
        self.validate(record, resolutions)
        plan = ResolvePlan()
        for p in record.pending:
            payload = resolutions[p.request_id]
            if p.reason is SuspendReason.PERMISSION:
                if bool(payload.get("granted")):
                    if p.related_call_id is None:
                        raise ResolveError(
                            f"permission pending missing related_call_id: {p.request_id}"
                        )
                    plan.execute_tool_call_ids.append(p.related_call_id)
                else:
                    plan.deny_outputs[p.related_call_id or p.request_id] = str(
                        payload.get("reason", "denied by user")
                    )
            elif p.reason in (SuspendReason.FORM, SuspendReason.DATA):
                if p.related_call_id is None:
                    raise ResolveError(
                        f"form/data pending missing related_call_id: {p.request_id}"
                    )
                plan.direct_outputs[p.related_call_id] = payload
            elif p.reason is SuspendReason.SYSTEM_RETRY:
                if payload.get("action") == "abort":
                    plan.abort = True
                else:
                    plan.resample = True
            else:
                # 未知 reason:禁静默丢弃(CLAUDE.md 禁 silent fallback)
                raise ResolveError(f"unhandled_suspend_reason: {p.reason}")
        return plan
