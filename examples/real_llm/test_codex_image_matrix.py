"""独立 Codex provider 的图片、工具、state 与冷恢复真实矩阵。"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

import anyio
import test_openai_image_matrix as shared

import taifeng
from taifeng.llm.audit_redaction import project_attempt_request
from taifeng.llm.image_input import (
    OpenAIImageCostEstimator,
    redact_sensitive_request_data,
)
from taifeng.llm.providers.codex import CodexResponsesClient
from taifeng.llm.providers.codex.accumulator import CodexResponsesAccumulator
from taifeng.llm.providers.codex.wire import build_codex_payload
from taifeng.llm.types import (
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    ProviderStateEnvelope,
    TextPart,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.telemetry.jsonl_sink import attach_jsonl_sink

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from taifeng.llm.client import ModelClient
    from taifeng.llm.events import ResponseEvent

ImageMatrixResult = shared.ImageMatrixResult


def _instruction_request(model: str) -> ApiRequest:
    """构造顶层 instructions 语义验收请求。"""
    return ApiRequest(
        model=model,
        system_prompt=[
            "Return the exact code supplied by the user.",
            "Use the required JSON schema and no extra commentary.",
        ],
        input_items=[
            ApiMessageItem(
                role="user",
                content=[TextPart(text="The exact code is CODEX-INSTRUCTION-5627.")],
            )
        ],
        response_format=shared._schema(
            "codex_instruction", {"code": {"type": "string"}}
        ),
    )


async def _events(client: ModelClient, request: ApiRequest) -> list[ResponseEvent]:
    """消费一次独立 Codex 网络 attempt。"""
    async with client.session(cancel=CancellationToken()) as session:
        return [event async for event in session.stream(request)]


def _kinds(events: list[ResponseEvent], marker: str) -> Counter[str]:
    """核对真实 usage，并附 provider/dialect 场景标记。"""
    shared._assert_usage(events)
    kinds = Counter(event.kind for event in events)
    kinds.update(provider_codex=1, dialect_codex_responses_v1=1)
    kinds[marker] += 1
    return kinds


async def _run_instructions(client: ModelClient, model: str) -> Counter[str]:
    """验证 instructions 被代理实际执行。"""
    events = await _events(client, _instruction_request(model))
    parsed = shared._structured(events)
    if parsed.get("code") != "CODEX-INSTRUCTION-5627":
        raise AssertionError(f"instructions semantic mismatch: {parsed!r}")
    return _kinds(events, "instructions_verified")


async def _run_image(
    client: ModelClient,
    model: str,
    *,
    multi: bool,
) -> Counter[str]:
    """验证单图或有序多图语义。"""
    request = (
        shared._multi_request(model, "codex")
        if multi
        else shared._single_request(model, "codex")
    )
    events = await _events(client, request)
    parsed = shared._structured(events)
    if multi:
        shared._assert_serial(parsed.get("first_serial"))
        shared._assert_geometry(
            parsed.get("second_geometry"), color="red", shape="rectangle"
        )
        return _kinds(events, "ordered_multi_image_verified")
    shared._assert_serial(parsed.get("serial"))
    shared._assert_geometry(parsed.get("geometry"), color="blue", shape="triangle")
    return _kinds(events, "single_image_verified")


async def _run_tool(client: ModelClient, model: str) -> Counter[str]:
    """验证图片直接驱动一个 normalized function call。"""
    events = await _events(client, shared._tool_request(model))
    shared._assert_usage(events)
    outputs = [event.data.get("items") for event in events if event.kind == "normalized_output"]
    if len(outputs) != 1 or not isinstance(outputs[0], list):
        raise AssertionError("Codex did not emit one normalized output")
    calls = [item for item in outputs[0] if item.get("type") == "function_call"]
    if len(calls) != 1 or calls[0].get("name") != "record_inventory":
        raise AssertionError("Codex did not emit exactly one inventory function call")
    arguments = json.loads(str(calls[0].get("arguments", "{}")))
    shared._assert_serial(arguments.get("serial"))
    shared._assert_geometry(arguments.get("geometry"), color="blue", shape="triangle")
    return _kinds(events, "image_tool_call_verified")


async def _pool(
    *,
    api_key: str,
    model: str,
    base_url: str,
    storage: Path,
    observed: list[dict[str, Any]],
) -> Any:
    """构造 Codex legacy JSONL EnginePool。"""
    client = CodexResponsesClient(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=180.0,
    )
    return await taifeng.EnginePool.create(
        skills_dir=shared.SKILLS_DIR,
        storage_dir=storage,
        model_client=client,
        extra_tools=[shared._record_tool(observed)],
        compressors=[],
        enable_request_capture=True,
        image_input_policy=shared._policy(),
        input_cost_estimator=OpenAIImageCostEstimator(),
    )


def _assert_capture_safe(
    capture: dict[str, Any] | None,
    *,
    image_body: str,
) -> None:
    """普通 capture 必须同时删除图片正文和 reasoning ciphertext。"""
    shared._assert_redacted(capture, image_body)
    encoded = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    if "encrypted_content" in encoded or "data:image/" in encoded:
        raise AssertionError("Codex request capture leaked provider state or Data URL")


async def _run_hot_state(
    *,
    api_key: str,
    model: str,
    base_url: str,
    root: Path,
    observed: list[dict[str, Any]],
) -> tuple[Counter[str], str, Any]:
    """验证图片工具回合内的 encrypted state 热重放。"""
    pool = await _pool(
        api_key=api_key,
        model=model,
        base_url=base_url,
        storage=root / "store",
        observed=observed,
    )
    attachment = shared._fixture_attachment()
    event_log = root / "events-hot.jsonl"
    try:
        engine = await pool.get_or_create(
            session_id="codex-image-real", entry_skill_id="inventory-reader"
        )
        attach_jsonl_sink(engine, event_log)
        events, capture = await shared._capture_submission(
            engine,
            taifeng.UserMessage(
                text="Read and register this benign inventory label.",
                attachments=[attachment.model_dump()],
            ),
        )
        items = await shared._load_items(pool, engine.thread_id)
        _assert_capture_safe(capture, image_body=attachment.content)
        event_text = await anyio.Path(event_log).read_text(encoding="utf-8")
        if attachment.content in event_text or "encrypted_content" in event_text:
            raise AssertionError("Codex telemetry leaked sensitive request data")
        if len(observed) != 1:
            raise AssertionError("Codex hot path did not execute exactly one tool call")
        shared._assert_serial(observed[0].get("serial"))
        states = [
            item.payload.get("provider_state", {})
            for item in items
            if item.kind == "reasoning"
        ]
        if not states or not any(
            state.get("provider") == "codex"
            and state.get("protocol") == "responses"
            and state.get("payload", {}).get("encrypted_content")
            for state in states
        ):
            raise AssertionError("Codex encrypted reasoning state was not persisted")
        if shared._turn_usage(events) <= 0:
            raise AssertionError("Codex hot turn usage was missing")
        kinds = Counter(message.kind for message in events)
        kinds.update(
            provider_codex=1,
            image_tool_executed=1,
            encrypted_state_hot_replayed=1,
        )
        return kinds, engine.thread_id, attachment
    finally:
        await pool.close()


async def _run_cold_resume(
    *,
    api_key: str,
    model: str,
    base_url: str,
    root: Path,
    observed: list[dict[str, Any]],
    thread_id: str,
    attachment: Any,
) -> Counter[str]:
    """重开 pool 并从 legacy JSONL 恢复 Codex 图片/state 历史。"""
    pool = await _pool(
        api_key=api_key,
        model=model,
        base_url=base_url,
        storage=root / "store",
        observed=observed,
    )
    try:
        engine = await pool.get_or_create(
            session_id="codex-image-cold",
            entry_skill_id="inventory-reader",
            resume_thread_id=thread_id,
        )
        events, capture = await shared._capture_submission(
            engine,
            taifeng.UserMessage(text="What exact serial did you register? Answer briefly."),
        )
        items = await shared._load_items(pool, thread_id)
        _assert_capture_safe(capture, image_body=attachment.content)
        last_text = next(
            str(item.payload.get("text", ""))
            for item in reversed(items)
            if item.kind == "assistant_message"
        )
        shared._assert_serial(last_text)
        if len(observed) != 1:
            raise AssertionError("Codex cold resume repeated the tool side effect")
        if shared._turn_usage(events) <= 0:
            raise AssertionError("Codex cold turn usage was missing")
        kinds = Counter(message.kind for message in events)
        kinds.update(provider_codex=1, legacy_jsonl_cold_resume=1)
        return kinds
    finally:
        await pool.close()


async def _durable_results(
    *, api_key: str, model: str, base_url: str, logs_dir: Path
) -> list[ImageMatrixResult]:
    """顺序运行热 state 与冷恢复，并分别记账。"""
    started = time.monotonic()
    observed: list[dict[str, Any]] = []
    definitions = (
        ("codex_encrypted_state_hot_replay", "Codex encrypted state 热重放"),
        ("codex_legacy_jsonl_cold_resume", "Codex 图片/state legacy JSONL 冷恢复"),
    )
    try:
        hot, thread_id, attachment = await _run_hot_state(
            api_key=api_key,
            model=model,
            base_url=base_url,
            root=logs_dir / "codex-durable",
            observed=observed,
        )
        cold = await _run_cold_resume(
            api_key=api_key,
            model=model,
            base_url=base_url,
            root=logs_dir / "codex-durable",
            observed=observed,
            thread_id=thread_id,
            attachment=attachment,
        )
    except Exception as exc:  # noqa: BLE001 —— 相依场景如实失败
        note = f"{type(exc).__name__}: {exc}"[:240]
        return [
            ImageMatrixResult(
                scenario_id=scenario_id,
                capability=capability,
                verdict="FAIL",
                note=note,
                duration_s=time.monotonic() - started,
            )
            for scenario_id, capability in definitions
        ]
    return [
        ImageMatrixResult(
            scenario_id=scenario_id,
            capability=capability,
            kinds=kinds,
            duration_s=time.monotonic() - started,
        )
        for (scenario_id, capability), kinds in zip(
            definitions, (hot, cold), strict=True
        )
    ]


async def _record_result(
    scenario_id: str,
    capability: str,
    operation: Callable[[], Awaitable[Counter[str]]],
) -> ImageMatrixResult:
    """把一个真实场景异常收敛为稳定 FAIL。"""
    started = time.monotonic()
    result = ImageMatrixResult(scenario_id=scenario_id, capability=capability)
    try:
        result.kinds = await operation()
    except Exception as exc:  # noqa: BLE001 —— 后续真实场景仍需执行
        result.verdict = "FAIL"
        result.note = f"{type(exc).__name__}: {exc}"[:240]
    result.duration_s = time.monotonic() - started
    return result


async def run_codex_image_matrix(
    *,
    api_key: str,
    model: str,
    base_url: str,
    logs_dir: Path,
) -> list[ImageMatrixResult]:
    """运行六个 Codex 真实场景并只写安全摘要。"""
    client = CodexResponsesClient(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=180.0,
    )
    jobs = (
        (
            "codex_instructions",
            "Codex 顶层 instructions",
            lambda: _run_instructions(client, model),
        ),
        (
            "codex_image_single",
            "Codex 单图片语义",
            lambda: _run_image(client, model, multi=False),
        ),
        (
            "codex_image_order",
            "Codex 有序多图片语义",
            lambda: _run_image(client, model, multi=True),
        ),
        (
            "codex_image_tool_call",
            "Codex 图片驱动 function call",
            lambda: _run_tool(client, model),
        ),
    )
    results = [
        await _record_result(scenario_id, capability, operation)
        for scenario_id, capability, operation in jobs
    ]
    results.extend(
        await _durable_results(
            api_key=api_key,
            model=model,
            base_url=base_url,
            logs_dir=logs_dir,
        )
    )
    await anyio.Path(logs_dir).mkdir(parents=True, exist_ok=True)
    safe_summary = [
        {
            "scenario_id": result.scenario_id,
            "verdict": result.verdict,
            "note": result.note,
            "kinds": dict(result.kinds),
            "duration_s": result.duration_s,
        }
        for result in results
    ]
    await anyio.Path(logs_dir / "codex-image-summary.json").write_text(
        json.dumps(safe_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def _message_events(text: str) -> list[dict[str, object]]:
    """构造代理实测的 done-item → completed 序列。"""
    done = {
        "id": "msg_selfcheck",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**done, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_selfcheck",
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "total_tokens": 6,
                },
            },
        },
    ]


def preflight_codex_image_matrix() -> None:
    """零网络预检 instructions/list、多图、done、state 与双重脱敏。"""
    request = shared._multi_request("gpt-5.6-luna", "codex").model_copy(
        update={"system_prompt": [" first ", "second"]}
    )
    payload = build_codex_payload(request, default_model="gpt-5.6-luna")
    if payload.get("instructions") != " first \n\nsecond":
        raise AssertionError("Codex instructions preflight failed")
    if not isinstance(payload.get("input"), list):
        raise AssertionError("Codex input must remain a typed list")
    parts = payload["input"][0]["content"]
    if [part["type"] for part in parts] != [
        "input_text",
        "input_image",
        "input_image",
    ]:
        raise AssertionError("Codex ordered image wire preflight failed")

    accumulator = CodexResponsesAccumulator()
    for event in _message_events("done fact"):
        accumulator.accept(event)
    terminal = accumulator.finalize()
    if terminal.items[0].text != "done fact":  # type: ignore[union-attr]
        raise AssertionError("Codex done-item accumulator preflight failed")

    attachment = shared._fixture_attachment()
    ciphertext = "SELFHECK-CIPHERTEXT-SENTINEL"
    state_request = ApiRequest(
        model="gpt-5.6-luna",
        input_items=[
            ApiMessageItem(
                role="user",
                content=[TextPart(text="inspect"), shared._image_part(attachment)],
            ),
            ApiProviderStateItem(
                sample_id="sample-selfcheck",
                output_index=0,
                state=ProviderStateEnvelope(
                    provider="codex",
                    protocol="responses",
                    item_type="reasoning",
                    payload={
                        "id": "rs_selfcheck",
                        "type": "reasoning",
                        "encrypted_content": ciphertext,
                        "summary": [],
                        "status": "completed",
                    },
                ),
            ),
        ],
    )
    state_payload = build_codex_payload(
        state_request, default_model="gpt-5.6-luna"
    )
    if state_payload["input"][1].get("encrypted_content") != ciphertext:
        raise AssertionError("Codex provider state wire preflight failed")
    projection = project_attempt_request("codex", "gpt-5.6-luna", state_request)
    safe = json.dumps(projection.api_request_safe, sort_keys=True)
    capture = json.dumps(
        redact_sensitive_request_data(state_request.model_dump(mode="json")),
        sort_keys=True,
    )
    for encoded in (safe, capture):
        if attachment.content in encoded or ciphertext in encoded:
            raise AssertionError("Codex preflight leaked sensitive request content")
    kinds = {entry.kind for entry in projection.redactions}
    if kinds != {"image_base64", "provider_encrypted_content"}:
        raise AssertionError("Codex strict observer redaction manifest is incomplete")


__all__ = ["preflight_codex_image_matrix", "run_codex_image_matrix"]
