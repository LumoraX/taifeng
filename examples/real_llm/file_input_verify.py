"""用户文件（PDF）输入的真实端点验证，provider 由 .env 决定（llm-file-input 契约）。

``test_codex_file_input.py`` 把 codex 的 PDF 输入固定进了能力矩阵；本脚本把同一个场景交给任意声明了
``"file"`` 能力的 provider（codex / openai / anthropic / gemini），用来验证它们各自的文件 wire：

- 纯标准库现拼一个只含随机核对码的单页 PDF，经 ``EnginePool(file_input_policy=...)`` 作为用户消息附件；
- 模型必须调工具登记核对码、并在回复里复述它——随机码每次不同，答对只能是真的读到了文件；
- request capture 与事件日志都不含文件正文。

运行：
    cd taifeng
    LLM_BOOTSTRAP_PROVIDER=gemini PYTHONPATH=src uv run python examples/real_llm/file_input_verify.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import anyio  # noqa: E402
import test_codex_file_input as scenario  # noqa: E402
import test_openai_image_matrix as shared  # noqa: E402

import taifeng  # noqa: E402
from taifeng import FileAttachmentV1  # noqa: E402
from taifeng.llm.client import model_capabilities  # noqa: E402
from taifeng.telemetry.jsonl_sink import attach_jsonl_sink  # noqa: E402


async def main() -> int:
    try:
        client, meta = build_model_client(retry=False, timeout_seconds=180.0)
    except ProviderBootstrapError as exc:
        print(f"❌ 无法构造客户端：{exc}")
        return 2
    provider, model = str(meta.get("provider")), str(meta.get("model"))
    print(f"provider={provider} model={model}")
    if "file" not in model_capabilities(client).input_modalities:
        print(f"provider={provider} 的客户端没有声明 file 能力，不适用本验证。")
        return 2

    code = scenario._random_code()  # noqa: SLF001
    attachment = FileAttachmentV1.from_bytes(scenario.minimal_pdf(code), filename="verification.pdf")
    observed: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pool = await taifeng.EnginePool.create(
            skills_dir=scenario.SKILLS_DIR, storage_dir=root / "store", model_client=client,
            extra_tools=[scenario._record_tool(observed)],  # noqa: SLF001
            compressors=[], enable_request_capture=True,
            file_input_policy=scenario._policy(),  # noqa: SLF001
        )
        event_log = root / "events.jsonl"
        try:
            engine = await pool.get_or_create(
                session_id="file-input-real", entry_skill_id="document-reader")
            attach_jsonl_sink(engine, event_log)
            events, capture = await shared._capture_submission(  # noqa: SLF001
                engine,
                taifeng.UserMessage(
                    text="Read the attached PDF and register the verification code printed in it.",
                    attachments=[attachment.model_dump()],
                ),
            )
            items = await shared._load_items(pool, engine.thread_id)  # noqa: SLF001
        finally:
            await pool.close()
        event_text = await anyio.Path(event_log).read_text(encoding="utf-8")

    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(f"  {'✅' if condition else '❌'} {label}")
        if not condition:
            failures.append(label)

    terminal = [m.kind for m in events if m.kind in ("turn_completed", "turn_failed")]
    check(terminal[-1:] == ["turn_completed"], f"端点接受带 PDF 的请求，turn 完成（{terminal}）")
    check([entry.get("code") for entry in observed] == [code],
          f"模型经工具登记的核对码与 PDF 里的随机码逐字一致（{observed}）")
    reply = next((str(i.payload.get("text", "")) for i in reversed(items)
                  if i.kind == "assistant_message"), "")
    check(code in reply, "最终回复复述了核对码")
    try:
        scenario._assert_safe(capture, event_text, attachment.content)  # noqa: SLF001
        safe = True
    except AssertionError as exc:
        print(f"     {exc}")
        safe = False
    check(safe, "request capture 与事件日志不含文件正文")
    check(shared._turn_usage(events) > 0, "真实 usage 非零")  # noqa: SLF001
    print("✅ 全部通过" if not failures else f"❌ 失败 {len(failures)} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
