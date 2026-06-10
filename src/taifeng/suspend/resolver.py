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


# suspension-ttl:内核到期裁决的哨兵 payload key(engine 定时器签发,plan() 识别)。
# 形如 {"__expired__": True}。这样到期 auto-Resume 走公共 Resume Op,root / 嵌套 /
# spawn 三条续跑链零改动全复用。业务侧伪造它等价于自己提交 deny / abort,
# 无额外能力增益 —— 文档标注为内核内部形态,不列入公开 payload 契约。
EXPIRE_SENTINEL = "__expired__"


def _is_expire_payload(payload: Any) -> bool:
    """是否内核到期哨兵 payload({"__expired__": True})。"""
    return isinstance(payload, dict) and payload.get(EXPIRE_SENTINEL) is True


@dataclass
class ResolvePlan:
    """续跑计划。"""

    execute_tool_call_ids: list[str] = field(default_factory=list)  # permission allow → 执行 tool
    direct_outputs: dict[str, Any] = field(default_factory=dict)  # call_id → output(form/data)
    deny_outputs: dict[str, str] = field(default_factory=dict)  # call_id → deny reason(permission)
    resample: bool = False  # system_retry → 重跑 sample
    abort: bool = False  # system_retry / resource_limit 的 action=abort


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
            # suspension-ttl:内核到期哨兵 → 按 pending 的 on_expire 裁决
            if _is_expire_payload(payload):
                self._apply_expiry(plan, p)
                continue
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
            elif p.reason is SuspendReason.RESOURCE_LIMIT:
                # 护栏触顶挂起:retry = 重建 runner 在迭代边界继续采样循环,
                # **不置 resample**(挂起点无悬空 fc,无"同次 sample"可重跑);
                # abort 与 SYSTEM_RETRY 同语义(在挂起点落失败终态)。
                action = payload.get("action")
                if action == "abort":
                    plan.abort = True
                elif action != "retry":
                    # 非法 action:禁静默兜底,显式拒绝
                    raise ResolveError(
                        f"invalid_resource_limit_action: {action!r} (want retry|abort)"
                    )
            else:
                # 未知 reason:禁静默丢弃(CLAUDE.md 禁 silent fallback)
                raise ResolveError(f"unhandled_suspend_reason: {p.reason}")
        return plan

    @staticmethod
    def _apply_expiry(plan: ResolvePlan, p: Any) -> None:
        """把单个 pending 的到期裁决并入 plan(suspension-ttl)。

        - SYSTEM_RETRY / RESOURCE_LIMIT:按 on_expire——retry → 自动续跑
          (SYSTEM_RETRY 置 resample,RESOURCE_LIMIT 重建续跑无需置位);
          abort → 终止。
        - 人类输入类(PERMISSION / FORM / DATA)与内核派发态(CHILD_SKILL):
          无法替用户造数据 → 悬空 fc 回填 "suspension_expired" error output
          (保配对完整,R5)+ abort 终止。构造期已禁 on_expire="retry"。
        - 混合 record:任一 abort 性 pending 即整体 abort(record 级一次性裁决)。

        Args:
            plan: 正在累积的续跑计划(就地修改)。
            p: 到期的 PendingRequest。
        """
        if p.reason in (SuspendReason.SYSTEM_RETRY, SuspendReason.RESOURCE_LIMIT):
            if p.on_expire == "retry":
                if p.reason is SuspendReason.SYSTEM_RETRY:
                    plan.resample = True
                # RESOURCE_LIMIT retry:重建 runner 续跑即继续循环,无需置位
            else:
                plan.abort = True
            return
        # 人类输入类 / 内核派发态:回填过期标记保 fc/fco 配对,整体终止
        plan.deny_outputs[p.related_call_id or p.request_id] = "suspension_expired"
        plan.abort = True
