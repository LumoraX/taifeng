"""SuspensionResolver —— 把 Resume.resolutions 配回 SuspensionRecord.pending。

产出 ResolvePlan 告诉 turn/engine 续跑时该做什么:执行哪些 tool call(permission allow)、
哪些 call_id 直接回填 output(form/data)、哪些 call_id 回填 error output(deny / 到期)、
是否中止(system_retry / resource_limit 的 abort)。
核销是 **request 级**(multi-pending-partial-resume):resolutions 可为 record
request_ids 的非空子集,仅裁决子集;空集 / 未知 request_id 仍显式拒绝。
整体 resolved-marker 与续跑由调用方(engine)在全部 pending 核销后落定。
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
    abort: bool = False  # system_retry / resource_limit 的 action=abort
    # 注:无 resample 位——SYSTEM_RETRY retry 的「重跑同次 sample」由挂起点 history
    # 形态天然保证(挂起时无失败轮 assistant 消息,重建续跑即重新采样),无需标志位


class SuspensionResolver:
    """挂起配对器(无状态)。"""

    def validate(self, record: SuspensionRecord, resolutions: dict[str, Any]) -> None:
        """校验 resolutions 是 record request_ids 的非空子集;违例抛 ResolveError。

        request 级核销(multi-pending-partial-resume):子集 = 仅裁决该子集,
        其余 pending 保持活跃(由 engine 判定全量达成后才落 marker / 续跑)。
        空集与未知 request_id 仍显式拒绝(禁静默)。
        """
        want = record.request_ids()
        got = set(resolutions.keys())
        if not got:
            raise ResolveError("empty_resolutions")
        extra = got - want
        if extra:
            raise ResolveError(f"unknown_request_ids: extra={sorted(extra)}")

    def plan(self, record: SuspensionRecord, resolutions: dict[str, Any]) -> ResolvePlan:
        """校验后产出续跑计划。

        Raises:
            ResolveError: resolutions 不匹配,或人类输入类 pending 缺 related_call_id。
        """
        self.validate(record, resolutions)
        plan = ResolvePlan()
        # request 级核销:只裁决 resolutions 覆盖到的 pending,其余保持活跃
        for p in record.pending:
            if p.request_id not in resolutions:
                continue
            payload = resolutions[p.request_id]
            # suspension-ttl:内核到期哨兵 → 按 pending 的 on_expire 裁决
            if _is_expire_payload(payload):
                self._apply_expiry(plan, p)
                continue
            # 结构化 payload 的 reason:非 dict 形态显式拒绝(禁 AttributeError 逃逸
            # 致 resume 任务静默崩溃——create_task 派发的异常无人消费)
            if (p.reason in (SuspendReason.PERMISSION, SuspendReason.SYSTEM_RETRY,
                             SuspendReason.RESOURCE_LIMIT)
                    and not isinstance(payload, dict)):
                raise ResolveError(
                    f"invalid_payload_shape: {p.request_id} (want dict, "
                    f"got {type(payload).__name__})"
                )
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
                # retry:无需置位——重建续跑天然重新采样
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
          (重建 runner 续跑即重新采样,无需标志位);abort → 终止。
        - 人类输入类(PERMISSION / FORM / DATA)与内核派发态(CHILD_SKILL):
          无法替用户造数据 → 悬空 fc 回填 "suspension_expired" error output
          (保配对完整,R5)+ abort 终止。构造期已禁 on_expire="retry"。
        - 混合 record:任一 abort 性 pending 即整体 abort(record 级一次性裁决)。

        Args:
            plan: 正在累积的续跑计划(就地修改)。
            p: 到期的 PendingRequest。
        """
        if p.reason in (SuspendReason.SYSTEM_RETRY, SuspendReason.RESOURCE_LIMIT):
            # retry:不 abort 即续跑(重建 runner 重新采样);abort:终止
            if p.on_expire != "retry":
                plan.abort = True
            return
        # 人类输入类 / 内核派发态:回填过期标记保 fc/fco 配对,整体终止。
        # 值为裁决缘由;渲染前缀由 engine 按 pending reason 决定(PERMISSION →
        # permission_denied,其余 → suspension_expired,避免数据问询超时被误读为权限拒绝)
        plan.deny_outputs[p.related_call_id or p.request_id] = "ttl_reached"
        plan.abort = True
