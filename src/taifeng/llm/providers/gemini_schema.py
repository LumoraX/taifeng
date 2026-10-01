"""把工具的 JSON Schema 投影成 Gemini 函数声明接受的形状（ADR 0115）。

Gemini 的 ``functionDeclarations[].parameters`` 是 OpenAPI 3.0 Schema 的子集，遇到不认识的关键字
（最常见的是 ``additionalProperties``）整次请求 400。内核的工具 schema 是完整的 JSON Schema，而且内置
工具普遍带 ``additionalProperties``——不投影的话，任何带默认工具的会话在 Gemini 上都发不出去。

投影只影响「告诉模型参数长什么样」。参数是否合规仍由内核按**原始** schema 在派发前校验
（tool-argument-validation），所以去掉 Gemini 不认的约束不会放进不合规的调用。

参照：Gemini API ``Schema`` 对象的字段表；差异：这里只做删减与 ``type`` 的空值改写，不做语义改写。
"""

from __future__ import annotations

from typing import Any

# Gemini Schema 接受的关键字（其余一律去掉）
_SUPPORTED = frozenset({
    "type", "format", "title", "description", "nullable", "enum", "default", "example",
    "properties", "required", "propertyOrdering", "minProperties", "maxProperties",
    "items", "minItems", "maxItems",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "anyOf",
})


def to_gemini_schema(schema: Any) -> Any:
    """递归投影一份 schema；不是 dict 的值原样返回。输入不被改动。"""
    if not isinstance(schema, dict):
        return schema
    projected: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SUPPORTED:
            continue
        if key == "properties" and isinstance(value, dict):
            # properties 的键是属性名（可能恰好与关键字同名），值才是 schema
            projected[key] = {name: to_gemini_schema(sub) for name, sub in value.items()}
        elif key == "items":
            projected[key] = to_gemini_schema(value)
        elif key == "anyOf" and isinstance(value, list):
            projected[key] = [to_gemini_schema(option) for option in value]
        elif key == "type" and isinstance(value, list):
            # ["string", "null"] → type: string + nullable；多个非空类型无法表达，去掉 type
            concrete = [item for item in value if item != "null"]
            if "null" in value:
                projected["nullable"] = True
            if len(concrete) == 1:
                projected[key] = concrete[0]
        else:
            projected[key] = value
    return projected


__all__ = ["to_gemini_schema"]
