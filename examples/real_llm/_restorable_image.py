"""被压缩移出上下文的图片可读回（restorable-attachments，ADR 0147）—— 真实模型验证。

两轮对话：
    1. 用户发来库存标签图片，模型按 skill 约定只回「收到」，不读出序列号；
    2. 图片已被 ``MultimodalEvictionStrategy`` 换成带路径的描述，用户问序列号。

判定（四条全满足才 PASS）：
    - 第 1 轮回答里没有序列号 —— 否则第 2 轮可以凭自己说过的话作答，证明不了读回；
    - 发生过一次带落盘的附件驱逐（``compaction_completed``）；
    - 第 2 轮模型调用 ``file_read`` 读了 ``_offload/`` 下的路径，结果带回与原件摘要一致的图片；
    - 第 2 轮回答里有序列号。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

import test_openai_image_matrix as shared

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.strategies import MultimodalEvictionStrategy
from taifeng.llm.image_input import OpenAIImageCostEstimator
from taifeng.telemetry.jsonl_sink import attach_jsonl_sink
from taifeng.tool.builtins.file_io import make_file_read_tool

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.llm.client import ModelClient

SKILLS_DIR = shared.HERE / "fixtures" / "restorable_image_skills"
CAPABILITY = "被驱逐的图片落盘留路径，模型用 file_read 读回原图（ADR 0147）"


def _last_assistant_text(items: list[Any]) -> str:
    """thread 里最后一条助手正文。"""
    return next(
        (str(item.payload.get("text", "")) for item in reversed(items)
         if item.kind == "assistant_message"),
        "",
    )


def _offload_reads(events: list[Any]) -> list[str]:
    """本轮 file_read 读过的 ``_offload/`` 路径。"""
    paths: list[str] = []
    for msg in events:
        if msg.kind != "tool_call_started" or msg.data.get("name") != "file_read":
            continue
        try:
            path = json.loads(msg.data.get("arguments") or "{}").get("path", "")
        except json.JSONDecodeError:
            continue
        if isinstance(path, str) and path.startswith("_offload/"):
            paths.append(path)
    return paths


async def _run(client: ModelClient, root: Path) -> Counter[str]:
    """跑两轮并逐条核对判定；任一条不满足抛 AssertionError。"""
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    pool = await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        storage_dir=root / "store",
        model_client=client,
        extra_tools=[make_file_read_tool(root_dir=workspace, attach_images=True)],
        compressors=[
            # 策略自身阈值也压到最低：只要有不在尾部保护内的带图条目就驱逐。实测第 1 轮
            # 采样前图片就已被驱逐（用户消息之后还有一条预算提示，尾部保护 1 条护不到它）——
            # 模型在第 2 轮只能靠读回原件作答，这正是本场景要证明的。
            MultimodalEvictionStrategy(
                trigger_ratio=1e-6, keep_recent=0, protect_tail_messages=1,
                allow_head_evict=True, file_root=workspace,
            ),
        ],
        # 回合内的压缩检查以软阈值为闸（低于它任何策略都不会被询问），这里把闸压到最低，
        # 让一张图片的体量就足以进入检查；窗口本身保持正常大小。
        budget=ContextBudget(context_window=200_000, soft_limit_ratio=0.001),
        image_input_policy=shared._policy(),  # noqa: SLF001 —— 同一份真实矩阵图片策略
        input_cost_estimator=OpenAIImageCostEstimator(),
    )
    attachment = shared._fixture_attachment()  # noqa: SLF001
    try:
        engine = await pool.get_or_create(
            session_id="restorable-image", entry_skill_id="image-keeper",
        )
        attach_jsonl_sink(engine, root / "events.jsonl")
        first, _ = await shared._capture_submission(  # noqa: SLF001
            engine,
            taifeng.UserMessage(
                text="这张图片先收着，回复「收到」即可，现在不要读里面的内容。",
                attachments=[attachment.model_dump()],
            ),
        )
        items = await shared._load_items(pool, engine.thread_id)  # noqa: SLF001
        first_text = _last_assistant_text(items)
        if shared.SERIAL in first_text.upper().replace(" ", ""):
            raise AssertionError("turn 1 already read out the serial; read-back is unprovable")

        second, _ = await shared._capture_submission(  # noqa: SLF001
            engine,
            taifeng.UserMessage(text="现在告诉我：刚才那张图片上的库存序列号是多少？"),
        )
        events = [*first, *second]
        evictions = [
            msg for msg in events
            if msg.kind == "compaction_completed"
            and (msg.data.get("detail") or {}).get("offloaded_attachments", 0) >= 1
        ]
        if not evictions:
            raise AssertionError("no eviction with a saved attachment was observed")
        reads = _offload_reads(second)
        if not reads:
            raise AssertionError("turn 2 did not file_read an _offload/ path")
        if not (workspace / reads[0]).is_file():
            raise AssertionError(f"read path does not exist in workspace: {reads[0]}")
        items = await shared._load_items(pool, engine.thread_id)  # noqa: SLF001
        returned = [
            image.get("sha256")
            for item in items if item.kind == "function_call_output"
            for image in item.payload.get("attachments") or []
        ]
        if attachment.sha256 not in returned:
            raise AssertionError("file_read result did not carry the original image")
        shared._assert_serial(_last_assistant_text(items))  # noqa: SLF001
        log_text = (root / "events.jsonl").read_text(encoding="utf-8")
        if attachment.content in log_text:
            raise AssertionError("telemetry JSONL leaked image body")
        kinds = Counter(msg.kind for msg in events)
        kinds.update(attachment_evicted_to_disk=1, image_read_back=1)
        return kinds
    finally:
        await pool.close()


async def run_restorable_image(
    client: ModelClient, *, logs_dir: Path, scenario_id: str,
) -> shared.ImageMatrixResult:
    """运行场景并收敛为台账结果（失败不上抛，记 FAIL）。"""
    return await shared._record_result(  # noqa: SLF001
        scenario_id, CAPABILITY, lambda: _run(client, logs_dir / scenario_id),
    )
