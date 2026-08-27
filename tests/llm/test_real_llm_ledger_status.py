"""真实 LLM 台账的未执行证据边界。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "real_llm"))

from _ledger import LedgerWriter, R3Audit, ScenarioRecord  # noqa: E402


def test_ledger_records_not_executed_validation(tmp_path: Path) -> None:
    """无凭据必须落明确 NOT_EXECUTED，而不是伪造 PASS/FAIL。"""
    json_path = tmp_path / "ledger.json"
    md_path = tmp_path / "ledger.md"
    writer = LedgerWriter(json_path=json_path, md_path=md_path)

    writer.mark_not_executed(
        key="openai_image_input",
        reason="OpenAI API key unavailable in verification environment",
        command="capability_matrix.py --provider openai --model gpt-5.6",
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["not_executed"]["openai_image_input"]["status"] == "NOT_EXECUTED"
    assert "OpenAI API key unavailable" in md_path.read_text(encoding="utf-8")


def test_real_image_result_clears_not_executed_gap(tmp_path: Path) -> None:
    """真实图片场景写入后，生成器必须清除对应未执行缺口。"""
    json_path = tmp_path / "ledger.json"
    md_path = tmp_path / "ledger.md"
    writer = LedgerWriter(json_path=json_path, md_path=md_path)
    writer.mark_not_executed(
        key="openai_image_input",
        reason="missing key",
        command="image matrix",
    )

    writer.merge_and_write(
        provider="openai",
        model="gpt-5.6",
        records=[
            ScenarioRecord(
                scenario_id="openai_chat_image_single",
                capability="image",
                verdict="PASS",
                note="",
                expect=["completed"],
                missing=[],
                kinds={"completed": 1},
                grants=0,
                duration_s=1.0,
                commit="abc",
                timestamp_utc="2026-08-27 00:00:00 UTC",
            )
        ],
        r3=R3Audit(),
        full_run=False,
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert "openai_image_input" not in data["not_executed"]
