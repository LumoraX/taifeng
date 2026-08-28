"""审计通道引擎级端到端：经真实 AgentEngine 跑完整 turn，断言 durable 落账。

补齐 helper 级测试（test_audit_tool.py / test_audit_skill.py 直接调 audited_tool_batch
/ AuditedSkillDispatch）与引擎级之间的最后一层缺口——**从引擎入口 UserMessage 提交
→ TurnRunner audit 分支 → 真实 JsonlSessionJournalCore durable 落账**的贯通验证。

用真实公有工厂 ``EnginePool.create(audit=...)``：真实内置工具（read_skill / call_skill
按内核默认 offered）、真实 journal core、真实 projector，SimClient 仅替代 provider。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
from typing import TYPE_CHECKING

import httpx
import pytest

import taifeng
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.records import (
    ConversationItemV1,
    deserialize_response_item,
)
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers.openai.responses import OpenAIResponsesClient
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_config import AuditConfig

if TYPE_CHECKING:
    from pathlib import Path


def _observed(client: SimClient) -> AttemptObservableClientAdapter:
    """把 SimClient 包成审计路径要求的官方 attempt-observer adapter。"""
    return AttemptObservableClientAdapter(
        client, provider="sim", default_model="sim-model"
    )


def _audit_config(core: JsonlSessionJournalCore) -> AuditConfig:
    """构造最小合法 strict audit 配置（有界附件上限）。"""
    return AuditConfig(
        journal_core=core,
        writer_id="writer-e2e",
        max_attachment_bytes=65536,
        max_total_attachment_bytes=1048576,
    )


def _responses_sse(response_id: str, output: list[dict[str, object]]) -> bytes:
    """构造只依赖 terminal truth 的 Responses SSE。"""
    event = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "model": "gpt-5.6-2026-08-01",
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        },
    }
    return f"data: {json.dumps(event)}\n\n".encode()


def _image_attachment() -> dict[str, object]:
    """构造 strict audit 与图片 admission 均认可的 1x1 PNG。"""
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    return ImageAttachmentV1(
        media_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"),
        detail="high",
    ).model_dump()


async def _run_until_root_done(
    engine: taifeng.AgentEngine,
    text: str,
    *,
    deadline_seconds: float = 10.0,
) -> str:
    """提交一轮并等**最外层根 turn** 终态，返回 turn_completed / turn_failed。

    call_skill 派生的子 sub-turn 亦发 ``turn_completed``（``is_root=False``），且比父
    entry 更早 emit——``engine.subscribe(sub_id)`` 会在首个 turn_completed 即早退（见
    tests/skill/test_composite_e2e.py 注释）。故改用 ``subscribe_all()`` 后台收集，仅在
    终态事件带 ``is_root=True``（最外层 entry turn）时退出，确保父 turn 完整收敛。
    """
    return await _run_op_until_root_done(
        engine,
        taifeng.UserMessage(text=text),
        deadline_seconds=deadline_seconds,
    )


async def _run_op_until_root_done(
    engine: taifeng.AgentEngine,
    op: taifeng.UserMessage,
    *,
    deadline_seconds: float = 10.0,
) -> str:
    """在提交任意 UserMessage 前注册 collector，并等待根 turn 终态。"""
    result: list[str] = []
    done = asyncio.Event()
    sub_holder: list[str] = []

    async def collector() -> None:
        async for ev in engine.subscribe_all():
            if not sub_holder or ev.submission_id != sub_holder[0]:
                continue
            if ev.msg.kind in ("turn_completed", "turn_failed") and ev.msg.data.get(
                "is_root"
            ):
                result.append(ev.msg.kind)
                done.set()
                return

    task = asyncio.create_task(collector())
    await asyncio.sleep(0)  # 让 collector 先注册 subscribe_all 队列
    sub_holder.append(await engine.submit(op))
    try:
        await asyncio.wait_for(done.wait(), timeout=deadline_seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    assert result, "未收到根 turn 终态事件"
    return result[0]


@pytest.mark.asyncio
async def test_engine_call_skill_turn_durably_records_tool_and_skill_lineage(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """引擎级 call_skill turn：durable 落账 tool 收敛 + 完整子 skill 谱系。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    client = _observed(
        SimClient(
            turns=[
                # 父 entry：LLM 决定 call_skill 派发子专科 style-checker
                SimTurn(
                    text="派发风格审查子技能",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "call_skill",
                            "arguments": (
                                '{"skill_id": "style-checker", '
                                '"reason": "审查代码风格"}'
                            ),
                        }
                    ],
                ),
                # 子 style-checker（atomic）：一轮文本结论
                SimTurn(text="风格审查完成：未见违规"),
                # 父 entry：拿到子结果后综合
                SimTurn(text="综合审查结论：通过"),
            ]
        )
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        audit=_audit_config(core),
    )
    engine = await pool.get_or_create(
        session_id="ses-e2e", entry_skill_id="code-reviewer"
    )
    assert await _run_until_root_done(engine, "请审查这段 diff") == "turn_completed"
    await pool.close()

    committed = [e async for e in core.load("ses-e2e")]
    types = [e.record_type for e in committed]

    # ---- 存在性：tool 收敛 + 完整子 skill 谱系全部 durable 落账 ----
    # tool 收敛：call_skill 恰一 intent + 恰一 outcome（§8 唯一终态）
    assert types.count("tool_intent_committed") == 1
    assert types.count("tool_outcome_committed") == 1
    # 子 skill 谱系：selected → started 批 → finished 批（§9 全谱系）
    for lineage in (
        "skill_selected",
        "skill_dispatch_started",
        "skill_dispatch_finished",
    ):
        assert types.count(lineage) == 1, lineage
    # 每次 LLM 采样都 checkpoint-before-commit（父2轮 + 子1轮 = 3 次）
    assert types.count("llm_response_checkpoint") == 3
    assert types.count("llm_response_committed") == 3

    # ---- 顺序不变式：审计正确性的核心约束 ----
    def _idx(record_type: str) -> int:
        return types.index(record_type)

    # 意图先于任何效果：tool_intent 先于子 skill 派发
    assert _idx("tool_intent_committed") < _idx("skill_selected")
    # 子 skill 先完成，父 call_skill 才收敛（同步派发语义）
    assert _idx("skill_dispatch_finished") < _idx("tool_outcome_committed")
    # 每个 llm_response_committed 都紧跟其 checkpoint（checkpoint-before-delta）
    committed_idxs = [
        i for i, t in enumerate(types) if t == "llm_response_committed"
    ]
    for ci in committed_idxs:
        assert types[ci - 1] == "llm_response_checkpoint", types[ci - 2 : ci + 1]


@pytest.mark.asyncio
async def test_engine_plain_turn_durably_records_llm_commit_without_tool_records(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """引擎级纯文本 turn：只 durable 落 LLM 提交 + 会话项，无任何 tool/skill 记录。

    锁定审计通道最小 turn 形状（无工具时不得凭空产生 tool_intent/outcome），与
    call_skill 用例互补隔离出「干净采样」路径。
    """
    core = JsonlSessionJournalCore(tmp_path / "journal")
    client = _observed(SimClient(turns=[SimTurn(text="直接回答，无需派发")]))
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        audit=_audit_config(core),
    )
    engine = await pool.get_or_create(
        session_id="ses-plain", entry_skill_id="code-reviewer"
    )
    assert await _run_until_root_done(engine, "你好") == "turn_completed"
    await pool.close()

    types = [e.record_type async for e in core.load("ses-plain")]
    # 干净采样一轮：checkpoint-before-commit 一对
    assert types.count("llm_response_checkpoint") == 1
    assert types.count("llm_response_committed") == 1
    # 无工具/子 skill：绝不凭空产生任何 tool/skill 谱系记录
    for forbidden in (
        "tool_intent_committed",
        "tool_outcome_committed",
        "skill_selected",
        "skill_dispatch_started",
        "skill_dispatch_finished",
    ):
        assert forbidden not in types, forbidden
    # 初始化与收尾骨架齐全
    assert types[:3] == ["session_started", "thread_created", "thread_bound"]
    assert types[-1] == "session_ended"


@pytest.mark.asyncio
async def test_openai_responses_image_state_and_tool_origin_survive_strict_audit(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实际 Responses adapter 经 strict audit 保留图片、密文与 tool sample 谱系。"""
    bodies = [
        _responses_sse(
            "resp-1",
            [
                {
                    "id": "rs-1",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "读取 skill"}],
                    "encrypted_content": "ciphertext",
                    "status": "completed",
                },
                {
                    "id": "fc-1",
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read_skill",
                    "arguments": '{"skill_id":"style-checker"}',
                    "status": "completed",
                },
            ],
        ),
        _responses_sse(
            "resp-2",
            [
                {
                    "id": "msg-2",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "检查完成"}],
                    "status": "completed",
                }
            ],
        ),
    ]
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=bodies[len(requests) - 1])

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    core = JsonlSessionJournalCore(tmp_path / "journal-responses")
    inner = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    client = AttemptObservableClientAdapter(
        inner, provider="openai", default_model="gpt-5.6"
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "threads-responses",
        model_client=client,
        compressors=[],
        audit=_audit_config(core),
        image_input_policy=ImageInputPolicy(
            enabled=True,
            max_images=1,
            max_item_bytes=65536,
            max_total_bytes=65536,
            allowed_media_types=frozenset({"image/png"}),
        ),
    )
    engine = await pool.get_or_create(
        session_id="ses-responses", entry_skill_id="code-reviewer"
    )
    result = await _run_op_until_root_done(
        engine,
        taifeng.UserMessage(text="检查图片", attachments=[_image_attachment()]),
    )
    assert result == "turn_completed"
    await pool.close()

    committed = [entry async for entry in core.load("ses-responses")]
    items = [
        deserialize_response_item(ConversationItemV1.model_validate(entry.payload))
        for entry in committed
        if entry.record_type == "conversation_item"
    ]
    reasoning = next(item for item in items if item.kind == "reasoning")
    call = next(item for item in items if item.kind == "function_call")
    output = next(item for item in items if item.kind == "function_call_output")
    request_record = next(
        entry for entry in committed if entry.record_type == "llm_request_committed"
    )
    expected_sample_id = (
        f"{engine.thread_id}:{request_record.submission_id}:"
        f"turn:{request_record.payload['turn_index']}:"
        f"llm:{request_record.payload['iteration']}"
    )
    assert reasoning.payload["provider_state"]["payload"]["encrypted_content"] == "ciphertext"
    assert reasoning.metadata["llm_sample_id"] == expected_sample_id
    assert call.metadata["llm_sample_id"] == reasoning.metadata["llm_sample_id"]
    assert output.metadata["origin_llm_sample_id"] == call.metadata["llm_sample_id"]
    assert requests[0]["input"][1]["content"][1]["type"] == "input_image"
    assert [item["type"] for item in requests[1]["input"][-3:]] == [
        "reasoning",
        "function_call",
        "function_call_output",
    ]
