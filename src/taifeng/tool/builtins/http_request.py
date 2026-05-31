"""http_request —— 受 PermissionPolicy 约束的 HTTP 调用工具。

设计要点：
    - 复用仓库已有 ``httpx>=0.27``，零新依赖
    - PermissionScope = ``"network"``（早已定义但本 change 前无 builtin 使用）
    - **强制要求 PermissionPolicy**：``policy is None`` 立即拒绝（保守，与 shell_exec 同）
    - 4xx / 5xx 不算 ToolResult.error，让 LLM 自行解读 status code
    - ``parallel_safe=False``（单一 ToolSpec 同时承载读写方法，保守序列化）
    - R4 取消：handler 入口 ``ctx.cancel.raise_if_cancelled()`` + ``httpx.Timeout``

参照：hermes-gap-roadmap.md P0；对标 hermes httpx-based / codex reqwest-based。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from taifeng.permission.types import PermissionPolicy, PermissionRequest
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


_DEFAULT_ALLOWED_METHODS: tuple[str, ...] = (
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
)


HTTP_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["url"],
    "properties": {
        "url": {
            "type": "string",
            "description": "完整 URL，必须含 scheme（http 或 https）",
        },
        "method": {
            "type": "string",
            "enum": list(_DEFAULT_ALLOWED_METHODS),
            "default": "GET",
            "description": "HTTP 方法；缺省 GET",
        },
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "请求 header；空 dict 等价不传",
        },
        "body": {
            "description": (
                "请求体：string 直传；dict/list 自动以 JSON 序列化"
                "（自动设置 content-type=application/json）"
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "description": "覆盖工厂默认 timeout（必须 ≤ 工厂上限）",
        },
    },
}


def _validate_url(url: Any) -> str | None:
    """校验 URL 是否合法 http/https。返回拒绝原因，None 表示通过。"""
    if not isinstance(url, str) or not url:
        return "url required and must be string"
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"invalid url: {url!r}"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme: {parsed.scheme!r} (only http/https)"
    if not parsed.netloc:
        return "url missing host"
    return None


def make_http_request_tool(
    *,
    policy: PermissionPolicy | None = None,
    timeout_seconds: float = 30.0,
    max_response_bytes: int = 1024 * 1024,
    max_redirects: int = 5,
    allowed_methods: tuple[str, ...] = _DEFAULT_ALLOWED_METHODS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolSpec:
    """HTTP 请求工具。

    Args:
        policy: PermissionPolicy；**必须提供**，None 时 handler 立即拒绝
        timeout_seconds: 单次请求超时上限（LLM 可在 args.timeout_seconds 内更短覆盖）
        max_response_bytes: 响应 body 截断上限（默认 1MB）
        max_redirects: 重定向跳数上限
        allowed_methods: 允许的 HTTP 方法白名单
        transport: 可选 httpx transport（**仅测试用**，业务侧不传）

    Returns:
        ToolSpec(name='http_request', parallel_safe=False, ...)
    """

    # 大写归一化，方便后续 args.method 比较
    upper_allowed = tuple(m.upper() for m in allowed_methods)
    factory_timeout = timeout_seconds

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # ---- step 1: 入参校验 ----
        url = args.get("url")
        err = _validate_url(url)
        if err is not None:
            return ToolResult.error(f"bad_args: {err}", reason="bad_args")

        method_raw = args.get("method", "GET")
        if not isinstance(method_raw, str):
            return ToolResult.error("bad_args: method must be string", reason="bad_args")
        method = method_raw.upper()
        if method not in upper_allowed:
            return ToolResult.error(
                f"bad_args: method {method!r} not in allowed_methods {upper_allowed}",
                reason="bad_args",
            )

        headers = args.get("headers") or {}
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            return ToolResult.error(
                "bad_args: headers must be dict[str, str]", reason="bad_args"
            )

        body = args.get("body")  # None / str / dict / list 皆可

        req_timeout_raw = args.get("timeout_seconds")
        if req_timeout_raw is not None:
            if not isinstance(req_timeout_raw, (int, float)) or req_timeout_raw <= 0:
                return ToolResult.error(
                    "bad_args: timeout_seconds must be positive number",
                    reason="bad_args",
                )
            if float(req_timeout_raw) > factory_timeout:
                return ToolResult.error(
                    "bad_args: timeout_seconds "
                    f"{req_timeout_raw} > factory limit {factory_timeout}",
                    reason="bad_args",
                )
            effective_timeout = float(req_timeout_raw)
        else:
            effective_timeout = factory_timeout

        # ---- step 2: R4 取消检查 ----
        ctx.cancel.raise_if_cancelled()

        # ---- step 3: PermissionPolicy ----
        if policy is None:
            return ToolResult.error(
                "http_request requires PermissionPolicy (refuse by default)",
                reason="no_policy",
            )

        req = PermissionRequest(
            scope="network",
            target=f"{method} {url}",
            reason="LLM 请求 HTTP 调用",
            metadata={
                "thread_id": ctx.thread_id,
                "call_id": ctx.call_id,
                "method": method,
                "url": url,
            },
        )
        decision = await policy.check(req)
        if not decision.granted:
            return ToolResult.error(
                f"permission_denied: {decision.reason}",
                reason="permission_denied",
            )

        # ---- step 4: 执行 ----
        # body 序列化策略：dict/list 走 json=（自动 content-type）；
        # string 走 content=（按需用户自填 content-type）；None 不传 body
        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers if headers else None,
        }
        if body is not None:
            if isinstance(body, (dict, list)):
                request_kwargs["json"] = body
            elif isinstance(body, str):
                request_kwargs["content"] = body.encode("utf-8")
            else:
                return ToolResult.error(
                    f"bad_args: body must be str / dict / list, got {type(body).__name__}",
                    reason="bad_args",
                )

        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(effective_timeout),
            "follow_redirects": True,
            "max_redirects": max_redirects,
        }
        if transport is not None:
            client_kwargs["transport"] = transport

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.request(**request_kwargs)
        except httpx.TooManyRedirects as e:
            return ToolResult.error(
                f"redirect_limit: {e}", reason="redirect_limit"
            )
        except httpx.TimeoutException as e:
            return ToolResult.error(
                f"timeout after {effective_timeout}s: {e}", reason="timeout"
            )
        except httpx.ConnectError as e:
            return ToolResult.error(f"connect_error: {e}", reason="connect_error")
        except httpx.RequestError as e:
            # 涵盖 ReadError / WriteError / NetworkError 等
            return ToolResult.error(f"connect_error: {e}", reason="connect_error")
        except Exception as e:  # noqa: BLE001 — 兜底归类
            logger.exception("http_request unknown error: %s %s", method, url)
            return ToolResult.error(f"unknown: {e}", reason="unknown")

        # ---- step 5: 响应序列化 + 截断 ----
        body_bytes = resp.content  # httpx 已读完字节
        bytes_in = len(body_bytes)
        truncated = bytes_in > max_response_bytes
        if truncated:
            body_text = body_bytes[:max_response_bytes].decode("utf-8", errors="replace")
        else:
            body_text = body_bytes.decode("utf-8", errors="replace")

        # headers 全部小写化（HTTP/1.1 header 名 case-insensitive；便于 LLM 取用）
        headers_out = {k.lower(): v for k, v in resp.headers.items()}

        payload = {
            "status": resp.status_code,
            "headers": headers_out,
            "body": body_text,
            "truncated": truncated,
            "url_final": str(resp.url),
        }
        # ---- step 6: 4xx/5xx 不算 ToolResult.error；让 LLM 自行判断 ----
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            status_code=resp.status_code,
            bytes_in=bytes_in,
            truncated=truncated,
            method=method,
            url_final=str(resp.url),
        )

    return ToolSpec(
        name="http_request",
        description=(
            "发起 HTTP 请求（GET/POST/PUT/PATCH/DELETE/HEAD）。"
            "受业务侧 PermissionPolicy(scope='network') 审批；"
            "响应 body 默认截断到 1MB；4xx/5xx 不视为工具错误，由 LLM 自行解读 status code。"
        ),
        input_schema=HTTP_REQUEST_SCHEMA,
        handler=handler,
        parallel_safe=False,
        timeout_seconds=timeout_seconds,
    )


__all__ = ["HTTP_REQUEST_SCHEMA", "make_http_request_tool"]
