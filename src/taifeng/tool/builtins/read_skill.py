"""read_skill 工具 —— LLM 按需取 skill body，或 skill 目录内的附属文件。

ToolContext.extras 必须含 ``skill_snapshot``。

渐进加载分三层（Agent Skills 范式）：child 列表只给 id + description → ``read_skill(skill_id)``
取 SKILL.md 正文 → ``read_skill(skill_id, path)`` 取正文里引用的附属文件（如
``references/api.md``、``FORMS.md``）。第三层此前缺失，作者只能把全部细节塞进正文。

附属文件的安全边界：路径必须解析在该 skill 目录内（拒绝 ``..`` 与符号链接逃逸）、
必须是 UTF-8 文本、大小不超过 ``MAX_SKILL_FILE_BYTES``；可见性与读正文同一规则
（只能读当前 entry 可达图内的 skill；启用白名单外授权时，还包括当前 skill 可发现的 skill）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.skill.authorization import is_discoverable_outside
from taifeng.skill.working_set_runtime import view_from_extras
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.registry import SkillSnapshot

# 附属文件大小上限（字节），与 SKILL.md 正文上限一致；更大的资料应拆分或走 file_read
MAX_SKILL_FILE_BYTES = 256 * 1024


def _resolve_skill_file(defn: SkillDefinition, raw_path: str) -> Path | ToolResult:
    """把相对路径解析为 skill 目录内的真实文件路径；越界 / 不存在返回错误结果。"""
    skill_dir = defn.body_path.parent.resolve()
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return ToolResult.error(f"invalid_path: {raw_path} must be relative to the skill directory",
                                reason="bad_args")
    # resolve() 展开符号链接：链接指向目录外同样判越界
    target = (skill_dir / candidate).resolve()
    if not target.is_relative_to(skill_dir):
        return ToolResult.error(f"path_outside_skill: {raw_path}", reason="not_visible")
    if not target.is_file():
        return ToolResult.error(f"file_not_found: {raw_path} in skill {defn.id}", reason="not_found")
    return target


def _read_text(target: Path) -> str | ToolResult:
    """同步读取附属文件（在工作线程中执行）；超限 / 非 UTF-8 返回错误结果。"""
    size = target.stat().st_size
    if size > MAX_SKILL_FILE_BYTES:
        return ToolResult.error(
            f"file_too_large: {size} bytes exceeds {MAX_SKILL_FILE_BYTES}", reason="too_large")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult.error("not_a_text_file: skill files must be UTF-8 text",
                                reason="not_text")


async def _read_skill_file(defn: SkillDefinition, raw_path: str) -> ToolResult:
    """读取 skill 目录内的附属文件。"""
    resolved = _resolve_skill_file(defn, raw_path)
    if isinstance(resolved, ToolResult):
        return resolved
    # 阻塞文件 IO 放到工作线程，不占用事件循环
    text = await anyio.to_thread.run_sync(_read_text, resolved)
    if isinstance(text, ToolResult):
        return text
    return ToolResult.ok(text, skill_id=defn.id, path=raw_path)


def _discoverable_outside(skill_id: str, snapshot: SkillSnapshot, ctx: ToolContext) -> bool:
    """该 skill 是否在当前 skill 的白名单外可发现范围内（未启用相位 4 时恒为 False）。"""
    policy = ctx.extras.get("dispatch_policy")
    caller = ctx.extras.get("current_skill")
    authorization = getattr(policy, "authorization", None)
    if authorization is None or caller is None:
        return False
    stack = ctx.extras.get("call_stack")
    return is_discoverable_outside(
        caller, skill_id, snapshot, authorization, ctx.extras.get("capabilities"),
        on_stack=stack.path() if stack is not None else (),
        hidden=view_from_extras(ctx.extras).hidden,
    )


async def _read_skill_handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """取 skill 正文（无 path）或附属文件（有 path）。"""
    skill_id = args.get("skill_id")
    if not skill_id or not isinstance(skill_id, str):
        return ToolResult.error("missing_or_invalid_argument: skill_id", reason="bad_args")

    snapshot: SkillSnapshot | None = ctx.extras.get("skill_snapshot")
    if snapshot is None:
        return ToolResult.error("skill_snapshot not in tool context", reason="config_error")

    # 权限边界：必须在「当前 entry skill 的可达图」内（正文与附属文件同一规则）
    # 白名单外可发现的 skill 同样可读：试用（读说明书）先于准入（ADR 0089）
    visible: frozenset[str] | None = ctx.extras.get("visible_skills")
    if (
        visible is not None
        and skill_id not in visible
        and not _discoverable_outside(skill_id, snapshot, ctx)
    ):
        return ToolResult.error(
            f"skill_not_visible: {skill_id} not reachable from current entry skill",
            reason="not_visible",
        )

    defn = snapshot.get(skill_id)
    if defn is None:
        return ToolResult.error(f"unknown_skill: {skill_id}", reason="not_found")

    path = args.get("path")
    if path is not None:
        return await _read_skill_file(defn, path)

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
        "path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "可选：skill 目录内的相对路径（正文中引用的附属文件，如 references/api.md）；"
                "省略则返回 SKILL.md 正文"
            ),
        },
    },
    "required": ["skill_id"],
    "additionalProperties": False,
}


def make_read_skill_tool() -> ToolSpec:
    """构造 read_skill 工具规范。"""
    return ToolSpec(
        name="read_skill",
        description=(
            "读取指定 skill 的完整文档内容；给出 path 时读取该 skill 目录内的附属文件"
            "（正文中引用的 references 等）。仅能读取当前 entry skill 的可达图内的 skill。"
        ),
        input_schema=READ_SKILL_SCHEMA,
        handler=_read_skill_handler,
        parallel_safe=True,  # 只读 snapshot / skill 目录，安全并行
        timeout_seconds=5.0,
    )
