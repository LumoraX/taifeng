"""MCP 工具 ``outputSchema`` 校验（2025-06-18 §Tools「Output Schema」）。

规范：工具在 ``tools/list`` 声明 ``outputSchema`` 时，server **MUST** 返回符合它的
``structuredContent``，客户端 **SHOULD** 按它校验；安全条款另有「客户端 SHOULD 在把工具结果
交给 LLM 之前校验」。规范没有规定不合规时客户端怎么办，本模块取两家官方 SDK 的一致做法：

| 情形 | 处置 |
| --- | --- |
| ``isError: true`` | 不校验（工具执行错误的结果本就是非结构化的错误说明） |
| 声明了 outputSchema 却没给 ``structuredContent`` | 违例 |
| ``structuredContent`` 违反 outputSchema | 违例 |

违例由桥转成该次调用的错误结果（``reason="mcp_output_schema_violation"``），**不**把未通过校验的
内容交给模型——server 已违反自己声明的契约，其数据不可信。

校验器复用 ``taifeng.tool.arg_validation.schema_violations``：覆盖 type / required / properties /
enum / const / items / additionalProperties:false；不认识的关键字（pattern / format / oneOf / $ref …）
放过——只报确定的违例，宁可漏拦也不误拦合法结果。

参照：modelcontextprotocol typescript-sdk ``Client.callTool``（有 outputSchema 且非 isError 时：缺
structuredContent → 报错；不合 schema → 报错）与 python-sdk ``ClientSession._validate_tool_result``。
差异：taifeng 不引入 jsonschema 依赖，只校验内核子集；违例落成工具错误结果而非抛异常（模型可见原因）。
"""

from __future__ import annotations

from typing import Any

from taifeng.tool.arg_validation import schema_violations


class McpOutputSchemaError(ValueError):
    """``tools/list`` 条目的 ``outputSchema`` 形状非法（不是 ``type: object`` 的 JSON 对象）。"""


def parse_output_schema(meta: dict[str, Any]) -> dict[str, Any] | None:
    """取工具元数据里的 ``outputSchema``。

    Args:
        meta: ``tools/list`` 返回的单个工具对象。

    Returns:
        schema 对象；未声明为 None。

    Raises:
        McpOutputSchemaError: 声明了但不是对象，或 ``type`` 不是 ``"object"``（规范规定
            outputSchema 的根类型恒为 object，structuredContent 也只能是对象）。
    """
    schema = meta.get("outputSchema")
    if schema is None:
        return None
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise McpOutputSchemaError(
            f"tool {meta.get('name')!r}: outputSchema must be an object schema with type 'object'")
    return schema


def structured_output_violations(
    output_schema: dict[str, Any],
    structured_content: dict[str, Any] | None,
    *,
    is_error: bool,
) -> list[str]:
    """按 outputSchema 校验一次调用的结构化结果，返回确定的违例（空列表 = 通过）。

    Args:
        output_schema: 工具声明的 outputSchema（已经 ``parse_output_schema`` 校验形状）。
        structured_content: 本次结果的 ``structuredContent``（未提供为 None）。
        is_error: 本次结果的 ``isError``；为真时不校验。
    """
    if is_error:
        return []
    if structured_content is None:
        return ["$: structuredContent is missing but the tool declares an outputSchema"]
    return schema_violations(output_schema, structured_content)


__all__ = ["McpOutputSchemaError", "parse_output_schema", "structured_output_violations"]
