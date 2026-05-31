"""McpPrompter —— 用 MCP ``elicitation/create`` 实现 PermissionPrompter。

参照：
    - codex codex-rs/mcp-server/src/exec_approval.rs（elicitation 路由）
    - MCP 协议 spec 2024-11-05 / elicitation/create

设计要点（R1 业务零侵入）：
    - McpPrompter 实现 PermissionPrompter 协议；业务侧把它注入 PermissionPolicy
    - 不引入业务术语；所有透传字段（call_chain / 业务自定义 metadata）由 PermissionRequest 携带
    - timeout / error / cancel 全转为 PermissionDecision.deny（保守失败）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from taifeng.permission.types import PermissionDecision, PermissionRequest

if TYPE_CHECKING:
    from taifeng.mcp.server import McpStdioServer

logger = logging.getLogger(__name__)


# elicitation 默认超时（秒）
DEFAULT_ELICITATION_TIMEOUT_SECONDS = 60.0


class McpPrompter:
    """PermissionPrompter 的 MCP elicitation/create 实现。

    业务侧用法：

        server = McpStdioServer(pool)
        prompter = McpPrompter(server, timeout_seconds=120.0)
        policy = PermissionPolicy(prompter=prompter, default_mode="ask")
        pool = await EnginePool.create(..., permission_policy=policy)
        await server.run()

    elicitation/create 协议形态（MCP 2024-11-05）：

        request → server 发：
            {"method": "elicitation/create",
             "params": {"message": "<prompt>",
                        "requestedSchema": {"type": "object", ...}}}
        response ← client 回：
            {"result": {"action": "accept"|"reject"|"cancel",
                        "content": {...}}}
    """

    def __init__(
        self,
        server: McpStdioServer,
        *,
        timeout_seconds: float = DEFAULT_ELICITATION_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            server: 已构造（未必已 run）的 McpStdioServer；prompter 通过其
                ``server_initiated_request`` 发起 elicitation
            timeout_seconds: 等待 client 响应秒数；超时返回 deny
        """
        self._server = server
        self._timeout = float(timeout_seconds)

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        """实现 PermissionPrompter 协议。

        失败语义（全保守 deny）：
            - timeout → deny(reason='elicitation_timeout')
            - server-initiated error → deny(reason='elicitation_error:<type>')
            - client action != accept → deny(reason='user_<action>')
            - accept + content.approved=false → deny(reason=content.reason
              or 'user_denied')
        """
        params = self._build_params(request)
        try:
            from taifeng.mcp.server import McpServerInitiatedRequestError

            response = await self._server.server_initiated_request(
                "elicitation/create",
                params,
                timeout=self._timeout,
            )
        except TimeoutError:
            return PermissionDecision.deny(reason="elicitation_timeout")
        except McpServerInitiatedRequestError as e:
            logger.warning(
                "elicitation rejected by client: code=%s msg=%s",
                e.code, e.message,
            )
            return PermissionDecision.deny(
                reason=f"elicitation_error:{e.code}",
            )
        except Exception as e:
            logger.exception("elicitation failed unexpectedly")
            return PermissionDecision.deny(
                reason=f"elicitation_error:{type(e).__name__}",
            )

        return self._parse_response(response)

    # ------------------------------------------------------------------
    # Internal: params 构造 + response 解析
    # ------------------------------------------------------------------

    def _build_params(self, request: PermissionRequest) -> dict[str, Any]:
        """把 PermissionRequest 包成 elicitation/create params。"""
        chain_str = (
            " → ".join(request.call_chain)
            if request.call_chain else "(entry)"
        )
        # 展示 metadata 关键字段，让 host UI 看到具体参数（如 args.command）
        args_preview = self._render_args_preview(request.metadata)
        message_lines = [
            "Approve operation?",
            f"  scope:  {request.scope}",
            f"  target: {request.target}",
        ]
        if args_preview:
            message_lines.append(f"  args:   {args_preview}")
        message_lines.append(f"  chain:  {chain_str}")
        message_lines.append(f"  reason: {request.reason or '(none)'}")
        message = "\n".join(message_lines)
        return {
            "message": message,
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "approved": {
                        "type": "boolean",
                        "description": "Allow this operation?",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for decision",
                    },
                },
                "required": ["approved"],
            },
        }

    @staticmethod
    def _render_args_preview(metadata: dict[str, Any]) -> str:
        """metadata 内 args / args dict 转人类可读 preview（限 200 字符）。"""
        import json as _json

        # for_tool_call / for_script_exec 把 args 塞到 metadata["args"]
        args = metadata.get("args")
        if args is None:
            return ""
        try:
            text = _json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(args)
        return text[:200]

    def _parse_response(self, response: dict[str, Any]) -> PermissionDecision:
        """把 elicitation response 转 PermissionDecision。"""
        action = response.get("action")
        content = response.get("content") or {}

        if action == "accept":
            approved = bool(content.get("approved", False))
            reason_str = str(content.get("reason") or "")
            if approved:
                return PermissionDecision.allow(
                    reason=reason_str or "user_approved",
                    remember="once",
                )
            return PermissionDecision.deny(
                reason=reason_str or "user_denied",
            )
        if action == "reject":
            return PermissionDecision.deny(reason="user_rejected")
        if action == "cancel":
            return PermissionDecision.deny(reason="user_cancelled")

        return PermissionDecision.deny(
            reason=f"elicitation_unknown_action:{action!r}",
        )
