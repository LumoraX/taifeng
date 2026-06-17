"""SKILL.md 加载器 —— frontmatter 解析、目录扫描、静态环检测。

参照：
    - 内部已实测 Python 实现（loader 范式）
    - codex codex-rs/core-skills/src/loader.rs
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, get_args

import yaml

from taifeng.skill.definition import (
    ChildRecall,
    SkillDefinition,
    SkillExposure,
    SkillRequirements,
    SkillSource,
    SkillType,
    SkillValidationError,
)
from taifeng.skill.dispatch import CircularSkillReference, detect_cycles
from taifeng.skill.scripts.types import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    ScriptDescriptor,
    ScriptLanguage,
)

logger = logging.getLogger(__name__)

# SKILL.md 大小限制（body）
MAX_SKILL_BODY_SIZE = 256 * 1024  # 256KB
TRUNCATE_SUFFIX = "\n\n[内容已截断，完整文档请参考 references/]"

# Frontmatter 分隔符
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# child_recall 合法值集合（直接从 ChildRecall Literal 派生，避免魔法值重复）
_CHILD_RECALL_VALUES: frozenset[str] = frozenset(get_args(ChildRecall))


class SkillLoadError(Exception):
    """Skill 加载失败。"""


class UnknownChildSkill(SkillLoadError):
    """Composite skill 引用了未知的子 skill。"""


def parse_skill_md(path: Path) -> dict[str, Any] | None:
    """解析单个 SKILL.md 文件。

    Returns:
        含 ``frontmatter`` / ``body`` 的 dict；若文件不存在或格式错误返回 ``None``
    """
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        logger.warning("missing/invalid frontmatter in %s", path)
        return None
    try:
        frontmatter = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        logger.warning("YAML parse error in %s: %s", path, e)
        return None
    if not isinstance(frontmatter, dict):
        logger.warning("frontmatter is not a mapping in %s", path)
        return None
    body = m.group(2)
    if len(body.encode("utf-8")) > MAX_SKILL_BODY_SIZE:
        logger.warning("body of %s exceeds %dKB, truncated", path, MAX_SKILL_BODY_SIZE // 1024)
        body = body[:MAX_SKILL_BODY_SIZE] + TRUNCATE_SUFFIX
    return {"frontmatter": frontmatter, "body": body, "path": path}


def _build_definition(
    parsed: dict[str, Any],
    skill_dir: Path,
    source: SkillSource,
) -> SkillDefinition:
    fm = parsed["frontmatter"]
    skill_id = skill_dir.name

    required = ["name", "description"]
    missing = [k for k in required if k not in fm]
    if missing:
        raise SkillValidationError(f"skill {skill_id!r} missing required fields: {missing}")

    skill_type: SkillType = fm.get("type", "atomic")
    if skill_type not in ("atomic", "composite"):
        raise SkillValidationError(f"skill {skill_id!r} invalid type: {skill_type!r}")

    scripts = _build_scripts(fm, skill_id, skill_dir)
    requires, exposure = _build_visibility(fm, skill_id)

    # === B 声明式编排：解析可选 orchestration（atomic 声明即报错，给清晰信息）===
    child_skills = frozenset(fm.get("child_skills", []) or [])
    orchestration = None
    raw_orch = fm.get("orchestration")
    if raw_orch is not None:
        if skill_type == "atomic":
            # 提前拦截，避免落到白名单校验报「引用未知 id」的误导信息
            raise SkillValidationError(
                f"atomic skill {skill_id!r} 不能声明 orchestration"
            )
        # 延迟 import：loader 在 import 链上游，orchestration 仅依赖 definition
        from taifeng.skill.orchestration import parse_orchestration

        orchestration = parse_orchestration(
            raw_orch, skill_id=skill_id, child_skills=child_skills
        )

    return SkillDefinition(
        id=skill_id,
        name=str(fm["name"]),
        description=str(fm["description"]),
        version=str(fm.get("version", "1.0.0")),
        body=parsed["body"],
        body_path=parsed["path"],
        type=skill_type,
        entry=bool(fm.get("entry", False)),
        child_skills=child_skills,
        tool_names=frozenset(fm.get("tool_names", []) or []),
        max_call_depth=int(fm.get("max_call_depth", 6)),
        model=fm.get("model"),
        requires=requires,
        exposure=exposure,
        frontmatter_raw=fm,
        scripts=scripts,
        source=source,
        orchestration=orchestration,
    )


def _build_visibility(
    fm: dict[str, Any], skill_id: str
) -> tuple[SkillRequirements, SkillExposure]:
    """从 frontmatter 解析 G4 可见性字段（``requires`` + ``exposure``）。

    ``requires`` 形如::

        requires:
          bins: [jq, rg]
          env: [OPENAI_API_KEY]
          os: [linux, darwin]

    ``exposure`` 形如 ``exposure: {model_invocable: false, user_invocable: true}``。
    缺省时全部用安全默认（无要求 / 全可见）。
    """
    raw_req = fm.get("requires") or {}
    if not isinstance(raw_req, dict):
        raise SkillValidationError(
            f"skill {skill_id!r} frontmatter requires 必须是 mapping"
        )
    requires = SkillRequirements(
        bins=frozenset(raw_req.get("bins", []) or []),
        env=frozenset(raw_req.get("env", []) or []),
        os=frozenset(raw_req.get("os", []) or []),
    )

    raw_exp = fm.get("exposure") or {}
    if not isinstance(raw_exp, dict):
        raise SkillValidationError(
            f"skill {skill_id!r} frontmatter exposure 必须是 mapping"
        )
    # child_recall 三值枚举校验：缺省回退 auto；非法值必须抛错（禁 silent fallback）
    raw_recall = raw_exp.get("child_recall", "auto")
    if raw_recall not in _CHILD_RECALL_VALUES:
        raise SkillValidationError(
            f"skill {skill_id!r} frontmatter exposure.child_recall 非法值 "
            f"{raw_recall!r}，合法值：{sorted(_CHILD_RECALL_VALUES)}"
        )
    exposure = SkillExposure(
        model_invocable=bool(raw_exp.get("model_invocable", True)),
        user_invocable=bool(raw_exp.get("user_invocable", True)),
        child_recall=raw_recall,
    )
    return requires, exposure


# 隐式发现支持的扩展名 → 默认 language 映射
_SCRIPT_EXT_LANGUAGE: dict[str, ScriptLanguage] = {
    ".sh": "shell",
    ".py": "python",
    ".js": "custom",
    ".ts": "custom",
}


def _resolve_script_path(skill_id: str, skill_dir: Path, raw_path: str) -> Path:
    """把 frontmatter 中的相对路径解析为绝对路径，并校验落在 skill 目录下。

    防止 ``../../etc/passwd`` 等越权脚本声明。
    """
    candidate = (skill_dir / raw_path).resolve()
    try:
        # Python 3.9+ ``is_relative_to`` —— 不在 skill_dir 下抛 ValueError
        candidate.relative_to(skill_dir.resolve())
    except ValueError as e:
        raise SkillValidationError(
            f"skill {skill_id!r} script path {raw_path!r} 落在 skill 目录之外（防越权）"
        ) from e
    return candidate


def _build_explicit_descriptor(
    skill_id: str,
    skill_dir: Path,
    entry: dict[str, Any],
) -> ScriptDescriptor:
    """从 frontmatter 显式声明构造 ``ScriptDescriptor``。"""
    required_keys = ("name", "path", "language")
    missing = [k for k in required_keys if k not in entry]
    if missing:
        raise SkillValidationError(
            f"skill {skill_id!r} script 显式声明缺字段 {missing}"
        )
    if "timeout_seconds" not in entry:
        raise SkillValidationError(
            f"skill {skill_id!r} script {entry['name']!r} 显式声明必须给出 timeout_seconds"
        )
    language = entry["language"]
    if language not in ("shell", "python", "custom"):
        raise SkillValidationError(
            f"skill {skill_id!r} script {entry['name']!r} 未知 language: {language!r}"
        )
    path = _resolve_script_path(skill_id, skill_dir, str(entry["path"]))
    args_schema = entry.get("args_schema") or {"type": "object"}
    if not isinstance(args_schema, dict):
        raise SkillValidationError(
            f"skill {skill_id!r} script {entry['name']!r} args_schema 必须是 mapping"
        )
    return ScriptDescriptor(
        skill_id=skill_id,
        name=str(entry["name"]),
        path=path,
        language=language,
        description=str(entry.get("description", "")),
        args_schema=args_schema,
        timeout_seconds=float(entry["timeout_seconds"]),
        max_output_bytes=int(entry.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
    )


def _build_implicit_descriptor(
    skill_id: str,
    skill_dir: Path,
    path: Path,
) -> ScriptDescriptor | None:
    """隐式发现兜底：根据文件扩展名生成 ``ScriptDescriptor``，未知扩展名返回 ``None``。"""
    language = _SCRIPT_EXT_LANGUAGE.get(path.suffix)
    if language is None:
        return None
    return ScriptDescriptor(
        skill_id=skill_id,
        name=path.stem,
        path=path,
        language=language,
        description="",
        args_schema={"type": "object"},
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    )


def _build_scripts(
    fm: dict[str, Any],
    skill_id: str,
    skill_dir: Path,
) -> tuple[ScriptDescriptor, ...]:
    """合并 frontmatter 显式声明 + 目录隐式发现，同名以显式为准。"""
    descriptors: dict[str, ScriptDescriptor] = {}

    # === 显式声明 ===
    explicit_entries = fm.get("scripts") or []
    if explicit_entries and not isinstance(explicit_entries, list):
        raise SkillValidationError(
            f"skill {skill_id!r} frontmatter scripts 必须是数组"
        )
    for entry in explicit_entries:
        if not isinstance(entry, dict):
            raise SkillValidationError(
                f"skill {skill_id!r} scripts 数组元素必须是 mapping"
            )
        descriptor = _build_explicit_descriptor(skill_id, skill_dir, entry)
        descriptors[descriptor.name] = descriptor

    # === 隐式发现（同名跳过）===
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir():
        for p in sorted(scripts_dir.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            implicit = _build_implicit_descriptor(skill_id, skill_dir, p)
            if implicit is None:
                continue
            if implicit.name in descriptors:
                continue
            descriptors[implicit.name] = implicit

    return tuple(descriptors.values())


def load_skills_from_dir(
    skills_dir: Path,
    *,
    source: SkillSource = "user",
) -> dict[str, SkillDefinition]:
    """扫描目录加载所有 SKILL.md。

    Raises:
        SkillValidationError: 任一 skill 自校验失败
        UnknownChildSkill: child_skills 引用未知 skill
        CircularSkillReference: 调用图存在环
    """
    if not skills_dir.is_dir():
        raise FileNotFoundError(f"skills dir not found: {skills_dir}")

    skills: dict[str, SkillDefinition] = {}
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill_md = entry / "SKILL.md"
        parsed = parse_skill_md(skill_md)
        if parsed is None:
            continue
        defn = _build_definition(parsed, entry, source=source)
        defn.validate()
        skills[defn.id] = defn
        logger.debug("loaded skill %s (type=%s, entry=%s)", defn.id, defn.type, defn.entry)

    # child_skills 引用完整性
    for s in skills.values():
        unknown = s.child_skills - set(skills.keys())
        if unknown:
            raise UnknownChildSkill(
                f"composite skill {s.id!r} references unknown child skills: {sorted(unknown)}"
            )

    # 静态环检测
    cycles = detect_cycles(skills)
    if cycles:
        formatted = "\n".join("  " + " → ".join(c) for c in cycles)
        raise CircularSkillReference(
            f"Detected {len(cycles)} cycle(s) in skill graph:\n{formatted}"
        )

    return skills


def compute_reachable_graph(
    skills: dict[str, SkillDefinition],
) -> dict[str, frozenset[str]]:
    """对每个 entry skill，预计算其可达的全部子 skill 集合。

    用于订阅校验：「订阅 entry skill X = 默认允许访问其调用图内的所有 skill」。
    """
    result: dict[str, frozenset[str]] = {}
    for s in skills.values():
        if not s.entry:
            continue
        visited: set[str] = set()
        stack = [s.id]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            defn = skills.get(cur)
            if defn is None:
                continue
            for child in defn.child_skills:
                if child not in visited:
                    stack.append(child)
        result[s.id] = frozenset(visited - {s.id})  # 排除自己
    return result
