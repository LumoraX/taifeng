"""工具参数按 ``input_schema`` 预校验 + 「请按 schema 重写」反馈（tool-argument-validation）。

参照 opencode ``tool/tool.ts`` 的 ``InvalidArgumentsError``：参数不合 schema 时不调 handler，
把**具体违例 + schema 本身**作为错误结果还给模型，让它在下一轮重写参数。差异：taifeng
不引入 jsonschema 依赖（内核核心依赖保持最小），内置一个只覆盖常用子集的校验器：

    type（含联合类型） / required / properties（递归） / enum / const / items /
    additionalProperties: false

**只报确定的违例**：不认识的关键字（``pattern`` / ``format`` / ``oneOf`` / ``$ref`` 等）一律
放过——宁可漏拦交给 handler 自己校验，也不误拦合法调用。模型最常犯的错（缺必填字段、
类型错、枚举外取值、多出字段）都在覆盖范围内。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from taifeng.tool.registry import ToolRegistry

# 错误反馈里回显的 schema 最大字符数（防超大 schema 撑爆上下文）
_SCHEMA_ECHO_LIMIT = 2000
# 单次最多报告的违例条数（模型改参够用，避免刷屏）
_MAX_VIOLATIONS = 8

# JSON Schema type → Python 判定（bool 是 int 子类，须先排除）
_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def _type_name(value: Any) -> str:
    """把 Python 值映射回 JSON Schema 类型名（用于错误文案）。"""
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPE_CHECKS[name](value):
            return name
    return type(value).__name__


def _check_type(schema: dict[str, Any], value: Any, path: str) -> str | None:
    """校验 ``type``；未知类型名放过（不误拦）。"""
    declared = schema.get("type")
    if declared is None:
        return None
    names = declared if isinstance(declared, list) else [declared]
    known = [n for n in names if isinstance(n, str) and n in _TYPE_CHECKS]
    if not known or any(_TYPE_CHECKS[n](value) for n in known):
        return None
    return f"{path}: expected {'|'.join(known)}, got {_type_name(value)}"


def _check_object(schema: dict[str, Any], value: dict[str, Any], path: str, out: list[str]) -> None:
    """对象：required / additionalProperties:false / properties 递归。"""
    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in value:
                out.append(f"{path}: missing required property {key!r}")
    properties = schema.get("properties")
    props = properties if isinstance(properties, dict) else {}
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in props:
                out.append(f"{path}: unexpected property {key!r}")
    for key, sub_schema in props.items():
        if key in value and isinstance(sub_schema, dict):
            _validate(sub_schema, value[key], f"{path}.{key}", out)


def _validate(schema: dict[str, Any], value: Any, path: str, out: list[str]) -> None:
    """递归校验一个值；违例追加到 ``out``。"""
    type_error = _check_type(schema, value, path)
    if type_error is not None:
        # 类型都不对，下探子结构只会产生噪声
        out.append(type_error)
        return
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: must equal {schema['const']!r}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        out.append(f"{path}: {value!r} is not one of {enum!r}")
    if isinstance(value, dict):
        _check_object(schema, value, path, out)
    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        for i, element in enumerate(value):
            _validate(items, element, f"{path}[{i}]", out)


def schema_violations(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """返回参数相对 schema 的确定违例（空列表 = 通过或无法判定）。

    Args:
        schema: 工具的 ``input_schema``（JSON Schema 对象）。
        arguments: 已解析为 dict 的调用参数。
    """
    if not isinstance(schema, dict) or not schema:
        return []
    out: list[str] = []
    _validate(schema, arguments, "$", out)
    return out[:_MAX_VIOLATIONS]


def invalid_arguments_feedback(schema: dict[str, Any], violations: list[str]) -> str:
    """渲染给模型的改参反馈：违例清单 + 期望的 schema（截断）。

    文案是中性的 LLM-facing 事实（英文，与既有 ``invalid_arguments:`` 前缀一致），
    不含产品意见（R1）。
    """
    rendered = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(rendered) > _SCHEMA_ECHO_LIMIT:
        rendered = rendered[:_SCHEMA_ECHO_LIMIT] + "…(truncated)"
    return (
        "invalid_arguments: " + "; ".join(violations)
        + ". The tool was not executed. Re-issue the call with arguments that match "
        + f"its input schema: {rendered}"
    )


def check_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """一站式：通过返回 None，否则返回可直接作为错误结果的反馈文本。"""
    violations = schema_violations(schema, arguments)
    if not violations:
        return None
    return invalid_arguments_feedback(schema, violations)


def arguments_rejection(
    registry: ToolRegistry | None, name: str, arguments: dict[str, Any], parse_error: str | None,
) -> str | None:
    """派发前参数关卡的单一入口：解析错误优先，其次 schema 违例；通过返回 None。

    turn 主派发、resume 执行被批准 / 被人工裁决 retry 的调用、子 thread 续跑三条路径
    共用本函数——同一条调用在哪条路径执行，参数规则都一样严。

    Args:
        registry: 工具注册表（取 ``input_schema``；None 或未注册的工具不做 schema 校验）。
        name: 工具名。
        arguments: 已解析的参数（解析失败时为 ``{}``）。
        parse_error: ``parse_tool_arguments`` 报告的解析错误。
    """
    if parse_error is not None:
        return f"invalid_arguments: {parse_error}"
    spec = registry.get(name) if registry is not None else None
    if spec is None:
        return None
    return check_tool_arguments(spec.input_schema, arguments)


__all__ = [
    "arguments_rejection",
    "check_tool_arguments",
    "invalid_arguments_feedback",
    "schema_violations",
]
