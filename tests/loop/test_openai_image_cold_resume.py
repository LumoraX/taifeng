"""OpenAI 图片与 Responses 状态跨 JSONL 冷恢复的 wire 等价测试。"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.conversation.models import (
    assistant_message,
    function_call,
    function_call_output,
    reasoning,
    suspension_item,
    user_message,
)
from taifeng.llm.errors import InvalidHistoryError
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers.openai.chat import OpenAIChatClient
from taifeng.llm.providers.openai.responses import OpenAIResponsesClient
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.prompt import build_api_request

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.skill.models import SkillDefinition, SkillSnapshot

    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.client import ModelCapabilities

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_PNG_BASE64 = base64.b64encode(_PNG).decode("ascii")


def _attachment() -> dict[str, object]:
    """构造可通过 canonical admission 的 1x1 PNG。"""
    return ImageAttachmentV1(
        media_type="image/png",
        size=len(_PNG),
        sha256=hashlib.sha256(_PNG).hexdigest(),
        content=_PNG_BASE64,
        detail="high",
    ).model_dump()


def _policy() -> ImageInputPolicy:
    """业务显式启用的有界图片策略。"""
    return ImageInputPolicy(
        enabled=True,
        max_images=2,
        max_item_bytes=1024,
        max_total_bytes=2048,
        allowed_media_types=frozenset({"image/png"}),
    )


async def _skill(skills_dir: Path) -> tuple[SkillDefinition, SkillSnapshot]:
    """加载测试 skill，供 hot/cold 请求使用同一 prompt 快照。"""
    from taifeng.skill.registry import FilesystemSkillRegistry

    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()
    entry = snapshot.get("code-reviewer")
    assert entry is not None
    return entry, snapshot


def _request(
    history: list[ResponseItem],
    *,
    entry: SkillDefinition,
    snapshot: SkillSnapshot,
    capabilities: ModelCapabilities,
) -> object:
    """从 durable history 构建 protocol-neutral 请求。"""
    return build_api_request(
        entry=entry,
        snapshot=snapshot,
        history=history,
        tools=[],
        model="gpt-5.6",
        image_input_policy=_policy(),
        model_input_capabilities=capabilities,
    )


async def _load(store: object, thread_id: str) -> list[ResponseItem]:
    """读取一个 thread 的全部可见项。"""
    stream = await store.load_thread(thread_id)  # type: ignore[attr-defined]
    return [item async for item in stream]


@pytest.mark.asyncio
async def test_chat_image_payload_is_identical_after_engine_cold_resume(
    skills_dir: Path, threads_dir: Path
) -> None:
    """Chat 从 JSONL 重载后必须生成完全相同的 Data URL payload。"""
    client = OpenAIChatClient(api_key="sk-test", model="gpt-5.6")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        image_input_policy=_policy(),
    )
    engine = await pool.get_or_create(session_id="chat-image", entry_skill_id="code-reviewer")
    item = user_message(
        "inspect inventory",
        thread_id=engine.thread_id,
        attachments=[_attachment()],
    )
    await pool.store.append(item)
    hot_history = await _load(pool.store, engine.thread_id)
    entry, snapshot = await _skill(skills_dir)
    hot_request = _request(
        hot_history, entry=entry, snapshot=snapshot, capabilities=client.capabilities
    )
    hot_payload = client.session(cancel=CancellationToken())._build_payload(hot_request)
    thread_id = engine.thread_id
    await pool.close()

    cold_client = OpenAIChatClient(api_key="sk-test", model="gpt-5.6")
    cold_pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=cold_client,
        compressors=[],
        image_input_policy=_policy(),
    )
    cold_engine = await cold_pool.get_or_create(
        session_id="chat-image-cold",
        entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    cold_request = _request(
        cold_engine.history_snapshot(),
        entry=entry,
        snapshot=snapshot,
        capabilities=cold_client.capabilities,
    )
    cold_payload = cold_client.session(cancel=CancellationToken())._build_payload(cold_request)
    await cold_pool.close()

    assert cold_payload == hot_payload
    assert cold_payload["messages"][-1]["content"][1] == {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/png;base64,{_PNG_BASE64}",
            "detail": "high",
        },
    }


def _responses_history(thread_id: str) -> list[ResponseItem]:
    """构造完整 Responses sample 及已核销工具结果。"""
    user = user_message(
        "inspect inventory",
        thread_id=thread_id,
        attachments=[_attachment()],
    )
    state = reasoning("", thread_id=thread_id, summary="检查图片")
    state.payload["provider_state"] = {
        "provider": "openai",
        "protocol": "responses",
        "item_type": "reasoning",
        "payload": {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "encrypted-state",
            "summary": [{"type": "summary_text", "text": "检查图片"}],
            "status": "completed",
        },
    }
    state.metadata = {"llm_sample_id": "sample-1", "provider_output_index": 0}
    message = assistant_message("库存 A-17", thread_id=thread_id, model="gpt-5.6")
    message.metadata = {"llm_sample_id": "sample-1", "provider_output_index": 1}
    call = function_call("call-1", "inspect", '{"id":"A-17"}', thread_id=thread_id)
    call.metadata = {"llm_sample_id": "sample-1", "provider_output_index": 2}
    output = function_call_output(
        "call-1", '{"ok":true}', thread_id=thread_id, is_error=False
    )
    output.metadata = {"origin_llm_sample_id": "sample-1"}
    return [user, state, message, call, output]


@pytest.mark.asyncio
async def test_responses_ordered_payload_is_identical_after_engine_cold_resume(
    skills_dir: Path, threads_dir: Path
) -> None:
    """Responses 冷恢复必须原位重放 state/call/output 与图片顺序。"""
    client = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        image_input_policy=_policy(),
    )
    engine = await pool.get_or_create(
        session_id="responses-image", entry_skill_id="code-reviewer"
    )
    user, *sample, output = _responses_history(engine.thread_id)
    await pool.store.append(user)
    await pool.store.append_atomic_batch(sample, batch_id="sample-1")
    await pool.store.append(output)
    hot_history = await _load(pool.store, engine.thread_id)
    entry, snapshot = await _skill(skills_dir)
    hot_request = _request(
        hot_history, entry=entry, snapshot=snapshot, capabilities=client.capabilities
    )
    hot_payload = client.session(cancel=CancellationToken())._build_payload(hot_request)
    thread_id = engine.thread_id
    await pool.close()

    cold_client = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    cold_pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=cold_client,
        compressors=[],
        image_input_policy=_policy(),
    )
    cold_engine = await cold_pool.get_or_create(
        session_id="responses-image-cold",
        entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    cold_request = _request(
        cold_engine.history_snapshot(),
        entry=entry,
        snapshot=snapshot,
        capabilities=cold_client.capabilities,
    )
    cold_payload = cold_client.session(cancel=CancellationToken())._build_payload(cold_request)
    await cold_pool.close()

    assert cold_payload == hot_payload
    assert [item["type"] for item in cold_payload["input"][1:]] == [
        "message",
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert cold_payload["input"][1]["content"][1]["type"] == "input_image"
    assert cold_payload["input"][2]["encrypted_content"] == "encrypted-state"
    assert cold_payload["input"][4]["call_id"] == "call-1"
    assert cold_payload["input"][5]["call_id"] == "call-1"


@pytest.mark.asyncio
async def test_chat_cold_resume_rejects_responses_provider_state(
    skills_dir: Path, threads_dir: Path
) -> None:
    """Chat 恢复到 Responses 密文状态时必须 fail closed。"""
    responses = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=responses,
        compressors=[],
        image_input_policy=_policy(),
    )
    engine = await pool.get_or_create(
        session_id="foreign-state", entry_skill_id="code-reviewer"
    )
    user, state, *_ = _responses_history(engine.thread_id)
    await pool.store.append(user)
    await pool.store.append_atomic_batch([state], batch_id="sample-1")
    thread_id = engine.thread_id
    await pool.close()

    chat = OpenAIChatClient(api_key="sk-test", model="gpt-5.6")
    cold_pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=chat,
        compressors=[],
        image_input_policy=_policy(),
    )
    cold_engine = await cold_pool.get_or_create(
        session_id="foreign-state-cold",
        entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    entry, snapshot = await _skill(skills_dir)

    with pytest.raises(InvalidHistoryError):
        _request(
            cold_engine.history_snapshot(),
            entry=entry,
            snapshot=snapshot,
            capabilities=chat.capabilities,
        )
    await cold_pool.close()


@pytest.mark.asyncio
async def test_cold_resume_settles_orphan_response_call_as_unknown_without_retry(
    skills_dir: Path,
    threads_dir: Path,
) -> None:
    """冷恢复只追加稳定 unknown output，绝不重放结果未知的工具调用。"""
    client = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="orphan-call",
        entry_skill_id="code-reviewer",
    )
    call = function_call(
        "call-unknown",
        "inspect",
        '{"id":"A-17"}',
        thread_id=engine.thread_id,
    )
    call.metadata = {"llm_sample_id": "sample-unknown", "provider_output_index": 0}
    await pool.store.append_atomic_batch([call], batch_id="sample-unknown")
    thread_id = engine.thread_id
    await pool.close()

    cold_pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6"),
        compressors=[],
    )
    cold_engine = await cold_pool.get_or_create(
        session_id="orphan-call-cold",
        entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    history = cold_engine.history_snapshot()
    await cold_pool.close()

    assert [item.kind for item in history] == [
        "function_call",
        "function_call_output",
    ]
    recovered = history[-1]
    assert recovered.payload == {
        "call_id": "call-unknown",
        "output": "tool outcome unknown after process recovery; not retried",
        "is_error": True,
    }
    assert recovered.metadata["origin_llm_sample_id"] == "sample-unknown"

    second_pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6"),
        compressors=[],
    )
    second_engine = await second_pool.get_or_create(
        session_id="orphan-call-cold-again",
        entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    second_history = second_engine.history_snapshot()
    await second_pool.close()

    assert sum(item.kind == "function_call_output" for item in second_history) == 1


@pytest.mark.asyncio
async def test_cold_resume_preserves_tool_call_owned_by_active_suspension(
    skills_dir: Path,
    threads_dir: Path,
) -> None:
    """活跃 suspension 所属 call 仍等待业务裁决，不得被 unknown recovery 核销。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6"),
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="suspended-call",
        entry_skill_id="code-reviewer",
    )
    call = function_call("call-waiting", "inspect", "{}", thread_id=engine.thread_id)
    call.metadata = {"llm_sample_id": "sample-waiting", "provider_output_index": 0}
    await pool.store.append_atomic_batch([call], batch_id="sample-waiting")
    await pool.store.append(
        suspension_item(
            record_id="suspension-1",
            submission_id="submission-1",
            turn_index=1,
            pending=[
                {
                    "request_id": "request-1",
                    "reason": "permission",
                    "payload_schema": {},
                    "related_call_id": "call-waiting",
                    "detail": {},
                    "ttl_seconds": None,
                    "on_expire": "abort",
                }
            ],
            created_at=1,
            thread_id=engine.thread_id,
        )
    )
    thread_id = engine.thread_id
    await pool.close()

    cold_pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6"),
        compressors=[],
    )
    cold_engine = await cold_pool.get_or_create(
        session_id="suspended-call-cold",
        entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    history = cold_engine.history_snapshot()
    await cold_pool.close()

    assert [item.kind for item in history] == ["function_call", "suspension"]
