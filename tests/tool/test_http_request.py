"""http_request 工具单测。

设计：
    - 全部走 ``httpx.MockTransport``，**0 真实网络**
    - 覆盖 spec ``tool-builtins-extended`` 的 http_request Requirement 全部 11 个 Scenario
"""

from __future__ import annotations

import json

import httpx
import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import (
    PermissionPolicy,
    PermissionRule,
)
from taifeng.tool.builtins.http_request import make_http_request_tool
from taifeng.tool.spec import ToolContext


def _make_ctx() -> ToolContext:
    """构造最小 ToolContext —— extras 留空，handler 不依赖其他注入。"""
    return ToolContext(
        call_id="tc-http-1",
        cancel=CancellationToken(),
        thread_id="t-1",
        extras={},
    )


def _allow_all_policy() -> PermissionPolicy:
    """默认放行的 policy —— 网络访问全过。"""
    return PermissionPolicy(rules=[], default_mode="allow")


def _deny_all_policy() -> PermissionPolicy:
    """显式拒绝所有 network 的 policy —— 用于断言"被拒绝时不发请求"。"""
    return PermissionPolicy(
        rules=[
            PermissionRule(
                scope="network",
                target_pattern="glob:*",
                mode="deny",
                reason="not_in_allowlist",
            ),
        ],
        default_mode="deny",
    )


# ====================================================================
# 1. 工厂层
# ====================================================================

def test_factory_default_parallel_safe_false() -> None:
    """ToolSpec.parallel_safe 必须 False（保守）；name 与 url required。"""
    t = make_http_request_tool(policy=None)
    assert t.name == "http_request"
    assert t.parallel_safe is False
    assert "url" in t.input_schema["required"]


# ====================================================================
# 2. policy 校验
# ====================================================================

async def test_no_policy_returns_no_policy_error() -> None:
    """policy=None → 立即拒绝，不发请求。"""
    t = make_http_request_tool(policy=None)
    r = await t.handler({"url": "https://example.com/"}, _make_ctx())
    assert r.is_error
    assert r.data["reason"] == "no_policy"


async def test_permission_denied_returns_error() -> None:
    """deny policy → permission_denied，且 mock transport 不被调用。"""
    sent = {"count": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        sent["count"] += 1
        return httpx.Response(200)

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(policy=_deny_all_policy(), transport=transport)
    r = await t.handler({"url": "https://leaky.example/"}, _make_ctx())
    assert r.is_error
    assert r.data["reason"] == "permission_denied"
    assert sent["count"] == 0


# ====================================================================
# 3. 成功路径
# ====================================================================

async def test_get_success_returns_json_body() -> None:
    """GET 成功 → output JSON 含 status/body，data 含 telemetry 字段。"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pong": True})

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(policy=_allow_all_policy(), transport=transport)
    r = await t.handler(
        {"url": "https://api.example.com/v1/ping"}, _make_ctx(),
    )
    assert not r.is_error
    payload = json.loads(r.output)
    assert payload["status"] == 200
    assert "pong" in payload["body"]
    assert payload["truncated"] is False
    assert r.data["status_code"] == 200
    assert r.data["truncated"] is False
    assert r.data["method"] == "GET"


async def test_post_json_body_serialized() -> None:
    """dict body → 自动 JSON 序列化 + content-type=application/json。"""
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(policy=_allow_all_policy(), transport=transport)
    r = await t.handler(
        {
            "url": "https://api.example.com/v1/echo",
            "method": "POST",
            "body": {"a": 1},
        },
        _make_ctx(),
    )
    assert not r.is_error
    assert captured["body"] == b'{"a":1}'
    ctype = captured["content_type"]
    assert isinstance(ctype, str) and "application/json" in ctype


# ====================================================================
# 4. 4xx / 5xx 不算 error
# ====================================================================

async def test_4xx_response_not_marked_as_error() -> None:
    """404 → ToolResult.ok（is_error=False）；让 LLM 自己解读。"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(policy=_allow_all_policy(), transport=transport)
    r = await t.handler({"url": "https://x.example/"}, _make_ctx())
    assert r.is_error is False
    payload = json.loads(r.output)
    assert payload["status"] == 404


async def test_5xx_response_not_marked_as_error() -> None:
    """503 → ToolResult.ok；LLM 可根据 status 决定是否重试。"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="oops")

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(policy=_allow_all_policy(), transport=transport)
    r = await t.handler({"url": "https://x.example/"}, _make_ctx())
    assert r.is_error is False
    payload = json.loads(r.output)
    assert payload["status"] == 503


# ====================================================================
# 5. body 截断
# ====================================================================

async def test_body_truncation_at_max_bytes() -> None:
    """超 max_response_bytes 截断；data.bytes_in 保留原始字节数。"""
    big = b"x" * 2048

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(
        policy=_allow_all_policy(),
        max_response_bytes=1024,
        transport=transport,
    )
    r = await t.handler({"url": "https://x.example/"}, _make_ctx())
    assert not r.is_error
    payload = json.loads(r.output)
    assert len(payload["body"]) == 1024
    assert payload["truncated"] is True
    assert r.data["bytes_in"] == 2048
    assert r.data["truncated"] is True


# ====================================================================
# 6. 异常归类
# ====================================================================

async def test_timeout_returns_timeout_error() -> None:
    """ReadTimeout → reason='timeout'。"""

    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(policy=_allow_all_policy(), transport=transport)
    r = await t.handler({"url": "https://x.example/"}, _make_ctx())
    assert r.is_error
    assert r.data["reason"] == "timeout"


# ====================================================================
# 7. 入参校验
# ====================================================================

async def test_invalid_url_returns_bad_args() -> None:
    """非 http/https scheme → bad_args，不发请求。"""
    t = make_http_request_tool(policy=_allow_all_policy())
    r = await t.handler({"url": "file:///etc/passwd"}, _make_ctx())
    assert r.is_error
    assert r.data["reason"] == "bad_args"


async def test_method_not_in_allowed_returns_bad_args() -> None:
    """method 不在 allowed_methods → bad_args。"""
    t = make_http_request_tool(
        policy=_allow_all_policy(),
        allowed_methods=("GET",),
    )
    r = await t.handler(
        {"url": "https://x.example/", "method": "DELETE"}, _make_ctx(),
    )
    assert r.is_error
    assert r.data["reason"] == "bad_args"


# ====================================================================
# 8. 额外：R4 取消 + connect_error 兜底
# ====================================================================

async def test_cancel_before_request_raises() -> None:
    """ctx.cancel 已取消 → handler 入口抛 CancelledError（R4）。"""
    import asyncio

    cancel = CancellationToken()
    cancel.cancel()
    ctx = ToolContext(
        call_id="tc-1", cancel=cancel, thread_id="t", extras={},
    )
    t = make_http_request_tool(policy=_allow_all_policy())
    with pytest.raises(asyncio.CancelledError):
        await t.handler({"url": "https://x.example/"}, ctx)


async def test_connect_error_returns_connect_error_reason() -> None:
    """ConnectError → reason='connect_error'。"""

    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(policy=_allow_all_policy(), transport=transport)
    r = await t.handler({"url": "https://x.example/"}, _make_ctx())
    assert r.is_error
    assert r.data["reason"] == "connect_error"
