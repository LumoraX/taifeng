"""声明式 skill 编排（B）—— SKILL.md ``orchestration`` 字段的解析、校验与运行时辅助。

建立在 A（并发 fan-out 派发）之上：并行组最终复用 ``loop/tool_batch.dispatch_batch``
执行，本模块**不写任何并发代码**，只负责把声明编译成已校验的结构 + 提供条件 flag 提取。

参照：docs/architecture/capabilities/skill-orchestration.md
差异：把设计里的 ``resolve_plan`` 拆为「静态 parse_orchestration（编译+校验）」+
「运行时 when 分支选择」两段——后者需运行时 flag，落在执行器（loop/orchestration_exec.py）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from taifeng.skill.definition import SkillValidationError

# parallel / serial 两种叶子原语；parallel 组内禁重复，serial 跨步重复=多次调用允许
_Primitive = Literal["parallel", "serial"]


class OrchestrationConditionError(RuntimeError):
    """``when.condition`` 引用的 flag 在上一步输出中缺失或非布尔。

    按 spec「禁 silent fallback」：不回退为 true/false，直接抛出 →
    由 ``TurnRunner.run`` 的通用 except 捕获 → turn ``end_reason=error`` + ``TurnFailed``。
    """


@dataclass(frozen=True)
class ParallelStep:
    """并行组：组内 skill 同批并发派发（执行器交给 dispatch_batch + Semaphore(max_parallel)）。"""

    skill_ids: tuple[str, ...]


@dataclass(frozen=True)
class SerialStep:
    """顺序段：组内 skill 依次执行（执行器用 Semaphore(1) 串行）。"""

    skill_ids: tuple[str, ...]


@dataclass(frozen=True)
class WhenStep:
    """单层条件：读上一步产出的布尔 flag，选 then / else 叶子执行。

    ``then`` / ``otherwise`` 只能是 ``ParallelStep | SerialStep`` 叶子（嵌套深度限 1 层，
    防 DSL 膨胀）；``otherwise`` 为 None 表示无 else 分支（condition=False 时跳过本段）。
    """

    condition: str
    then: ParallelStep | SerialStep
    otherwise: ParallelStep | SerialStep | None = None


# 顶层 step 的三种原语联合
Step = ParallelStep | SerialStep | WhenStep


@dataclass(frozen=True)
class OrchestrationSpec:
    """一个 composite skill 的完整编排声明（已解析 + 校验）。"""

    steps: tuple[Step, ...]


def parse_orchestration(
    raw: Any,
    *,
    skill_id: str,
    child_skills: frozenset[str],
) -> OrchestrationSpec:
    """把 frontmatter ``orchestration`` dict 解析 + 校验为 ``OrchestrationSpec``。

    校验项（任一失败抛 ``SkillValidationError``，fail-fast）：
        - orchestration 非 mapping / steps 非非空数组
        - 顶层 step 非「单键 mapping（parallel|serial|when）」/ 未知原语
        - 引用 id 不在 ``child_skills`` 白名单
        - 同一 id 在单个 parallel 组内重复
        - when.condition 非非空字符串 / 缺 then / then|else 内再嵌 when（超 1 层）
        - when 作为首个步骤（无前序输出可读 flag）

    Note:
        环检测无需在此重复——orchestration 引用 ⊆ child_skills，loader 的
        ``detect_cycles`` 已覆盖 child_skills 构成的调用图。
    """
    if not isinstance(raw, dict):
        raise SkillValidationError(f"skill {skill_id!r} orchestration 必须是 mapping")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration.steps 必须是非空数组"
        )
    steps: list[Step] = [
        _parse_step(rs, skill_id=skill_id, child_skills=child_skills, idx=i)
        for i, rs in enumerate(raw_steps)
    ]
    # when 不能是首个步骤——它读上一步产出的 flag，首步无前序输出必在运行时硬失败；
    # 故加载期 fail-fast，比等到实际派发才报错更早暴露配置问题。
    if isinstance(steps[0], WhenStep):
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration: when 不能是首个步骤（需上一步产出 flag）"
        )
    return OrchestrationSpec(steps=tuple(steps))


def _parse_step(
    raw_step: Any, *, skill_id: str, child_skills: frozenset[str], idx: int
) -> Step:
    """解析单个顶层 step（必须是单键 mapping：parallel|serial|when）。"""
    label = f"steps[{idx}]"
    if not isinstance(raw_step, dict) or len(raw_step) != 1:
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration {label} 必须是单键 mapping（parallel|serial|when）"
        )
    (key,) = raw_step.keys()
    val = raw_step[key]
    if key == "parallel":
        return ParallelStep(
            skill_ids=_parse_ids(
                val, skill_id=skill_id, child_skills=child_skills,
                label=label, primitive="parallel",
            )
        )
    if key == "serial":
        return SerialStep(
            skill_ids=_parse_ids(
                val, skill_id=skill_id, child_skills=child_skills,
                label=label, primitive="serial",
            )
        )
    if key == "when":
        return _parse_when(val, skill_id=skill_id, child_skills=child_skills, label=label)
    raise SkillValidationError(
        f"skill {skill_id!r} orchestration {label} 未知原语: {key!r}（仅支持 parallel|serial|when）"
    )


def _parse_ids(
    raw_ids: Any,
    *,
    skill_id: str,
    child_skills: frozenset[str],
    label: str,
    primitive: _Primitive,
) -> tuple[str, ...]:
    """校验并归一化一组 skill id（非空、字符串、在白名单、parallel 组内不重复）。"""
    if not isinstance(raw_ids, list) or not raw_ids:
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration {label} 的 {primitive} 必须是非空 id 列表"
        )
    ids: list[str] = []
    for x in raw_ids:
        if not isinstance(x, str) or not x:
            raise SkillValidationError(
                f"skill {skill_id!r} orchestration {label} 含非法 skill id: {x!r}"
            )
        ids.append(x)
    unknown = [i for i in ids if i not in child_skills]
    if unknown:
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration {label} 引用了不在 child_skills 的 skill: {unknown}"
        )
    # serial 段允许同 id 多次=按顺序多次调用，仅 parallel 组内重复才是歧义
    if primitive == "parallel" and len(set(ids)) != len(ids):
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration {label} parallel 组内 skill id 重复: {ids}"
        )
    return tuple(ids)


def _parse_leaf(
    raw: Any, *, skill_id: str, child_skills: frozenset[str], label: str
) -> ParallelStep | SerialStep:
    """解析 when 的叶子分支。接受两种写法（不接受再嵌 when → 深度限 1 层）：

    - 裸列表 ``[a, b]``          → ParallelStep（缺省并行，对齐 design when.then 示例）
    - ``{parallel: [a, b]}``     → ParallelStep
    - ``{serial: [a, b]}``       → SerialStep
    """
    if isinstance(raw, list):
        return ParallelStep(
            skill_ids=_parse_ids(
                raw, skill_id=skill_id, child_skills=child_skills,
                label=label, primitive="parallel",
            )
        )
    if isinstance(raw, dict) and len(raw) == 1:
        (key,) = raw.keys()
        if key in ("parallel", "serial"):
            ids = _parse_ids(
                raw[key], skill_id=skill_id, child_skills=child_skills,
                label=label, primitive=key,
            )
            return ParallelStep(skill_ids=ids) if key == "parallel" else SerialStep(skill_ids=ids)
    raise SkillValidationError(
        f"skill {skill_id!r} orchestration {label} "
        "必须是 list 或 {parallel|serial: [...]}（不可再嵌 when）"
    )


def _parse_when(
    val: Any, *, skill_id: str, child_skills: frozenset[str], label: str
) -> WhenStep:
    """解析 when 段：condition（非空字符串）+ then（必需叶子）+ else（可选叶子）。"""
    if not isinstance(val, dict):
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration {label}.when 必须是 mapping"
        )
    condition = val.get("condition")
    if not isinstance(condition, str) or not condition:
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration {label}.when.condition 必须是非空字符串"
        )
    if "then" not in val:
        raise SkillValidationError(
            f"skill {skill_id!r} orchestration {label}.when 必须含 then 分支"
        )
    then_leaf = _parse_leaf(
        val["then"], skill_id=skill_id, child_skills=child_skills, label=f"{label}.when.then"
    )
    otherwise: ParallelStep | SerialStep | None = None
    # 显式写了 else 就必须是合法叶子；else: null 不静默忽略，交给 _parse_leaf 报错
    if "else" in val:
        otherwise = _parse_leaf(
            val["else"], skill_id=skill_id, child_skills=child_skills, label=f"{label}.when.else"
        )
    return WhenStep(condition=condition, then=then_leaf, otherwise=otherwise)


def plan_to_groups(spec: OrchestrationSpec) -> list[dict[str, Any]]:
    """把 ``OrchestrationSpec`` 序列化为 ``orchestration_plan_resolved`` 事件的 groups。

    返回值为 JSON-safe 的 list，可直接放入事件 data 字段。

    设计说明：WhenStep 只序列化 condition 字段（不含 then/else 分支内容），这是
    有意为之——此处产出的是供可观测性使用的人类可读摘要，完整结构已存于执行器
    持有的 OrchestrationSpec 中，无需在事件 data 中重复。
    """
    groups: list[dict[str, Any]] = []
    for step in spec.steps:
        if isinstance(step, ParallelStep):
            groups.append({"type": "parallel", "skills": list(step.skill_ids)})
        elif isinstance(step, SerialStep):
            groups.append({"type": "serial", "skills": list(step.skill_ids)})
        else:  # WhenStep
            groups.append({"type": "when", "condition": step.condition})
    return groups


def extract_condition_flag(condition: str, prev_outputs: tuple[str, ...]) -> bool:
    """从上一步各 child 的输出（约定为 JSON object 文本）中提取布尔 flag。

    规则：按发起序解析每个 output 为 JSON object，取第一个含 ``condition`` 键的；
    其值必须是 bool。找不到 / 值非 bool → 抛 ``OrchestrationConditionError``（禁 silent fallback）。
    """
    for out in prev_outputs:
        try:
            parsed = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and condition in parsed:
            value = parsed[condition]
            if isinstance(value, bool):
                return value
            raise OrchestrationConditionError(
                f"condition {condition!r} 在上一步输出中存在但非布尔: {value!r}"
            )
    raise OrchestrationConditionError(
        f"condition {condition!r} 在上一步输出中缺失（共 {len(prev_outputs)} 条输出）"
    )
