"""附件存储 demo（ADR 0148）—— 图片字节进独立存储，会话里只留引用（SimClient，无需 key）。

流程：
    1. 两个 pool 各收一张同样的图片：一个内联（默认），一个配了 ``LocalAttachmentStore``；
       打印两份 transcript 的大小 —— 后者不含图片正文；
    2. 配了存储的 thread 里，图片被 ``MultimodalEvictionStrategy`` 驱逐，描述里留下
       ``path=attachment:<ref>``；
    3. 模型用 ``file_read`` 读这个路径，下一次请求里图片回来了；
    4. 删掉存储里的对象，再问一轮：turn 不失败，模型看到占位文本，事件流里有
       ``attachment_unavailable``。

运行：
    PYTHONPATH=src uv run python examples/attachment_store/demo.py
"""

from __future__ import annotations

import asyncio
import struct
import sys
import tempfile
import zlib
from pathlib import Path

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.strategies import MultimodalEvictionStrategy
from taifeng.experimental import LocalAttachmentStore
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import ImagePart
from taifeng.tool.builtins.file_io import make_file_read_tool

SKILLS = Path(__file__).resolve().parent / "skills"
CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}), provider="sim", protocol="sim",
    tool_output_modalities=frozenset({"text", "image"}),
)
POLICY = ImageInputPolicy(
    enabled=True, max_images=2, max_item_bytes=512 * 1024, max_total_bytes=1024 * 1024,
    allowed_media_types=frozenset({"image/png"}),
)


def _png(width: int = 160, height: int = 120) -> bytes:
    """生成一张真实可解码的 RGB PNG（带噪声，使体积接近真实截图）。"""
    rows = b"".join(
        b"\x00" + bytes((x * 7 + y * 13 + (x * y) % 251) % 256 for x in range(width * 3))
        for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))


def _jsonl_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*.jsonl"))


def _image_count(request: object) -> int:
    return sum(
        isinstance(part, ImagePart)
        for message in request.messages  # type: ignore[attr-defined]
        if isinstance(message.content, list) for part in message.content
    )


async def _run(engine: taifeng.AgentEngine, text: str, attachments: list | None = None) -> list:
    """提交一轮并收集全部事件，直到根 turn 终态。"""
    events: list = []
    sub_id = await engine.submit(taifeng.UserMessage(text=text, attachments=attachments or []))
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        events.append(ev.msg)
        if ev.msg.kind in ("turn_completed", "turn_failed") and ev.msg.data.get("is_root", True):
            return events
    return events


async def main() -> int:
    """跑完四步，逐条打印核验结果；任一不满足返回 1。"""
    image = ImageAttachmentV1.from_bytes(_png(), media_type="image/png")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sizes = {}
        for label, store in (("inline", None), ("store", LocalAttachmentStore(root / "blobs"))):
            client = SimClient(turns=[SimTurn(text="收到")], capabilities=CAPS)
            pool = await taifeng.EnginePool.create(
                skills_dir=SKILLS, storage_dir=root / label, model_client=client,
                compressors=[], image_input_policy=POLICY, attachment_store=store,
            )
            engine = await pool.get_or_create(session_id=label, entry_skill_id="image-desk")
            await _run(engine, "这张图先收着", [image.model_dump()])
            await pool.close()
            sizes[label] = _jsonl_bytes(root / label)
        print(f"[1] 图片 {image.size} 字节；transcript：内联 {sizes['inline']} 字节，"
              f"引用 {sizes['store']} 字节")
        ok &= sizes["store"] < sizes["inline"] - image.size

        store = LocalAttachmentStore(root / "blobs2")
        ref = f"sha256-{image.sha256}"
        client = SimClient(capabilities=CAPS, turns=[
            SimTurn(text="收到"),
            SimTurn(tool_calls=[{"id": "r1", "name": "file_read",
                                 "arguments": f'{{"path": "attachment:{ref}"}}'}]),
            SimTurn(text="看到了：一张 160x120 的渐变图"),
            SimTurn(text="那张图我现在取不到了"),
        ])
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS, storage_dir=root / "flow", model_client=client,
            image_input_policy=POLICY, attachment_store=store, attachment_cache_bytes=0,
            extra_tools=[make_file_read_tool(root_dir=root, attach_images=True, attachment_store=store)],
            compressors=[MultimodalEvictionStrategy(
                trigger_ratio=1e-6, keep_recent=0, protect_tail_messages=2, allow_head_evict=True,
            )],
            budget=ContextBudget(context_window=200_000, soft_limit_ratio=0.001),
        )
        engine = await pool.get_or_create(session_id="flow", entry_skill_id="image-desk")
        await _run(engine, "这张图先收着", [image.model_dump()])
        await _run(engine, "再看一眼那张图")
        note = next(i for i in engine.history_snapshot() if i.kind == "user_message").payload["text"]
        evicted = f"path=attachment:{ref}" in note
        print(f"[2] 驱逐后的描述带引用路径：{evicted}")
        back = _image_count(client.ledger.requests()[2].request) == 1
        print(f"[3] file_read 之后的请求里图片回来了：{back}")

        for path in store.root.rglob(image.sha256):
            path.unlink()
        events = await _run(engine, "现在呢？")
        kinds = [msg.kind for msg in events]
        degraded = "attachment_unavailable" in kinds and kinds[-1] == "turn_completed"
        print(f"[4] 对象丢失后 turn 照常完成并发出 attachment_unavailable：{degraded}")
        await pool.close()
        ok &= evicted and back and degraded

    print("✅ attachment_store demo 通过" if ok else "核验未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
