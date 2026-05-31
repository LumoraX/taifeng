"""skill 编排声明解析/校验 + 条件 flag 提取单测（纯函数，无 IO）。"""
from __future__ import annotations

import pytest

from taifeng.skill.definition import SkillValidationError
from taifeng.skill.orchestration import (
    OrchestrationConditionError,
    OrchestrationSpec,
    ParallelStep,
    SerialStep,
    WhenStep,
    extract_condition_flag,
    parse_orchestration,
    plan_to_groups,
)

CHILDREN = frozenset({"route-a", "route-b", "summarizer", "weather", "traffic"})


def _parse(raw: dict) -> OrchestrationSpec:
    return parse_orchestration(raw, skill_id="trip", child_skills=CHILDREN)


def test_parse_three_primitives() -> None:
    """parallel / serial / when 三原语解析为对应 Step 类型。"""
    spec = _parse({
        "steps": [
            {"parallel": ["route-a", "route-b"]},
            {"serial": ["summarizer"]},
            {"when": {"condition": "needs_weather", "then": ["weather", "traffic"]}},
        ]
    })
    assert isinstance(spec.steps[0], ParallelStep)
    assert spec.steps[0].skill_ids == ("route-a", "route-b")
    assert isinstance(spec.steps[1], SerialStep)
    assert spec.steps[1].skill_ids == ("summarizer",)
    assert isinstance(spec.steps[2], WhenStep)
    assert spec.steps[2].condition == "needs_weather"
    # when.then 裸列表缺省并行
    assert isinstance(spec.steps[2].then, ParallelStep)
    assert spec.steps[2].then.skill_ids == ("weather", "traffic")
    assert spec.steps[2].otherwise is None


def test_when_else_mapping_leaf() -> None:
    """when.else 支持 {serial: [...]} mapping 写法。"""
    spec = _parse({
        "steps": [
            {"parallel": ["route-a"]},
            {"when": {
                "condition": "f",
                "then": {"serial": ["weather"]},
                "else": {"parallel": ["traffic"]},
            }},
        ]
    })
    when = spec.steps[1]
    assert isinstance(when.then, SerialStep)
    assert isinstance(when.otherwise, ParallelStep)


def test_unknown_child_rejected() -> None:
    """引用不在 child_skills 的 id → SkillValidationError（fail-fast）。"""
    with pytest.raises(SkillValidationError, match="不在 child_skills"):
        _parse({"steps": [{"parallel": ["route-a", "ghost"]}]})


def test_duplicate_in_parallel_rejected() -> None:
    """同一 id 在单个 parallel 组内重复 → SkillValidationError。"""
    with pytest.raises(SkillValidationError, match="重复"):
        _parse({"steps": [{"parallel": ["route-a", "route-a"]}]})


def test_nested_when_rejected() -> None:
    """when.then 内再嵌 when（超 1 层）→ SkillValidationError。"""
    with pytest.raises(SkillValidationError, match="不可再嵌"):
        _parse({"steps": [{"when": {
            "condition": "f",
            "then": {"when": {"condition": "g", "then": ["weather"]}},
        }}]})


def test_empty_steps_rejected() -> None:
    """steps 缺失/空 → SkillValidationError。"""
    with pytest.raises(SkillValidationError, match="steps"):
        _parse({"steps": []})


def test_unknown_primitive_rejected() -> None:
    """未知原语 key → SkillValidationError。"""
    with pytest.raises(SkillValidationError, match="未知原语"):
        _parse({"steps": [{"loop": ["route-a"]}]})


def test_when_as_first_step_rejected() -> None:
    """when 作为首个步骤（无前序输出可读 flag）→ 加载期 SkillValidationError。"""
    with pytest.raises(SkillValidationError, match="不能是首个步骤"):
        _parse({"steps": [{"when": {"condition": "f", "then": ["weather"]}}]})


def test_plan_to_groups_serializable() -> None:
    """plan_to_groups 产出 JSON-safe 的事件 data。"""
    spec = _parse({"steps": [
        {"parallel": ["route-a", "route-b"]},
        {"when": {"condition": "needs_weather", "then": ["weather"]}},
    ]})
    groups = plan_to_groups(spec)
    assert groups == [
        {"type": "parallel", "skills": ["route-a", "route-b"]},
        {"type": "when", "condition": "needs_weather"},
    ]


def test_extract_condition_flag_true() -> None:
    """上一步输出含 bool flag → 返回该值。"""
    assert extract_condition_flag("needs_weather", ('{"needs_weather": true}',)) is True
    assert extract_condition_flag("needs_weather", ('{"needs_weather": false}',)) is False


def test_extract_condition_flag_first_match_wins() -> None:
    """多条输出时取第一个含该键且为 bool 的；非 JSON 行跳过。"""
    outs = ("not json", '{"other": 1}', '{"needs_weather": true}')
    assert extract_condition_flag("needs_weather", outs) is True


def test_extract_condition_flag_missing_raises() -> None:
    """flag 缺失 → OrchestrationConditionError（禁 silent fallback）。"""
    with pytest.raises(OrchestrationConditionError, match="缺失"):
        extract_condition_flag("needs_weather", ('{"other": 1}',))


def test_extract_condition_flag_non_bool_raises() -> None:
    """flag 存在但非布尔 → OrchestrationConditionError。"""
    with pytest.raises(OrchestrationConditionError, match="非布尔"):
        extract_condition_flag("needs_weather", ('{"needs_weather": "yes"}',))


def test_duplicate_in_serial_allowed() -> None:
    """serial 段内允许同 id 重复（=按顺序多次调用），不报错。"""
    spec = _parse({"steps": [{"serial": ["route-a", "route-a"]}]})
    assert isinstance(spec.steps[0], SerialStep)
    assert spec.steps[0].skill_ids == ("route-a", "route-a")


def test_extract_condition_flag_empty_outputs_raises() -> None:
    """上一步无任何输出（空元组）→ OrchestrationConditionError（禁 silent fallback）。"""
    with pytest.raises(OrchestrationConditionError, match="缺失"):
        extract_condition_flag("needs_weather", ())


def test_when_else_null_rejected() -> None:
    """显式 else: null 不被静默忽略 → SkillValidationError（交给叶子解析报错）。"""
    with pytest.raises(SkillValidationError):
        _parse({"steps": [
            {"serial": ["route-a"]},
            {"when": {"condition": "f", "then": ["weather"], "else": None}},
        ]})
