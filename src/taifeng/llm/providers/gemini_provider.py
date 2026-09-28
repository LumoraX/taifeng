"""Gemini 原生 provider —— 直连 streamGenerateContent，零 google-genai-sdk 依赖。

适用：Google AI Studio Gemini API（含 functionCall / cachedContent 元数据）。

参照：https://ai.google.dev/api/generate-content#streamGenerateContent

与 LiteLLM 路径的差异：
    - role 映射 ``assistant`` → ``model``、``tool`` → ``function`` 在本层完成
    - tools 字段是 ``[{functionDeclarations: [...]}]`` 嵌套结构
    - usage 从 ``usageMetadata`` 直接读（含 ``cachedContentTokenCount``）
    - functionCall 不流式发 args delta：上游整体到达，本层一次性 emit
      ``tool_call_done``（不发 ``tool_call_delta``）
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, Literal

from taifeng.llm.client import ModelClient, OneNetworkAttemptModelClient
from taifeng.llm.errors import (
    InvalidRequestError,
    InvalidResponseError,
    UnsupportedModalityError,
)
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    error,
    prompt_cache,
    rate_limits,
    reasoning_delta,
    server_model,
    text_delta,
    tool_call_done,
)
from taifeng.llm.providers._shared import (
    assert_text_only_request,
    classify_abnormal_finish,
    classify_http_error,
    extract_rate_limit_snapshot,
    extract_request_id,
    extract_usage_gemini,
    mid_history_system_text,
    parse_sse_data,
    transport_error,
)
from taifeng.llm.types import ApiRequest, ImagePart, TextPart, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.loop.cancellation import CancellationToken

# thinking-passback：reasoning_effort → Gemini thinkingBudget（请求级覆盖客户端配置）
_EFFORT_BUDGETS = {"none": 0, "minimal": 512, "low": 1024, "medium": 8192, "high": 24576}


def _thought_signature(tool_call: dict[str, Any]) -> str | None:
    """从 tool_call 的 extra_content 取回 Gemini thought signature（与 OpenAI 兼容版同形）。"""
    extra = tool_call.get("extra_content")
    google = extra.get("google") if isinstance(extra, dict) else None
    sig = google.get("thought_signature") if isinstance(google, dict) else None
    return sig if isinstance(sig, str) and sig else None


# Gemini role 映射
_ROLE_MAP = {
    "user": "user",
    "assistant": "model",
    "tool": "function",
    "system": "user",  # system 已合并到 systemInstruction；保险兜底
}



def _gemini_parts(content: list[TextPart | ImagePart]) -> list[dict[str, Any]]:
    """把纯文本 parts 映射为 Gemini ``parts``(``{text}``)。

    参照 ``openai/_shared.py`` 的 part 映射范式,差异:本 provider **未声明 image
    输入能力**,故只映射文本;图片由 ``_build_payload`` 开头的
    ``assert_text_only_request`` 在序列化前拒掉(与 openai_compat 同一道门控)。

    此前两处调用点是裸 ``parts.extend(msg.content)``,且 ``_build_payload`` 漏调
    门控 —— 把 pydantic 模型对象原样塞进请求体,经 httpx ``json=`` 发出时
    ``TypeError: Object of type TextPart is not JSON serializable``。

    Args:
        content: 核心层 ``PartContent`` 的 list 形态。

    Returns:
        可 JSON 序列化的 Gemini part 列表;空文本项丢弃(不承载信息,白占数组槽位)。

    Raises:
        UnsupportedModalityError: 含 ImagePart。正常路径已被门控先拒;这里再拒一次,
            保证直接调用本函数也不会**悄悄丢图**(禁止 silent fallback)。
    """
    mapped: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, ImagePart):
            raise UnsupportedModalityError("image input is not supported by this client")
        if part.text:
            mapped.append({"text": part.text})
    return mapped


def _to_gemini_contents(
    req: ApiRequest,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """把 ``ApiRequest`` 翻译为 Gemini ``systemInstruction`` + ``contents``。

    转换规则：
        - ``system_prompt: list[str]`` → 合并为 ``systemInstruction.parts[].text``
        - role 映射：assistant → model，tool → function
        - 文本 content → ``parts: [{text}]``
        - assistant.tool_calls → ``parts: [{functionCall: {name, args}}]``
        - tool 角色的 result → ``parts: [{functionResponse: {name, response}}]``
          （``name`` 是**函数名**，由同一请求内 assistant 的 tool_calls 回溯
          ``call_id → name``；回溯不到时退回 call_id 兜底，不猜测函数名）
    """
    sys_parts = [s for s in req.system_prompt if s]
    system_instruction: dict[str, Any] | None = None
    if sys_parts:
        system_instruction = {
            "parts": [{"text": "\n\n".join(sys_parts)}],
        }

    # call_id → 函数名映射：Gemini 的 functionResponse.name 要求填**函数名**，
    # 而 ApiMessage 的 tool 消息只带 tool_call_id。先扫一遍 assistant 的
    # tool_calls 建索引，让 tool 结果能对上其函数声明（对不上则退回 call_id）。
    call_id_to_name: dict[str, str] = {}
    for msg in req.messages:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for tc in msg.tool_calls:
            call_id = tc.get("id")
            fn_name = (tc.get("function") or {}).get("name")
            if call_id and fn_name:
                call_id_to_name[str(call_id)] = str(fn_name)

    contents: list[dict[str, Any]] = []
    for msg in req.messages:
        if msg.role == "system":
            # 顶层 system prompt 在 systemInstruction；这里是历史中段的注记（压缩摘要 /
            # pinned / 预算提示 / 记忆 / 业务注入）→ 原位改写为 user 文本，不得丢弃；
            # 与相邻 user 内容合并，避免连续同角色 content（mid-history-system）
            note = {"text": mid_history_system_text(msg.content)}
            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"].append(note)
            else:
                contents.append({"role": "user", "parts": [note]})
            continue

        gem_role = _ROLE_MAP.get(msg.role, "user")
        parts: list[dict[str, Any]] = []

        # tool 角色 → functionResponse
        if msg.role == "tool":
            # Gemini 要求 functionResponse.name 是**函数名**。优先用上面建好的
            # call_id → name 映射；回溯不到（如业务直接构造的裸 tool 消息）时
            # 退回 tool_call_id 兜底，不猜测、不伪造函数名。
            fn_name = call_id_to_name.get(
                str(msg.tool_call_id or ""), msg.tool_call_id or "",
            )
            if isinstance(msg.content, list):
                parts.extend(_gemini_parts(msg.content))
            else:
                raw = (
                    msg.content
                    if isinstance(msg.content, str)
                    else json.dumps(msg.content)
                )
                # 把 string 包成 functionResponse.response.content
                parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"content": raw},
                    },
                })
        else:
            # 文本 content
            if isinstance(msg.content, str):
                if msg.content:
                    parts.append({"text": msg.content})
            elif isinstance(msg.content, list):
                parts.extend(_gemini_parts(msg.content))

            # assistant.tool_calls → functionCall
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    fn = tc.get("function") or {}
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args = (
                            json.loads(args_raw)
                            if isinstance(args_raw, str)
                            else args_raw
                        )
                    except json.JSONDecodeError:
                        args = {}
                    fc_part: dict[str, Any] = {
                        "functionCall": {
                            "name": fn.get("name", ""),
                            "args": args,
                        },
                    }
                    # thinking-passback：thinking 模型的 functionCall part 必须带回
                    # 其 thoughtSignature，否则续传被拒（签名随 function_call 落史）
                    signature = _thought_signature(tc)
                    if signature is not None:
                        fc_part["thoughtSignature"] = signature
                    parts.append(fc_part)

        if not parts:
            continue
        # 相邻 user content 合并（中段 system 注记改写为 user 后可能与真实 user 相邻）
        if gem_role == "user" and contents and contents[-1]["role"] == "user":
            contents[-1]["parts"].extend(parts)
            continue
        contents.append({"role": gem_role, "parts": parts})

    return system_instruction, contents


def _to_gemini_tools(req: ApiRequest) -> list[dict[str, Any]] | None:
    """``ToolSpecRef`` → Gemini ``tools: [{functionDeclarations: [...]}]``。"""
    if not req.tools:
        return None
    return [{
        "functionDeclarations": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }
            for t in req.tools
        ],
    }]


class GeminiSession:
    """单 turn Gemini streamGenerateContent 调用 session。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        cancel: CancellationToken,
        auth_via: Literal["query", "header"] = "query",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
        previous_cache_read: int = 0,
        thinking_budget: int | None = None,
        include_thoughts: bool = False,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # thinking 配置（None = 用模型默认；include_thoughts 让思考摘要经 reasoning_delta 流出）
        self._thinking_budget = thinking_budget
        self._include_thoughts = include_thoughts
        self._base_url = base_url.rstrip("/")
        self._cancel = cancel
        self._auth_via = auth_via
        self._timeout = timeout_seconds
        self._previous_cache_read = previous_cache_read
        self._last_usage: TokenUsage | None = None
        self._end_turn = True
        # 本次流最后一个非空 finishReason —— 供流末判定终止真相 + 原样透传
        self._last_finish_reason: str | None = None
        # 本次流是否产出过文本（判异常终止时用：有产出就不作废）
        self._produced_text = False

        # 空 key 时省略鉴权（与其余 native client 一致）。header 模式不发空
        # x-goog-api-key；query 模式见 _build_url —— 空 key 不挂 &key=。
        headers: dict[str, str] = {"content-type": "application/json"}
        if auth_via == "header" and api_key.strip():
            headers["x-goog-api-key"] = api_key
        if extra_headers:
            headers.update(extra_headers)
        self._headers = headers

    async def __aenter__(self) -> GeminiSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    def _build_payload(self, request: ApiRequest) -> dict[str, Any]:
        # 与 openai_compat 同一道门控:本 provider 只消费兼容 messages view、未声明
        # image 输入能力,序列化前显式拒图,避免 pydantic part 泄漏进 JSON encoder
        assert_text_only_request(request)
        system_instruction, contents = _to_gemini_contents(request)
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction is not None:
            payload["systemInstruction"] = system_instruction
        gen_config: dict[str, Any] = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            gen_config["maxOutputTokens"] = request.max_output_tokens
        thinking = self._thinking_config(request)
        if thinking:
            gen_config["thinkingConfig"] = thinking
        if gen_config:
            payload["generationConfig"] = gen_config
        tools = _to_gemini_tools(request)
        if tools is not None:
            payload["tools"] = tools
        return payload

    def _thinking_config(self, request: ApiRequest) -> dict[str, Any]:
        """generationConfig.thinkingConfig：请求级 reasoning_effort 优先于客户端预算。"""
        config: dict[str, Any] = {}
        budget = (
            _EFFORT_BUDGETS.get(request.reasoning_effort)
            if request.reasoning_effort is not None else self._thinking_budget
        )
        if budget is not None:
            config["thinkingBudget"] = budget
        if self._include_thoughts:
            config["includeThoughts"] = True
        return config

    def _build_url(self, model: str) -> str:
        url = (
            f"{self._base_url}/v1beta/models/{model}:streamGenerateContent"
            "?alt=sse"
        )
        # 空 key 不挂 &key=（避免发出语义为空的鉴权参数；真实服务端会干净 401）。
        if self._auth_via == "query" and self._api_key.strip():
            url += f"&key={self._api_key}"
        return url

    async def stream(  # noqa: C901
        self, request: ApiRequest,
    ) -> AsyncIterator[ResponseEvent]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise InvalidRequestError(
                "httpx required for GeminiClient",
            ) from exc

        model = request.model or self._model
        payload = self._build_payload(request)
        url = self._build_url(model)

        yield created()
        yield server_model(model)

        # functionCall 不流式发 args delta —— 整体到达后一次性 emit done
        pending_tool_calls: list[dict[str, Any]] = []
        request_id: str | None = None  # G3：服务端 request-id

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream(
                    "POST", url, headers=self._headers, json=payload,
                ) as resp:
                    request_id = extract_request_id(resp.headers)
                    if resp.status_code != 200:
                        body = await resp.aread()
                        classified = classify_http_error(
                            resp.status_code,
                            body.decode("utf-8", errors="replace"),
                            provider="gemini",
                        )
                        classified.request_id = request_id
                        yield error(
                            message=str(classified),
                            kind=classified.kind,
                            retryable=classified.retryable,
                        )
                        raise classified

                    snapshot = extract_rate_limit_snapshot(resp.headers)
                    if snapshot is not None:
                        yield rate_limits(snapshot)

                    async for line in resp.aiter_lines():
                        self._cancel.raise_if_cancelled()
                        chunk = parse_sse_data(line)
                        if chunk is None:
                            continue
                        async for ev in self._process_chunk(
                            chunk, pending_tool_calls,
                        ):
                            yield ev
            except httpx.TransportError as exc:
                # 传输层失败统一归瞬时网络错。放宽到 TransportError 是为了覆盖
                # ProtocolError —— 尤其 RemoteProtocolError（"Server disconnected
                # without sending a response"，代理/网关流中途断连）。它不属
                # NetworkError，此前会裸逃 → classify_failure 落 unknown 硬失败，
                # 既不重试也不挂起恢复（与 openai_compat 同一处修复）。
                # ``TimeoutException`` 亦属 ``TransportError``，一并由
                # ``transport_error`` 判相位并剥离 URL。
                raise transport_error(exc, provider="gemini") from exc

        # 流终止真相（llm-provider-native 契约）：没见过任何 finishReason 说明流被
        # 中途掐断，绝不能发 completed 把它伪造成「成功的空回复」。
        if self._last_finish_reason is None:
            raise InvalidResponseError(
                "gemini stream ended without any finishReason terminal marker"
            )
        produced = bool(pending_tool_calls) or self._produced_text
        if not produced:
            failure = classify_abnormal_finish(
                self._last_finish_reason, provider="gemini",
            )
            if failure is not None:
                failure.request_id = request_id
                yield error(
                    message=str(failure),
                    kind=failure.kind,
                    retryable=failure.retryable,
                )
                raise failure

        # 流末把累积的 functionCall 整体发出
        for tc in pending_tool_calls:
            yield tool_call_done(
                call_id=tc["call_id"],
                name=tc["name"],
                arguments=tc["arguments"],
                extra_content=tc.get("extra_content"),
            )

        if self._last_usage is not None:
            yield prompt_cache(
                cache_read=self._last_usage.cache_read_input_tokens,
                cache_creation=self._last_usage.cache_creation_input_tokens,
                previous_cache_read=self._previous_cache_read,
            )

        yield completed(
            response_id=None,
            usage=self._last_usage or TokenUsage(),
            end_turn=self._end_turn,
            request_id=request_id,
            stop_reason=self._last_finish_reason,
        )

    async def _process_chunk(
        self,
        chunk: dict[str, Any],
        pending_tool_calls: list[dict[str, Any]],
    ) -> AsyncIterator[ResponseEvent]:
        """处理一个 SSE chunk —— Gemini chunk 形状 {candidates, usageMetadata}。"""
        candidates = chunk.get("candidates") or []
        if candidates:
            cand = candidates[0]
            content = cand.get("content") or {}
            for part in content.get("parts") or []:
                if "text" in part and part.get("thought") is True:
                    # includeThoughts 下的思考摘要：走 reasoning 流，不混进正文
                    t = part.get("text", "")
                    if t:
                        yield reasoning_delta(t)
                elif "text" in part:
                    t = part.get("text", "")
                    if t:
                        self._produced_text = True
                        yield text_delta(t)
                elif "functionCall" in part:
                    fc = part["functionCall"] or {}
                    name = fc.get("name", "")
                    args = fc.get("args", {}) or {}
                    call: dict[str, Any] = {
                        "call_id": f"fc_{uuid.uuid4().hex[:24]}",
                        "name": name,
                        "arguments": json.dumps(args),
                    }
                    # thinking-passback：签名挂在 functionCall part 上，经 extra_content
                    # 随 function_call 落史（与 OpenAI 兼容版 Gemini 同一形状）
                    signature = part.get("thoughtSignature")
                    if isinstance(signature, str) and signature:
                        call["extra_content"] = {"google": {"thought_signature": signature}}
                    pending_tool_calls.append(call)

            finish_reason = cand.get("finishReason")
            if finish_reason:
                # STOP → end_turn=True；其他（TOOL_CALL / MAX_TOKENS / ...） → False
                self._end_turn = finish_reason == "STOP"
                self._last_finish_reason = finish_reason

        # usage 元数据通常在末 chunk
        usage_raw = chunk.get("usageMetadata")
        if usage_raw:
            self._last_usage = extract_usage_gemini(usage_raw)

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._last_usage


class GeminiClient(OneNetworkAttemptModelClient, ModelClient):
    """Session 级 Gemini native 客户端。

    构造参数：
        api_key: GEMINI_API_KEY（业务侧从环境变量读后注入）
        model: 默认模型名，如 ``gemini-2.0-flash-exp``
        base_url: 默认 ``https://generativelanguage.googleapis.com``
        auth_via: ``"query"``（默认，URL 上挂 ``?key=``） / ``"header"``
        extra_headers: 额外 header
        timeout_seconds: httpx 超时
        thinking_budget: ``generationConfig.thinkingConfig.thinkingBudget``（None = 模型默认）；
            请求级 ``reasoning_effort`` 显式给出时覆盖。
        include_thoughts: 让思考摘要经 ``reasoning_delta`` 流出（默认 False）。
            无论是否开启，functionCall 的 ``thoughtSignature`` 都会随调用落史并回传。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.0-flash-exp",
        base_url: str = "https://generativelanguage.googleapis.com",
        auth_via: Literal["query", "header"] = "query",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
        thinking_budget: int | None = None,
        include_thoughts: bool = False,
    ) -> None:
        if thinking_budget is not None and thinking_budget < 0:
            raise ValueError(f"thinking_budget must be >= 0 or None, got {thinking_budget}")
        self._api_key = api_key
        self._default_model = model
        self._thinking_budget = thinking_budget
        self._include_thoughts = include_thoughts
        self._base_url = base_url
        self._auth_via = auth_via
        self._extra_headers = extra_headers
        self._timeout_seconds = timeout_seconds
        self._previous_cache_read = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> GeminiSession:
        return GeminiSession(
            api_key=self._api_key,
            model=model or self._default_model,
            base_url=self._base_url,
            cancel=cancel,
            auth_via=self._auth_via,
            extra_headers=self._extra_headers,
            timeout_seconds=self._timeout_seconds,
            previous_cache_read=self._previous_cache_read,
            thinking_budget=self._thinking_budget,
            include_thoughts=self._include_thoughts,
        )

    def record_cache_read(self, value: int) -> None:
        self._previous_cache_read = value


__all__ = ["GeminiClient", "GeminiSession"]
