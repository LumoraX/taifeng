"""OpenAI GPT-5.6 图片输入真实矩阵与零消耗 wire 预检。

真实场景显式覆盖官方 Chat 与 Responses 两个协议。fixture 是普通库存标签，
断言只看序列号、几何图形、usage/tool 事件和冷恢复顺序，不保存图片正文到日志。
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

import taifeng
from taifeng.llm.image_input import (
    ImageAttachmentV1,
    ImageInputPolicy,
    OpenAIImageCostEstimator,
    admit_image_attachments,
    redact_image_bodies,
)
from taifeng.llm.providers.openai import OpenAIChatClient, OpenAIResponsesClient
from taifeng.llm.types import (
    ApiMessageItem,
    ApiRequest,
    ImagePart,
    ResponseFormatSpec,
    TextPart,
    ToolSpecRef,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.telemetry.jsonl_sink import attach_jsonl_sink
from taifeng.tool.spec import ToolResult, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.llm.client import ModelClient
    from taifeng.llm.events import ResponseEvent
    from taifeng.loop.engine import AgentEngine
    from taifeng.tool.spec import ToolContext

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "inventory-label.png"
SKILLS_DIR = HERE / "fixtures" / "image_matrix_skills"
SERIAL = "TF-5627-A"
GEOMETRY = "blue triangle"


@dataclass
class ImageMatrixResult:
    """单个真实图片场景的稳定台账结果。"""

    scenario_id: str
    capability: str
    verdict: str = "PASS"
    note: str = ""
    kinds: Counter[str] = field(default_factory=Counter)
    duration_s: float = 0.0


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """构造带 CRC 的 PNG chunk。"""
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """仅用标准库生成静态 RGB PNG，供第二张顺序测试图片使用。"""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _attachment(data: bytes, *, detail: str = "high") -> ImageAttachmentV1:
    """从可信 fixture bytes 构造 canonical attachment。"""
    return ImageAttachmentV1(
        media_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"),
        detail=detail,
    )


def _image_part(attachment: ImageAttachmentV1) -> ImagePart:
    """把已审核 attachment 投影为 provider-neutral part。"""
    return ImagePart(
        media_type=attachment.media_type,
        base64_data=attachment.content,
        size=attachment.size,
        sha256=attachment.sha256,
        detail=attachment.detail,
    )


def _fixture_attachment() -> ImageAttachmentV1:
    """读取仓库内良性库存标签。"""
    return _attachment(FIXTURE.read_bytes())


def _policy() -> ImageInputPolicy:
    """真实矩阵使用的显式、有限图片业务策略。"""
    return ImageInputPolicy(
        enabled=True,
        max_images=2,
        max_item_bytes=1024 * 1024,
        max_total_bytes=2 * 1024 * 1024,
        allowed_media_types=frozenset({"image/png"}),
    )


def _schema(name: str, properties: dict[str, dict[str, str]]) -> ResponseFormatSpec:
    """构造严格字符串对象 schema，避免自然语言判定漂移。"""
    return ResponseFormatSpec(
        name=name,
        json_schema={
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        strict=True,
    )


def _single_request(model: str, protocol: str) -> ApiRequest:
    """构造单图语义请求。"""
    prompt = (
        "Read the benign inventory label. Return the exact serial and the colored "
        "geometry in lowercase English. Do not infer missing characters."
    )
    return ApiRequest(
        model=model,
        input_items=[
            ApiMessageItem(
                role="user",
                content=[TextPart(text=prompt), _image_part(_fixture_attachment())],
            )
        ],
        response_format=_schema(
            f"inventory_{protocol}",
            {"serial": {"type": "string"}, "geometry": {"type": "string"}},
        ),
    )


def _multi_request(model: str, protocol: str) -> ApiRequest:
    """构造两图顺序语义请求；第二图是无文字红色矩形。"""
    second = _attachment(_solid_png(160, 90, (220, 38, 38)), detail="low")
    prompt = (
        "Images are ordered. From image 1 return the exact inventory serial. "
        "For image 2 name its color and geometric shape in lowercase English."
    )
    return ApiRequest(
        model=model,
        input_items=[
            ApiMessageItem(
                role="user",
                content=[
                    TextPart(text=prompt),
                    _image_part(_fixture_attachment()),
                    _image_part(second),
                ],
            )
        ],
        response_format=_schema(
            f"inventory_order_{protocol}",
            {
                "first_serial": {"type": "string"},
                "second_geometry": {"type": "string"},
            },
        ),
    )


def _tool_ref() -> ToolSpecRef:
    """图片驱动登记工具的 provider-neutral schema。"""
    return ToolSpecRef(
        name="record_inventory",
        description="Record the exact serial and colored geometry read from the image.",
        input_schema={
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "geometry": {"type": "string"},
            },
            "required": ["serial", "geometry"],
            "additionalProperties": False,
        },
    )


def _tool_request(model: str) -> ApiRequest:
    """构造必须由图片内容触发的工具请求。"""
    return ApiRequest(
        model=model,
        input_items=[
            ApiMessageItem(
                role="user",
                content=[
                    TextPart(
                        text="Read the label, then call record_inventory exactly once."
                    ),
                    _image_part(_fixture_attachment()),
                ],
            )
        ],
        tools=[_tool_ref()],
        parallel_tool_calls=False,
    )


async def _events(client: ModelClient, request: ApiRequest) -> list[ResponseEvent]:
    """消费一次真实 provider stream。"""
    async with client.session(cancel=CancellationToken()) as session:
        return [event async for event in session.stream(request)]


def _assert_usage(events: list[ResponseEvent]) -> None:
    """真实场景必须观测到非零 provider usage。"""
    terminal = [event for event in events if event.kind == "completed"]
    if len(terminal) != 1:
        raise AssertionError("expected exactly one completed event")
    usage = terminal[0].data.get("usage", {})
    if int(usage.get("total_tokens", 0)) <= 0:
        raise AssertionError("provider usage was missing or zero")


def _structured(events: list[ResponseEvent]) -> dict[str, Any]:
    """提取唯一严格 JSON 输出。"""
    matches = [event.data.get("parsed") for event in events if event.kind == "structured_output"]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise AssertionError("expected one structured_output object")
    return matches[0]


def _assert_serial(value: object) -> None:
    """按去空格与大小写归一后核对固定序列号。"""
    normalized = str(value).upper().replace(" ", "")
    if SERIAL not in normalized:
        raise AssertionError(f"serial mismatch: {value!r}")


def _assert_geometry(value: object, *, color: str, shape: str) -> None:
    """核对模型确实提取了颜色与几何图形。"""
    normalized = str(value).lower()
    if color not in normalized or shape not in normalized:
        raise AssertionError(f"geometry mismatch: {value!r}")


async def _run_structured(
    client: ModelClient,
    request: ApiRequest,
    *,
    protocol: str,
    multi: bool,
) -> Counter[str]:
    """执行一个单图或多图严格语义场景。"""
    capabilities = client.capabilities
    if capabilities.provider != "openai" or capabilities.protocol != protocol:
        raise AssertionError("client protocol tag mismatch")
    events = await _events(client, request)
    _assert_usage(events)
    parsed = _structured(events)
    if multi:
        _assert_serial(parsed.get("first_serial"))
        _assert_geometry(parsed.get("second_geometry"), color="red", shape="rectangle")
    else:
        _assert_serial(parsed.get("serial"))
        _assert_geometry(parsed.get("geometry"), color="blue", shape="triangle")
    kinds = Counter(event.kind for event in events)
    kinds[f"protocol_{protocol}"] += 1
    return kinds


async def _run_chat_tool(client: OpenAIChatClient, model: str) -> Counter[str]:
    """验证 Chat 图片内容能驱动一次参数正确的工具调用。"""
    events = await _events(client, _tool_request(model))
    _assert_usage(events)
    calls = [event for event in events if event.kind == "tool_call_done"]
    if len(calls) != 1:
        raise AssertionError("Chat did not emit exactly one image-driven tool call")
    arguments = json.loads(str(calls[0].data.get("arguments", "{}")))
    _assert_serial(arguments.get("serial"))
    _assert_geometry(arguments.get("geometry"), color="blue", shape="triangle")
    kinds = Counter(event.kind for event in events)
    kinds["protocol_chat"] += 1
    return kinds


async def _capture_submission(
    engine: AgentEngine,
    operation: object,
) -> tuple[list[Any], dict[str, Any] | None]:
    """提交一次 operation，并消费到 root 终态。"""
    submission_id = await engine.submit(operation)  # type: ignore[arg-type]
    events: list[Any] = []
    capture: dict[str, Any] | None = None
    async for event in engine.subscribe(submission_id):
        events.append(event.msg)
        if event.msg.kind == "llm_request_recorded":
            capture = event.msg.data
        if event.msg.kind in {"turn_completed", "turn_failed"}:
            if event.msg.kind != "turn_completed":
                raise AssertionError(f"turn failed: {event.msg.data}")
            break
    return events, capture


async def _load_items(pool: Any, thread_id: str) -> list[Any]:
    """读取一个 thread 的可见 durable items。"""
    stream = await pool.store.load_thread(thread_id)
    return [item async for item in stream]


def _assert_redacted(capture: dict[str, Any] | None, image_body: str) -> None:
    """request capture 必须保留结构而不包含图片正文。"""
    if capture is None:
        raise AssertionError("llm_request_recorded was not observed")
    encoded = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    if image_body in encoded or "base64_data" in encoded or "data:image/" in encoded:
        raise AssertionError("request capture leaked image body")
    if "content_redacted" not in encoded:
        raise AssertionError("request capture omitted redaction descriptor")


def _record_tool(observed: list[dict[str, Any]]) -> ToolSpec:
    """构造真实 Engine 场景使用的纯内存登记工具。"""
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx
        observed.append(dict(args))
        return ToolResult.ok(json.dumps({"accepted": True, **args}, ensure_ascii=False))

    return ToolSpec(
        name="record_inventory",
        description="Record the exact serial and colored geometry read from the image.",
        input_schema=_tool_ref().input_schema,
        handler=handler,
        parallel_safe=True,
    )


def _turn_usage(events: list[Any]) -> int:
    """提取 root turn 的累计 token usage。"""
    completed = [msg for msg in events if msg.kind == "turn_completed"]
    if len(completed) != 1:
        raise AssertionError("expected one turn_completed")
    usage = completed[0].data.get("usage", {})
    return int(usage.get("total_tokens", 0))


async def _responses_pool(
    *,
    api_key: str,
    model: str,
    base_url: str,
    storage: Path,
    observed: list[dict[str, Any]],
) -> Any:
    """构造真实 Responses EnginePool，并显式启用图片策略。"""
    client = OpenAIResponsesClient(
        api_key=api_key, model=model, base_url=base_url, timeout_seconds=180.0
    )
    return await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        storage_dir=storage,
        model_client=client,
        extra_tools=[_record_tool(observed)],
        compressors=[],
        enable_request_capture=True,
        image_input_policy=_policy(),
        input_cost_estimator=OpenAIImageCostEstimator(),
    )


async def _run_responses_hot(
    *,
    api_key: str,
    model: str,
    base_url: str,
    root: Path,
    observed: list[dict[str, Any]],
) -> tuple[Counter[str], str, ImageAttachmentV1]:
    """执行热路径图片工具回合，并验证状态持久化与事件脱敏。"""
    pool = await _responses_pool(
        api_key=api_key,
        model=model,
        base_url=base_url,
        storage=root / "store",
        observed=observed,
    )
    attachment = _fixture_attachment()
    event_log = root / "events-hot.jsonl"
    try:
        engine = await pool.get_or_create(
            session_id="image-real", entry_skill_id="inventory-reader"
        )
        attach_jsonl_sink(engine, event_log)
        events, capture = await _capture_submission(
            engine,
            taifeng.UserMessage(
                text="Read and register this benign inventory label.",
                attachments=[attachment.model_dump()],
            ),
        )
        items = await _load_items(pool, engine.thread_id)
        _assert_redacted(capture, attachment.content)
        event_log_text = await anyio.Path(event_log).read_text(encoding="utf-8")
        if attachment.content in event_log_text:
            raise AssertionError("telemetry JSONL leaked image body")
        if len(observed) != 1:
            raise AssertionError("Responses did not execute exactly one tool call")
        _assert_serial(observed[0].get("serial"))
        _assert_geometry(observed[0].get("geometry"), color="blue", shape="triangle")
        encrypted = [
            item.payload.get("provider_state", {}).get("payload", {}).get(
                "encrypted_content"
            )
            for item in items
            if item.kind == "reasoning"
        ]
        if not any(encrypted):
            raise AssertionError("Responses did not persist encrypted reasoning state")
        kinds = Counter(msg.kind for msg in events)
        if kinds["tool_call_started"] != 1 or kinds["tool_call_completed"] != 1:
            raise AssertionError("tool telemetry was incomplete")
        if _turn_usage(events) <= 0:
            raise AssertionError("turn usage was missing")
        kinds.update(protocol_responses=1, encrypted_state_persisted=1)
        return kinds, engine.thread_id, attachment
    finally:
        await pool.close()


async def _run_responses_cold(
    *,
    api_key: str,
    model: str,
    base_url: str,
    root: Path,
    observed: list[dict[str, Any]],
    thread_id: str,
    attachment: ImageAttachmentV1,
) -> Counter[str]:
    """从同一 JSONL thread 冷建 Engine，并验证图片与状态重放。"""
    pool = await _responses_pool(
        api_key=api_key,
        model=model,
        base_url=base_url,
        storage=root / "store",
        observed=observed,
    )
    try:
        engine = await pool.get_or_create(
            session_id="image-real-cold",
            entry_skill_id="inventory-reader",
            resume_thread_id=thread_id,
        )
        attach_jsonl_sink(engine, root / "events-cold.jsonl")
        events, capture = await _capture_submission(
            engine,
            taifeng.UserMessage(text="What exact serial did you register? Answer briefly."),
        )
        items = await _load_items(pool, thread_id)
        _assert_redacted(capture, attachment.content)
        last_text = next(
            str(item.payload.get("text", ""))
            for item in reversed(items)
            if item.kind == "assistant_message"
        )
        _assert_serial(last_text)
        if len(observed) != 1:
            raise AssertionError("cold follow-up unexpectedly repeated the tool side effect")
        if _turn_usage(events) <= 0:
            raise AssertionError("cold turn usage was missing")
        kinds = Counter(msg.kind for msg in events)
        kinds.update(protocol_responses=1, cold_resume=1)
        return kinds
    finally:
        await pool.close()


async def _run_responses_durable(
    *, api_key: str, model: str, base_url: str, root: Path
) -> tuple[Counter[str], Counter[str]]:
    """顺序执行 Responses 热工具回合和冷恢复回合。"""
    observed: list[dict[str, Any]] = []
    hot, thread_id, attachment = await _run_responses_hot(
        api_key=api_key,
        model=model,
        base_url=base_url,
        root=root,
        observed=observed,
    )
    cold = await _run_responses_cold(
        api_key=api_key,
        model=model,
        base_url=base_url,
        root=root,
        observed=observed,
        thread_id=thread_id,
        attachment=attachment,
    )
    return hot, cold


async def _record_result(
    scenario_id: str,
    capability: str,
    operation: Callable[[], Awaitable[Counter[str]]],
) -> ImageMatrixResult:
    """执行单场景并把异常收敛成可继续跑测的 FAIL 记录。"""
    started = time.monotonic()
    result = ImageMatrixResult(scenario_id=scenario_id, capability=capability)
    try:
        result.kinds = await operation()
    except Exception as exc:  # noqa: BLE001 —— 单场景失败不能吞掉后续真实证据
        result.verdict = "FAIL"
        result.note = f"{type(exc).__name__}: {exc}"[:240]
    result.duration_s = time.monotonic() - started
    return result


def _direct_jobs(
    chat: OpenAIChatClient,
    responses: OpenAIResponsesClient,
    model: str,
) -> list[tuple[str, str, Callable[[], Awaitable[Counter[str]]]]]:
    """返回五个彼此独立的 Chat/Responses 真实图片场景。"""
    return [
        (
            "openai_chat_image_single",
            "OpenAI Chat 单图库存语义",
            lambda: _run_structured(
                chat, _single_request(model, "chat"), protocol="chat", multi=False
            ),
        ),
        (
            "openai_chat_image_order",
            "OpenAI Chat 多图顺序",
            lambda: _run_structured(
                chat, _multi_request(model, "chat"), protocol="chat", multi=True
            ),
        ),
        (
            "openai_responses_image_single",
            "OpenAI Responses 单图库存语义",
            lambda: _run_structured(
                responses,
                _single_request(model, "responses"),
                protocol="responses",
                multi=False,
            ),
        ),
        (
            "openai_responses_image_order",
            "OpenAI Responses 多图顺序",
            lambda: _run_structured(
                responses,
                _multi_request(model, "responses"),
                protocol="responses",
                multi=True,
            ),
        ),
        (
            "openai_chat_image_tool",
            "OpenAI Chat 图片驱动工具调用",
            lambda: _run_chat_tool(chat, model),
        ),
    ]


async def _durable_results(
    *, api_key: str, model: str, base_url: str, logs_dir: Path
) -> list[ImageMatrixResult]:
    """运行两个相依 durable 场景，并在任一步失败时分别记 FAIL。"""
    started = time.monotonic()
    definitions = (
        (
            "openai_responses_image_tool_replay",
            "Responses 图片工具、结果下一轮与 encrypted state",
        ),
        (
            "openai_responses_image_cold_resume",
            "Responses 图片与状态 JSONL 冷恢复",
        ),
    )
    try:
        hot, cold = await _run_responses_durable(
            api_key=api_key,
            model=model,
            base_url=base_url,
            root=logs_dir / "openai-responses-image-durable",
        )
    except Exception as exc:  # noqa: BLE001 —— 相依场景如实失败
        note = f"{type(exc).__name__}: {exc}"[:240]
        return [
            ImageMatrixResult(
                scenario_id=sid,
                capability=capability,
                verdict="FAIL",
                note=note,
                duration_s=time.monotonic() - started,
            )
            for sid, capability in definitions
        ]
    return [
        ImageMatrixResult(
            scenario_id=sid,
            capability=capability,
            kinds=kinds,
            duration_s=time.monotonic() - started,
        )
        for (sid, capability), kinds in zip(definitions, (hot, cold), strict=True)
    ]


async def _write_summary(logs_dir: Path, results: list[ImageMatrixResult]) -> None:
    """只写场景状态与事件计数，不写 request 或图片正文。"""
    await anyio.Path(logs_dir).mkdir(parents=True, exist_ok=True)
    summary = [
        {
            "scenario_id": result.scenario_id,
            "verdict": result.verdict,
            "note": result.note,
            "kinds": dict(result.kinds),
            "duration_s": result.duration_s,
        }
        for result in results
    ]
    await anyio.Path(logs_dir / "openai-image-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def run_openai_image_matrix(
    *,
    api_key: str,
    model: str,
    base_url: str,
    logs_dir: Path,
) -> list[ImageMatrixResult]:
    """依次运行 Chat/Responses 图片真实矩阵，并写无图片正文的摘要日志。"""
    chat = OpenAIChatClient(
        api_key=api_key, model=model, base_url=base_url, timeout_seconds=180.0
    )
    responses = OpenAIResponsesClient(
        api_key=api_key, model=model, base_url=base_url, timeout_seconds=180.0
    )
    jobs = _direct_jobs(chat, responses, model)
    results = [
        await _record_result(sid, capability, operation)
        for sid, capability, operation in jobs
    ]
    results.extend(
        await _durable_results(
            api_key=api_key,
            model=model,
            base_url=base_url,
            logs_dir=logs_dir,
        )
    )
    await _write_summary(logs_dir, results)
    return results


def preflight_openai_image_matrix() -> None:
    """零网络预检 fixture、双协议 wire、顺序和 request capture 脱敏。"""
    attachment = _fixture_attachment()
    inspected = admit_image_attachments([attachment], _policy())
    if (inspected[0].width, inspected[0].height) != (800, 450):
        raise AssertionError("inventory fixture dimensions changed")
    chat = OpenAIChatClient(api_key="sk-selfcheck", model="gpt-5.6")
    responses = OpenAIResponsesClient(api_key="sk-selfcheck", model="gpt-5.6")
    chat_request = _multi_request("gpt-5.6", "chat")
    response_request = _multi_request("gpt-5.6", "responses")
    chat_payload = chat.session(cancel=CancellationToken())._build_payload(chat_request)
    response_payload = responses.session(cancel=CancellationToken())._build_payload(
        response_request
    )
    chat_parts = chat_payload["messages"][-1]["content"]
    response_parts = response_payload["input"][-1]["content"]
    if [part["type"] for part in chat_parts] != ["text", "image_url", "image_url"]:
        raise AssertionError("Chat image order preflight failed")
    if [part["type"] for part in response_parts] != [
        "input_text",
        "input_image",
        "input_image",
    ]:
        raise AssertionError("Responses image order preflight failed")
    if chat_payload.get("store") is not False or response_payload.get("store") is not False:
        raise AssertionError("OpenAI store=false invariant failed")
    if "previous_response_id" in response_payload:
        raise AssertionError("Responses must not depend on previous_response_id")
    capture = redact_image_bodies(response_request.model_dump(mode="json"))
    encoded = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    if attachment.content in encoded or "base64_data" in encoded:
        raise AssertionError("image request capture redaction failed")
