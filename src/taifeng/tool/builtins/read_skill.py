"""read_skill 工具 —— LLM 按需取 skill body。

ToolContext.extras 必须含 ``skill_snapshot``。
"""

from __future__ import annotations

from typing import Any

from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec


async def _read_skill_handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    skill_id = args.get("skill_id")
    if not skill_id or not isinstance(skill_id, str):
        return ToolResult.error("missing_or_invalid_argument: skill_id", reason="bad_args")

    snapshot: SkillSnapshot | None = ctx.extras.get("skill_snapshot")
    if snapshot is None:
        return ToolResult.error("skill_snapshot not in tool context", reason="config_error")

    # 权限边界：必须在「当前 entry skill 的可达图」内
    visible: frozenset[str] | None = ctx.extras.get("visible_skills")
    if visible is not None and skill_id not in visible:
        return ToolResult.error(
            f"skill_not_visible: {skill_id} not reachable from current entry skill",
            reason="not_visible",
        )

    defn = snapshot.get(skill_id)
    if defn is None:
        return ToolResult.error(f"unknown_skill: {skill_id}", reason="not_found")

    return ToolResult.ok(
        defn.body,
        skill_id=defn.id,
        skill_type=defn.type,
        skill_name=defn.name,
    )


READ_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {
            "type": "string",
            "description": "要读取的 skill id（目录名）",
        },
    },
    "required": ["skill_id"],
    "additionalProperties": False,
}


def make_read_skill_tool() -> ToolSpec:
    """构造 read_skill 工具规范。"""
    return ToolSpec(
        name="read_skill",
        description="读取指定 skill 的完整文档内容。仅能读取当前 entry skill 的可达图内的 skill。",
        input_schema=READ_SKILL_SCHEMA,
        handler=_read_skill_handler,
        parallel_safe=True,  # 只读 snapshot，安全并行
        timeout_seconds=5.0,
    )
