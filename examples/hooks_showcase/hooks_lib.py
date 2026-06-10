"""hooks_showcase 的钩子定义 —— 被 demo.py（SimClient）与 web_ui server.py（真实 LLM）共用。

要点：hook 是**进程内 async 回调**，可跑任意业务逻辑（查 DB / 改写 args / 审计），
与「声明式权限规则」（PermissionPolicy 的 Style A 配置）互补：

    - 权限规则：静态、可序列化、按 pattern 匹配 → 适合「这个 skill / 命令能不能用」。
    - 钩子：命令式、能读运行时 args 与调用栈 → 适合「同一个 skill，按本次入参动态决策」。

本模块演示两类 hook（均在 call_skill 派发链路上触发）：

    1. pre_skill_dispatch（可否决）：按 *运行时 args* 拦截高风险派发
       —— ``scope=all`` 的全量导出直接 deny，引擎 emit ``skill_dispatch_hook_denied``。
       这是权限规则难以表达的「按入参动态判定」。
    2. post_skill_dispatch（仅审计、不可否决）：记录每次完成的派发，形成审计闭环
       —— spec 强制：返回 deny 不会改 ToolResult，hook 自身异常也不影响主流程。
"""

from __future__ import annotations

import logging

from taifeng.hooks import (
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    PostSkillDispatchHook,
    PreSkillDispatchHook,
)

logger = logging.getLogger("hooks_showcase")

# 业务级风险规则：该子 skill 在「全量」入参下属高风险，需 pre 钩子按 args 拦截。
_HIGH_RISK_SKILL = "data-export"


async def _block_full_export(
    hook: PreSkillDispatchHook, ctx: HookContext
) -> HookDecision:
    """pre_skill_dispatch：按运行时 args 拦截高风险全量导出。

    permission 规则只能按 skill_id / pattern 静态匹配，读不到本次 call_skill 的
    入参；而钩子能拿到 ``hook.args``，可做「同一 skill 按入参分流」的动态决策。

    返回 ``HookDecision.deny`` → 引擎中止派发并 emit ``skill_dispatch_hook_denied``；
    后续 PermissionPolicy / 子 turn 都不会执行。
    """
    if hook.target_skill_id == _HIGH_RISK_SKILL and hook.args.get("scope") == "all":
        return HookDecision.deny(
            "全量导出（scope=all）属高风险操作，业务钩子按入参拦截；请改用 scope=recent。"
        )
    return HookDecision.ok()


async def _audit_dispatch(
    hook: PostSkillDispatchHook, ctx: HookContext
) -> HookDecision:
    """post_skill_dispatch：仅审计、不可否决（spec 强制）。

    无论子 turn 成功与否都会触发；返回值不影响 ToolResult，仅用于记日志 / 埋点。
    """
    logger.info(
        "[AUDIT] dispatch 完成: thread=%s target=%s success=%s duration_ms=%s",
        ctx.thread_id,
        hook.target_skill_id,
        hook.success,
        hook.duration_ms,
    )
    return HookDecision.ok()


def build_showcase_hook_runner() -> HookRunner:
    """构造 hooks_showcase 用的 HookRunner（注册上面两类业务钩子）。

    demo.py（SimClient）与 web_ui server.py（真实 LLM）共用本工厂，确保两处行为一致。
    """
    registry = HookRegistry()
    registry.register("pre_skill_dispatch", _block_full_export)
    registry.register("post_skill_dispatch", _audit_dispatch)
    return HookRunner(registry)
