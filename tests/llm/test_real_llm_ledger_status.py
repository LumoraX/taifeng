"""真实 LLM 台账的未执行证据边界。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "real_llm"))

from _ledger import LedgerWriter, R3Audit, ScenarioRecord  # noqa: E402


def test_classify_real_regression_failure_status() -> None:
    """服务过载/网络失败与代码断言失败必须分开。"""
    from _ledger import classify_status

    assert classify_status("FAIL", "ServerError: server_is_overloaded") == "TRANSIENT_FAILURE"
    assert classify_status("FAIL", "generic provider error", "provider_internal") == "TRANSIENT_FAILURE"
    assert classify_status("FAIL", "AssertionError: missing execution identity") == "CODE_FAILURE"
    assert classify_status("PART", "完成但缺关键事件") == "PARTIAL"
    assert classify_status("PASS", "") == "PASS"


def test_old_overload_record_derives_transient_status(tmp_path: Path) -> None:
    """旧台账的过载备注不能被重写成代码失败。"""
    json_path = tmp_path / "ledger.json"
    md_path = tmp_path / "ledger.md"
    json_path.write_text(
        '{"run":{"timestamp_utc":"now","commit":"old","provider":"codex","model":"sim",'
        '"scenarios_run":[]},"scenarios":{"old":{"capability":"old","verdict":"FAIL",'
        '"note":"ServerError: server_is_overloaded","expect":[],"missing":[],"kinds":{},"grants":0,'
        '"duration_s":0,"commit":"old","timestamp_utc":"now"}},"r3_audit":{"emitted_kinds":[],"unmapped":[],"canonical_missing":[]}}',
        encoding="utf-8",
    )
    LedgerWriter(json_path=json_path, md_path=md_path).merge_and_write(
        provider="codex", model="sim", records=[], r3=R3Audit(), full_run=False,
    )
    assert "TRANSIENT_FAILURE" in md_path.read_text(encoding="utf-8")


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


def test_real_codex_result_clears_only_codex_not_executed_gap(tmp_path: Path) -> None:
    """Codex 真实记录只清自己的缺口，不得冒充 OpenAI 官方验收。"""
    json_path = tmp_path / "ledger.json"
    md_path = tmp_path / "ledger.md"
    writer = LedgerWriter(json_path=json_path, md_path=md_path)
    writer.mark_not_executed(
        key="openai_image_input",
        reason="missing official key",
        command="openai image matrix",
    )
    writer.mark_not_executed(
        key="codex_image_input",
        reason="missing proxy key",
        command="codex image matrix",
    )

    writer.merge_and_write(
        provider="codex",
        model="gpt-5.6-luna",
        records=[
            ScenarioRecord(
                scenario_id="codex_image_single",
                capability="Codex image",
                verdict="PASS",
                note="",
                expect=["completed"],
                missing=[],
                kinds={"completed": 1},
                grants=0,
                duration_s=1.0,
                commit="abc",
                timestamp_utc="2026-08-28 00:00:00 UTC",
            )
        ],
        r3=R3Audit(),
        full_run=False,
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert "codex_image_input" not in data["not_executed"]
    assert "openai_image_input" in data["not_executed"]
