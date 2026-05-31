"""Skill 系统 —— 统一抽象（无 Agent 概念）。

按 ADR 0006，所有能力单元都是 ``SkillDefinition``，原子 / 组合通过
``type`` 字段区分；composite skill 通过 ``call_skill`` 调用子 skill，
受 ``DispatchPolicy`` 深度 / 白名单 / 环检测约束。

设计文档：docs/architecture/skill-system.md
决策记录：
    - docs/decisions/0003-skill-as-context.md（skill = 上下文范式）
    - docs/decisions/0006-unified-skill-model.md（统一 skill，删除 Agent）
"""

from taifeng.skill.definition import SkillDefinition, SkillType, SkillValidationError
from taifeng.skill.dispatch import (
    CallFrame,
    CallStack,
    CircularSkillReference,
    DispatchPolicy,
    DispatchVerdict,
    detect_cycles,
)
from taifeng.skill.loader import (
    SkillLoadError,
    UnknownChildSkill,
    compute_reachable_graph,
    load_skills_from_dir,
    parse_skill_md,
)
from taifeng.skill.registry import FilesystemSkillRegistry, SkillRegistry, SkillSnapshot
from taifeng.skill.watcher import SkillFileWatcher

__all__ = [
    "CallFrame",
    "CallStack",
    "CircularSkillReference",
    "DispatchPolicy",
    "DispatchVerdict",
    "FilesystemSkillRegistry",
    "SkillFileWatcher",
    "SkillDefinition",
    "SkillLoadError",
    "SkillRegistry",
    "SkillSnapshot",
    "SkillType",
    "SkillValidationError",
    "UnknownChildSkill",
    "compute_reachable_graph",
    "detect_cycles",
    "load_skills_from_dir",
    "parse_skill_md",
]
