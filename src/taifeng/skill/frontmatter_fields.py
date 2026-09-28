"""SKILL.md frontmatter 字段的类型化读取 —— 类型不对即报错，不做强制转换。

此前 loader 直接 ``bool(fm.get("entry"))`` / ``frozenset(fm.get("child_skills"))``：
``entry: "false"`` 被读成 True，``child_skills: style-checker``（漏写方括号）被拆成
单个字符的集合，``model_invocable: "no"`` 仍然可见——全部静默。本模块集中提供按类型
取值的函数，以及 ``requires`` / ``exposure`` / ``inference`` 三个嵌套块的解析，
任何类型或取值不符都抛 ``SkillValidationError``（加载期 fail-fast）。

顶层未知键不在此拒绝：``SkillDefinition.frontmatter_raw`` 是业务透传通道，业务可放自有键。
"""

from __future__ import annotations

from typing import Any, get_args

from taifeng.skill.definition import (
    ChildRecall,
    ReasoningEffort,
    SkillExposure,
    SkillInference,
    SkillRequirements,
    SkillValidationError,
)

# child_recall 合法值集合（直接从 ChildRecall Literal 派生，避免魔法值重复）
_CHILD_RECALL_VALUES: frozenset[str] = frozenset(get_args(ChildRecall))

# inference 块合法键与 reasoning_effort 合法值（同样从类型派生）
_INFERENCE_KEYS: frozenset[str] = frozenset({"reasoning_effort", "temperature", "max_output_tokens"})
_REASONING_EFFORT_VALUES: frozenset[str] = frozenset(get_args(ReasoningEffort))
# temperature 取值上限：OpenAI / Gemini 的公共上界（Anthropic 为 1，超出由 provider 报错）
_MAX_TEMPERATURE = 2.0


def _fail(skill_id: str, key: str, expected: str, value: object) -> SkillValidationError:
    """构造统一格式的字段类型错误。"""
    return SkillValidationError(
        f"skill {skill_id!r} frontmatter {key} 须为{expected}，实际 {value!r}"
    )


def get_str(fm: dict[str, Any], key: str, skill_id: str) -> str | None:
    """取可选字符串字段；缺省或显式 null → None。"""
    value = fm.get(key)
    if value is not None and not isinstance(value, str):
        raise _fail(skill_id, key, "字符串", value)
    return value


def get_bool(fm: dict[str, Any], key: str, skill_id: str, *, default: bool) -> bool:
    """取布尔字段；只接受 YAML 布尔字面量（``"false"`` 这类字符串拒绝）。"""
    value = fm.get(key, default)
    if not isinstance(value, bool):
        raise _fail(skill_id, key, "布尔值 true / false", value)
    return value


def get_str_set(fm: dict[str, Any], key: str, skill_id: str) -> frozenset[str]:
    """取字符串列表字段；缺省或 null → 空集。

    裸字符串（漏写方括号）拒绝：``frozenset("abc")`` 会静默拆成字符集合。
    """
    value = fm.get(key)
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(skill_id, key, "字符串列表", value)
    return frozenset(value)


def get_positive_int(fm: dict[str, Any], key: str, skill_id: str, *, default: int) -> int:
    """取正整数字段（>= 1）；布尔值与浮点拒绝。"""
    value = fm.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail(skill_id, key, " >= 1 的整数", value)
    return value


def get_mapping(fm: dict[str, Any], key: str, skill_id: str) -> dict[str, Any]:
    """取嵌套 mapping 块；缺省或 null → 空 dict。"""
    value = fm.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SkillValidationError(f"skill {skill_id!r} frontmatter {key} 必须是 mapping")
    return value


def build_visibility(
    fm: dict[str, Any], skill_id: str
) -> tuple[SkillRequirements, SkillExposure]:
    """从 frontmatter 解析 G4 可见性字段（``requires`` + ``exposure``）。

    ``requires`` 形如::

        requires:
          bins: [jq, rg]
          env: [OPENAI_API_KEY]
          os: [linux, darwin]

    ``exposure`` 形如 ``exposure: {model_invocable: false, user_invocable: true}``。
    缺省时全部用安全默认（无要求 / 全可见）；类型不符报错。
    """
    raw_req = get_mapping(fm, "requires", skill_id)
    requires = SkillRequirements(
        bins=get_str_set(raw_req, "bins", skill_id),
        env=get_str_set(raw_req, "env", skill_id),
        os=get_str_set(raw_req, "os", skill_id),
    )

    raw_exp = get_mapping(fm, "exposure", skill_id)
    # child_recall 三值枚举校验：缺省回退 auto；非法值必须抛错（禁 silent fallback）
    raw_recall = raw_exp.get("child_recall", "auto")
    if raw_recall not in _CHILD_RECALL_VALUES:
        raise SkillValidationError(
            f"skill {skill_id!r} frontmatter exposure.child_recall 非法值 "
            f"{raw_recall!r}，合法值：{sorted(_CHILD_RECALL_VALUES)}"
        )
    exposure = SkillExposure(
        model_invocable=get_bool(raw_exp, "model_invocable", skill_id, default=True),
        user_invocable=get_bool(raw_exp, "user_invocable", skill_id, default=True),
        child_recall=raw_recall,
    )
    return requires, exposure


def build_inference(fm: dict[str, Any], skill_id: str) -> SkillInference:
    """从 frontmatter 解析 ``inference`` 块（skill 级推理参数）。

    形如::

        inference:
          reasoning_effort: high
          temperature: 0
          max_output_tokens: 2048

    缺省 → 全 None（不声明）。任何非法内容都在加载期抛错，不回退默认值：
    写错的键名 / 值若被静默忽略，作者会以为参数已生效。

    Raises:
        SkillValidationError: 非 mapping、未知键、取值类型或范围非法。
    """
    raw = get_mapping(fm, "inference", skill_id)
    unknown = sorted(set(raw) - _INFERENCE_KEYS)
    if unknown:
        raise SkillValidationError(
            f"skill {skill_id!r} frontmatter inference 含未知键 {unknown}，"
            f"合法键：{sorted(_INFERENCE_KEYS)}"
        )

    effort = raw.get("reasoning_effort")
    if effort is not None and effort not in _REASONING_EFFORT_VALUES:
        raise SkillValidationError(
            f"skill {skill_id!r} inference.reasoning_effort 非法值 {effort!r}，"
            f"合法值：{sorted(_REASONING_EFFORT_VALUES)}"
        )

    temperature = raw.get("temperature")
    # bool 是 int 子类，须先排除（``temperature: true`` 是写错而非 1.0）
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not 0 <= temperature <= _MAX_TEMPERATURE
    ):
        raise SkillValidationError(
            f"skill {skill_id!r} inference.temperature 须为 [0, {_MAX_TEMPERATURE}] 内的数值，"
            f"实际 {temperature!r}"
        )

    max_output = raw.get("max_output_tokens")
    if max_output is not None and (
        isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 1
    ):
        raise SkillValidationError(
            f"skill {skill_id!r} inference.max_output_tokens 须为 >= 1 的整数，实际 {max_output!r}"
        )

    return SkillInference(
        reasoning_effort=effort,
        temperature=None if temperature is None else float(temperature),
        max_output_tokens=max_output,
    )
