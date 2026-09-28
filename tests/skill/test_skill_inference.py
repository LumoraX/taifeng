"""skill-inference —— SKILL.md ``inference`` 块声明推理参数，随 entry 采样下发。

回归点：``ApiRequest`` 早有 reasoning_effort / temperature / max_output_tokens 字段，
各 provider 也都会翻译，但内核从未设置过它们——skill 无法按任务声明「分类用 0 温度」
「深推理用 high effort」，只能整个 client 一刀切。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.skill.definition import SkillInference, SkillValidationError
from taifeng.skill.loader import load_skills_from_dir
from tests.conftest import ATOMIC_SKILL, run_until_root_done_kind

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.llm.types import ApiRequest


def _entry_md(inference_block: str) -> str:
    """带 inference 块的 composite entry skill。"""
    return f"""---
name: code-reviewer
description: 代码审查专家
type: composite
entry: true
child_skills: [style-checker]
{inference_block}
---
# 代码审查专家
"""


def _atomic_md(inference_block: str) -> str:
    """带 inference 块的 atomic skill（经 call_skill 派发时独立采样）。"""
    return ATOMIC_SKILL.replace("type: atomic\n", f"type: atomic\n{inference_block}\n", 1)


def _write(root: Path, entry_block: str, atomic_block: str = "") -> Path:
    skills = root / "skills"
    (skills / "code-reviewer").mkdir(parents=True)
    (skills / "code-reviewer" / "SKILL.md").write_text(_entry_md(entry_block), encoding="utf-8")
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(_atomic_md(atomic_block), encoding="utf-8")
    return skills


def test_inference_absent_defaults_to_undeclared(tmp_path: Path) -> None:
    """未声明 inference → 全 None，请求不带任何参数（行为与改动前一致）。"""
    skills = load_skills_from_dir(_write(tmp_path, ""))
    assert skills["code-reviewer"].inference == SkillInference()


def test_inference_block_parsed(tmp_path: Path) -> None:
    """合法 inference 块逐字段解析；整数温度归一为 float。"""
    block = "inference:\n  reasoning_effort: high\n  temperature: 0\n  max_output_tokens: 2048"
    skills = load_skills_from_dir(_write(tmp_path, block))
    assert skills["code-reviewer"].inference == SkillInference(
        reasoning_effort="high", temperature=0.0, max_output_tokens=2048)


@pytest.mark.parametrize(("block", "match"), [
    ("inference: high", "必须是 mapping"),
    ("inference:\n  temprature: 0.2", "未知键"),
    ("inference:\n  reasoning_effort: extreme", "reasoning_effort 非法值"),
    ("inference:\n  temperature: 2.5", "temperature"),
    ("inference:\n  temperature: -0.1", "temperature"),
    ("inference:\n  temperature: warm", "temperature"),
    ("inference:\n  temperature: true", "temperature"),
    ("inference:\n  max_output_tokens: 0", "max_output_tokens"),
    ("inference:\n  max_output_tokens: 1.5", "max_output_tokens"),
    ("inference:\n  max_output_tokens: true", "max_output_tokens"),
], ids=["not-mapping", "typo-key", "bad-effort", "temp-high", "temp-negative",
        "temp-string", "temp-bool", "max-zero", "max-float", "max-bool"])
def test_invalid_inference_rejected_at_load(tmp_path: Path, block: str, match: str) -> None:
    """非法 inference 在加载期显式报错，不静默回退默认值。"""
    with pytest.raises(SkillValidationError, match=match):
        load_skills_from_dir(_write(tmp_path, block))


class _RecordingSim(SimClient):
    """记录每次采样收到的 ApiRequest。"""

    def __init__(self, *, turns: list[SimTurn]) -> None:
        super().__init__(turns=turns)
        self.seen: list[ApiRequest] = []

    def _next_turn(self, request: ApiRequest) -> SimTurn:
        self.seen.append(request)
        return super()._next_turn(request)


async def test_entry_and_dispatched_child_each_send_own_inference(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """父 entry 与 call_skill 派发的子 skill 各自按自己的声明下发推理参数。"""
    skills = _write(
        tmp_path,
        "inference:\n  reasoning_effort: high\n  max_output_tokens: 4096",
        "inference:\n  temperature: 0",
    )
    client = _RecordingSim(turns=[
        SimTurn(text="派发", tool_calls=[{
            "id": "c1", "name": "call_skill",
            "arguments": '{"skill_id": "style-checker", "reason": "查风格"}',
        }]),
        SimTurn(text="风格无问题"),
        SimTurn(text="综合：通过"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="inf", entry_skill_id="code-reviewer")
    kind = await run_until_root_done_kind(engine, taifeng.UserMessage(text="审查"))
    await pool.close()

    assert kind == "turn_completed"
    parent_first, child, parent_final = client.seen
    for parent in (parent_first, parent_final):
        assert (parent.reasoning_effort, parent.temperature, parent.max_output_tokens) == (
            "high", None, 4096)
    assert (child.reasoning_effort, child.temperature, child.max_output_tokens) == (
        None, 0.0, None)
