"""从 raw/*.json 生成「选择基准」技能集：N 个 atomic 叶子 + 1 个 router entry。

router（entry composite）把全部 N 个叶子挂为 child_skills——这样一次 turn 的 prompt 里会列出
全部 N 条 `id: 能力描述`，**模型必须从这 N 条里选出唯一最匹配的一个并 call_skill**。这正是
「成百上千 skill 时能不能选对」的核心被测点。

每个 raw/*.json 是一个领域的 [{id,name,description,task}, ...]；本脚本：
  1. 合并所有领域 → 全局技能列表（id 必须全局唯一）
  2. 生成 skills/<id>/SKILL.md（atomic 叶子，frontmatter.description = 能力描述）
  3. 生成 skills/router/SKILL.md（entry，child_skills = 全部叶子 id）
  4. 生成 tasks.json：[{task, expected}]，供 bench.py 评准确率（task↔正确目标 skill）

运行（纯 stdlib）：
    python examples/real_llm/skill_select/build_skills.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
SKILLS_DIR = HERE / "skills"
TASKS_PATH = HERE / "tasks.json"

_ROUTER_BODY = """---
name: 技能路由器
description: 顶层入口，根据用户请求从全部子技能中选出唯一最匹配的一个并调用
version: 1.0.0
type: composite
entry: true
max_call_depth: 3
child_skills: [{children}]
---
# 技能路由器

你是一个**技能路由器**。你拥有大量子技能，每个子技能有一个 id 和一句能力描述
（见 available_child_skills 列表）。

**你的唯一职责**：阅读用户的请求，理解其真实意图，从所有子技能中选出**唯一最匹配**的
那一个，立即用 `call_skill` 调用它（`skill_id` = 选中的 id，`args` = {{}}）。

规则：
- 只调用**一个**子技能，选最贴合用户意图的那个。
- 不要解释、不要寒暄、不要输出额外文本，直接调用。
- 用户的话往往是口语化的需求描述，不会照搬技能描述的措辞——按语义匹配。
"""


def _leaf_md(name: str, description: str) -> str:
    """渲染一个 atomic 叶子 skill 的 SKILL.md（description 进 frontmatter，供路由列表展示）。"""
    # description 里的冒号等对 YAML 安全：用双引号包裹并转义内部双引号
    safe_desc = description.replace('"', '\\"')
    safe_name = name.replace('"', '\\"')
    return f"""---
name: "{safe_name}"
description: "{safe_desc}"
version: 1.0.0
type: atomic
---
# {name}

{description}

> 选择基准叶子技能：被路由器选中即代表「该任务应由我处理」。本 demo 中叶子不实际执行
> （bench 用权限拒绝拦在 call_skill，只看路由器选了谁）。
"""


def _load_entries() -> list[dict]:
    """合并 raw/*.json，校验 id 全局唯一，返回全局技能列表。"""
    entries: list[dict] = []
    seen: set[str] = set()
    for jp in sorted(RAW_DIR.glob("*.json")):
        rows = json.loads(jp.read_text(encoding="utf-8"))
        for r in rows:
            sid = r["id"]
            if sid in seen:
                raise SystemExit(f"重复 skill id: {sid!r}（来自 {jp.name}）")
            seen.add(sid)
            entries.append(r)
    return entries


def build() -> int:
    """生成全部叶子 + router entry + tasks.json，返回叶子数量。"""
    entries = _load_entries()
    ids = [e["id"] for e in entries]

    # 叶子
    for e in entries:
        d = SKILLS_DIR / e["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            _leaf_md(e["name"], e["description"]), encoding="utf-8"
        )

    # router entry：child_skills 挂全部叶子
    rd = SKILLS_DIR / "router"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "SKILL.md").write_text(
        _ROUTER_BODY.format(children=", ".join(ids)), encoding="utf-8"
    )

    # tasks.json：task ↔ 正确目标 skill
    tasks = [{"task": e["task"], "expected": e["id"]} for e in entries]
    TASKS_PATH.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(entries)


if __name__ == "__main__":
    n = build()
    print(f"✅ 生成 {n} 个叶子 + 1 个 router entry 到 {SKILLS_DIR}")
    print(f"✅ {n} 条任务写入 {TASKS_PATH}")
