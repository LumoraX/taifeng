"""测试 render_system_prompt 的 instructions 注入 —— 装配顺序 + 向后兼容。"""

from __future__ import annotations

from pathlib import Path

from taifeng.instructions.types import ResolvedInstruction
from taifeng.loop.prompt import render_system_prompt
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.registry import SkillSnapshot


def _make_entry() -> SkillDefinition:
    """构造一个 entry-eligible 的 composite skill 用于装配测试。"""
    return SkillDefinition(
        id="root-skill",
        name="root",
        description="root entry",
        version="1.0.0",
        type="composite",
        entry=True,
        model="mock-model",
        body="root entry body content.",
        body_path=Path("_test_root.md"),  # noqa: S108 — 测试占位，不实际 IO
        child_skills=frozenset(),
        tool_names=frozenset(),
        max_call_depth=3,
    )


def _empty_snapshot() -> SkillSnapshot:
    return SkillSnapshot(version=1, skills=())


def test_no_instructions_no_block() -> None:
    """spec Scenario: 空 instructions 不出现 <system_instructions> 子串。"""
    entry = _make_entry()
    snap = _empty_snapshot()
    out = render_system_prompt(entry, snap)
    assert "<system_instructions" not in out
    # 兼容性：传 None 与 [] 行为一致
    out2 = render_system_prompt(entry, snap, instructions=None)
    out3 = render_system_prompt(entry, snap, instructions=[])
    assert out == out2 == out3


def test_renders_xml_block_in_order() -> None:
    """spec Scenario: 两层 instructions 按 resolver 给出的 priority 升序拼接。

    resolver.resolve 返回值已是升序；render_system_prompt 不重排，按调用方顺序。
    """
    entry = _make_entry()
    snap = _empty_snapshot()
    # resolver 给出的顺序：a 在 b 前（priority 10 < 50）
    instructions = [
        ResolvedInstruction(
            name="a", scope="engine", text="POLICY_A",
            fetched_at=0.0, source_kind="static",
        ),
        ResolvedInstruction(
            name="b", scope="session", text="POLICY_B",
            fetched_at=0.0, source_kind="dynamic",
        ),
    ]
    out = render_system_prompt(entry, snap, instructions=instructions)
    # 都应该出现
    assert 'name="a"' in out
    assert 'name="b"' in out
    # a 出现在 b 之前
    assert out.index("POLICY_A") < out.index("POLICY_B")
    # 都应该出现在 entry_skill 之前
    assert out.index("POLICY_A") < out.index("<entry_skill")
    assert out.index("POLICY_B") < out.index("<entry_skill")
    # scope 属性正确
    assert 'scope="engine"' in out
    assert 'scope="session"' in out


def test_block_contains_text_verbatim() -> None:
    """指令文本应原样放入 XML 块内（包含换行）。"""
    entry = _make_entry()
    snap = _empty_snapshot()
    txt = "Line1\nLine2\nLine3"
    out = render_system_prompt(
        entry, snap,
        instructions=[ResolvedInstruction(
            name="x", scope="engine", text=txt,
            fetched_at=0.0, source_kind="static",
        )],
    )
    assert txt in out


def test_existing_layout_unchanged_when_no_instructions() -> None:
    """spec: instructions=None 时行为完全不变。"""
    entry = _make_entry()
    snap = _empty_snapshot()
    out = render_system_prompt(entry, snap)
    assert "<entry_skill" in out
    assert "<available_child_skills>" in out
    assert "<dispatch_policy>" in out
