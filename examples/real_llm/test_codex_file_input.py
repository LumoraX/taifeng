"""Codex 文件（PDF）输入真实验证 + 零消耗 wire 预检（llm-file-input 契约）。

真实场景 ``codex_file_input``：纯标准库现拼一个只含唯一随机核对码的单页 PDF，经
``EnginePool(file_input_policy=...)`` 提交 ``UserMessage(attachments=[FileAttachmentV1])``，
走完整条真实路径（入队 admission → JSONL 落盘 → prompt 重建 → codex ``input_file`` wire →
模型阅读 → 工具登记）。断言只看：

- 模型读出的核对码与 PDF 里的随机码逐字一致（工具参数 + 最终回复双重证据）——随机码
  每次不同，模型不可能凭先验答对，能区分「端点收下了文件」与「模型读到了文件」；
- request capture / JSONL 事件日志都不含文件 base64 正文或 Data URL（脱敏）；
- 真实 usage 非零。

用法（需真实 key；脚本自读 .env 的 ``LLM_BOOTSTRAP_*``，provider 须为 codex）::

    LLM_BOOTSTRAP_PROVIDER=codex PYTHONPATH=src uv run python \\
        examples/real_llm/test_codex_file_input.py

``capability_matrix.py --provider codex`` 全量跑时本场景作为 provider 专属场景一并执行。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import test_openai_image_matrix as shared

import taifeng
from taifeng.llm.file_input import FileAttachmentV1, FileInputPolicy
from taifeng.llm.image_input import redact_sensitive_request_data
from taifeng.llm.providers.codex import CodexResponsesClient
from taifeng.llm.providers.codex.wire import build_codex_payload
from taifeng.llm.types import ApiMessageItem, ApiRequest, TextPart
from taifeng.telemetry.jsonl_sink import attach_jsonl_sink
from taifeng.tool.spec import ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.tool.spec import ToolContext

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE / "fixtures" / "file_input_skills"
ImageMatrixResult = shared.ImageMatrixResult


def minimal_pdf(text: str) -> bytes:
    """纯标准库拼一个单页、只含一行 ASCII 文本的最小合法 PDF（xref 偏移精确）。"""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 28 Tf 72 700 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_offset = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_offset,
    )
    return bytes(out)


def _random_code() -> str:
    """每次运行唯一的核对码：模型不可能凭先验或缓存答对。"""
    return f"TF-{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}"


def _policy() -> FileInputPolicy:
    """真实验证使用的显式、有限文件业务策略。"""
    return FileInputPolicy(enabled=True, max_files=1, max_item_bytes=256 * 1024,
                           max_total_bytes=256 * 1024)


def _record_tool(observed: list[dict[str, Any]]) -> ToolSpec:
    """纯内存登记工具：记录模型读出的核对码（工具参数是结构化证据）。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx
        observed.append(dict(args))
        return ToolResult.ok(json.dumps({"accepted": True, **args}, ensure_ascii=False))

    return ToolSpec(
        name="record_document_code",
        description="Record the exact verification code printed in the attached document.",
        input_schema={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def _assert_safe(capture: dict[str, Any] | None, event_text: str, body: str) -> None:
    """request capture 与事件日志都不得出现文件正文或 Data URL。"""
    if capture is None:
        raise AssertionError("llm_request_recorded was not observed")
    encoded = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    if body in encoded or "base64_data" in encoded or "data:application/pdf" in encoded:
        raise AssertionError("request capture leaked file body")
    if '"content_redacted": true' not in encoded:
        raise AssertionError("request capture omitted file redaction descriptor")
    if body in event_text or "data:application/pdf" in event_text:
        raise AssertionError("telemetry leaked file body")


async def _run_file_input(
    *, api_key: str, model: str, base_url: str, root: Path
) -> Counter[str]:
    """真实 EnginePool 端到端：PDF 附件 → 模型读出随机核对码并登记。"""
    code = _random_code()
    attachment = FileAttachmentV1.from_bytes(minimal_pdf(code), filename="verification.pdf")
    observed: list[dict[str, Any]] = []
    pool = await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        storage_dir=root / "store",
        model_client=CodexResponsesClient(
            api_key=api_key, model=model, base_url=base_url, timeout_seconds=180.0
        ),
        extra_tools=[_record_tool(observed)],
        compressors=[],
        enable_request_capture=True,
        file_input_policy=_policy(),
    )
    event_log = root / "events-file.jsonl"
    try:
        engine = await pool.get_or_create(
            session_id="codex-file-real", entry_skill_id="document-reader"
        )
        attach_jsonl_sink(engine, event_log)
        events, capture = await shared._capture_submission(
            engine,
            taifeng.UserMessage(
                text="Read the attached PDF and register the verification code printed in it.",
                attachments=[attachment.model_dump()],
            ),
        )
        items = await shared._load_items(pool, engine.thread_id)
    finally:
        await pool.close()
    event_text = await anyio.Path(event_log).read_text(encoding="utf-8")
    _assert_safe(capture, event_text, attachment.content)
    if [entry.get("code") for entry in observed] != [code]:
        raise AssertionError(f"tool did not register the exact code: {observed!r} != {code!r}")
    reply = next(
        str(item.payload.get("text", ""))
        for item in reversed(items)
        if item.kind == "assistant_message"
    )
    if code not in reply:
        raise AssertionError(f"final reply did not repeat the code: {reply!r}")
    stored = next(item for item in items if item.kind == "user_message")
    if stored.payload.get("attachments") != [attachment.model_dump()]:
        raise AssertionError("canonical file attachment was not persisted verbatim")
    if shared._turn_usage(events) <= 0:
        raise AssertionError("file turn usage was missing")
    kinds = Counter(message.kind for message in events)
    kinds.update(provider_codex=1, file_input_verified=1, file_tool_executed=1)
    return kinds


async def run_codex_file_matrix(
    *, api_key: str, model: str, base_url: str, logs_dir: Path
) -> list[ImageMatrixResult]:
    """运行 Codex 文件输入真实场景，异常收敛为稳定 FAIL（与图片矩阵同一台账形状）。"""
    started = time.monotonic()
    result = ImageMatrixResult(
        scenario_id="codex_file_input",
        capability="Codex 文件（PDF）输入：随机核对码读出 + 登记 + 脱敏",
    )
    try:
        result.kinds = await _run_file_input(
            api_key=api_key, model=model, base_url=base_url, root=logs_dir / "codex-file"
        )
    except Exception as exc:  # noqa: BLE001 —— 真实场景失败如实记 FAIL
        result.verdict = "FAIL"
        result.note = f"{type(exc).__name__}: {exc}"[:240]
    result.duration_s = time.monotonic() - started
    return [result]


def preflight_codex_file_input() -> None:
    """零消耗预检：PDF 结构、codex input_file wire 形状与 capture 脱敏。"""
    code = _random_code()
    attachment = FileAttachmentV1.from_bytes(minimal_pdf(code), filename="verification.pdf")
    request = ApiRequest(
        model="gpt-5.6-luna",
        input_items=[
            ApiMessageItem(role="user", content=[TextPart(text="read"), attachment.to_part()])
        ],
    )
    payload = build_codex_payload(request, default_model="gpt-5.6-luna")
    file_item = payload["input"][0]["content"][1]
    if file_item != {
        "type": "input_file",
        "file_data": f"data:application/pdf;base64,{attachment.content}",
        "filename": "verification.pdf",
    }:
        raise AssertionError(f"unexpected codex input_file wire: {file_item!r}")
    redacted = json.dumps(redact_sensitive_request_data(request.model_dump(mode="json")))
    if attachment.content in redacted:
        raise AssertionError("request capture redaction leaked file body")


async def main() -> int:
    """独立入口：只跑本场景，不写台账。"""
    sys.path.insert(0, str(HERE.parent))
    from _provider_bootstrap import load_dotenv_files, resolve_bootstrap_env

    load_dotenv_files()
    provider, _, api_key, model, base_url = resolve_bootstrap_env()
    if provider != "codex" or api_key is None or base_url is None:
        print("❌ 需要 LLM_BOOTSTRAP_PROVIDER=codex 且提供 API key 与 base_url", file=sys.stderr)
        return 2
    preflight_codex_file_input()
    print(f"[setup] provider=codex model={model}（预检通过）")
    with tempfile.TemporaryDirectory() as td:
        results = await run_codex_file_matrix(
            api_key=api_key, model=model, base_url=base_url, logs_dir=Path(td)
        )
    for result in results:
        icon = "✅" if result.verdict == "PASS" else "❌"
        print(f"  {icon}{result.verdict:4s}  {result.scenario_id:24s} "
              f"{result.duration_s:.1f}s {result.note}")
        print(f"      kinds={dict(result.kinds)}")
    return 0 if all(result.verdict == "PASS" for result in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
