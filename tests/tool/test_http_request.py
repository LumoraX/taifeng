"""http_request 工具单测。

设计：
    - 全部走 ``httpx.MockTransport``，**0 真实网络**
    - 覆盖 spec ``tool-builtins-extended`` 的 http_request Requirement 全部 11 个 Scenario
"""

from __future__ import annotations

import json
from typing import Any

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


# ====================================================================
# 9. redirect 逐跳审批（wave1 task 3 —— SSRF 绕过审批）
# ====================================================================

class _RecordingPolicy(PermissionPolicy):
    """记录每次 check 收到的 request，便于断言逐跳审批的次数与 metadata。"""

    def __init__(self, inner: PermissionPolicy) -> None:
        super().__init__(rules=list(inner.rules), default_mode=inner.default_mode)
        self.requests: list[Any] = []

    async def check(self, request: Any) -> Any:
        self.requests.append(request)
        return await super().check(request)


def _allow_only(prefix: str) -> PermissionPolicy:
    """只放行以 prefix 开头的 URL（任意 method），其余 deny。"""
    return PermissionPolicy(
        rules=[
            PermissionRule(
                scope="network",
                target_pattern=f"glob:* {prefix}*",
                mode="allow",
                reason="allowlist",
            ),
        ],
        default_mode="deny",
    )


async def test_redirect_second_hop_denied_is_not_sent() -> None:
    """首跳放行、302 指向内网元数据地址 → 二跳被 deny，transport 不收到二跳请求。"""
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.example.com":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"},
            )
        return httpx.Response(200, text="SECRET")

    transport = httpx.MockTransport(respond)
    policy = _RecordingPolicy(_allow_only("https://api.example.com/"))
    t = make_http_request_tool(policy=policy, transport=transport)
    r = await t.handler({"url": "https://api.example.com/x"}, _make_ctx())
    assert r.is_error
    assert r.data["reason"] == "permission_denied"
    assert seen == ["https://api.example.com/x"], "second hop MUST NOT be sent"
    assert len(policy.requests) == 2
    assert policy.requests[1].metadata["redirect_hop"] == 1
    assert policy.requests[1].metadata["redirect_from"] == "https://api.example.com/x"


async def test_redirect_chain_fully_allowed_returns_final_body() -> None:
    """A → 302 → B → 200：两跳都过 policy，返回 B 的响应体。"""
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"location": "https://b.example/final"})
        return httpx.Response(200, text="final-body")

    transport = httpx.MockTransport(respond)
    policy = _RecordingPolicy(_allow_all_policy())
    t = make_http_request_tool(policy=policy, transport=transport)
    r = await t.handler({"url": "https://a.example/start"}, _make_ctx())
    assert not r.is_error
    assert r.data["status_code"] == 200
    assert "final-body" in r.output
    assert [req.target for req in policy.requests] == [
        "GET https://a.example/start",
        "GET https://b.example/final",
    ]


async def test_redirect_exceeding_max_redirects_returns_redirect_limit() -> None:
    """max_redirects=2，链路 A→B→C→D 全 302 → redirect_limit，D 不发出。"""
    seen: list[str] = []
    chain = {
        "a.example": "https://b.example/",
        "b.example": "https://c.example/",
        "c.example": "https://d.example/",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        nxt = chain.get(request.url.host)
        if nxt:
            return httpx.Response(302, headers={"location": nxt})
        return httpx.Response(200, text="never")

    transport = httpx.MockTransport(respond)
    t = make_http_request_tool(
        policy=_allow_all_policy(), transport=transport, max_redirects=2,
    )
    r = await t.handler({"url": "https://a.example/"}, _make_ctx())
    assert r.is_error
    assert r.data["reason"] == "redirect_limit"
    assert "d.example" not in seen
