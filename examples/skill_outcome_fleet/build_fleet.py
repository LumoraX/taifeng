"""生成「技能舰队」—— 海量、多领域、多类型的 SKILL.md（分领域单独写）。

本脚本一次性写出 25 个 skill，验证 taifeng 在「成百上千 skill」规模下的两件事：
  1. 多类型 + 多方向：5 个领域，每个是一个自包含 mini-fleet =（1 个 composite 域编排器[entry]
     + 4 个 atomic 叶子）
  2. 战绩沉淀（认知回路 ⑦）：域编排器 call_skill 派发的每个叶子终态都会落一条 SkillExecutionRecord

目录布局（**分领域单独写** —— 每个领域一个独立 skill root，demo 用**多目录加载**验证 root 合并；
每个领域自包含，域编排器只引用本域叶子，故 per-dir child_skills 校验通过）：
    skills/data/       data-orchestrator[entry] + 4 leaf
    skills/research/   research-orchestrator[entry] + 4 leaf
    skills/devops/     devops-orchestrator[entry] + 4 leaf
    skills/content/    content-orchestrator[entry] + 4 leaf
    skills/analysis/   analysis-orchestrator[entry] + 4 leaf

> 注：taifeng loader 按目录各自校验 child_skills 引用，跨目录引用会被判「未知子 skill」。
> 故采「每域自包含」布局——域编排器是该域 entry、只派发本域叶子；demo 每轮跑 5 个域 entry。

每个 SKILL.md body 内嵌唯一路由标记 ``<<ROUTE:{skill_id}>>``——demo.py 据此「从 skill 图
自动生成」SimClient 路由（composite → fan-out call_skill 子 + 汇总；atomic → 终态文本），
无需为 25 个 skill 手写脚本。

运行（纯 stdlib，无需 venv / API key）：
    python examples/skill_outcome_fleet/build_fleet.py
"""

from __future__ import annotations

from pathlib import Path

# ── 领域 → （领域显示名, 该域 4 个 atomic 叶子: (id, 显示名, 描述)）──────────────
# 刻意覆盖「多个方向」：数据处理 / 研究 / 运维 / 内容 / 分析，皆为通用能力（无业务领域词）
DOMAINS: dict[str, tuple[str, list[tuple[str, str, str]]]] = {
    "data": (
        "数据处理",
        [
            ("data-parse", "数据解析", "把原始字节/文本解析为结构化记录（CSV/JSON/XML 自适应）"),
            ("data-validate", "数据校验", "对结构化记录做 schema 与约束校验，标记脏数据"),
            ("data-transform", "数据转换", "字段映射、单位归一、类型转换与派生列计算"),
            ("data-aggregate", "数据聚合", "分组聚合、统计汇总与窗口计算"),
        ],
    ),
    "research": (
        "研究调研",
        [
            ("research-search", "来源检索", "按问题检索候选信息源并去重"),
            ("research-summarize", "要点摘要", "把长文档压缩为带引用的要点列表"),
            ("research-factcheck", "事实核验", "对断言做交叉印证，标注可信度"),
            ("research-cite", "引用编排", "整理来源为规范化引用清单"),
        ],
    ),
    "devops": (
        "运维",
        [
            ("devops-logscan", "日志扫描", "扫描日志流，抽取异常签名与错误聚类"),
            ("devops-healthprobe", "健康探测", "探测服务健康端点，汇总存活/延迟指标"),
            ("devops-deploycheck", "发布检查", "校验发布前置条件（迁移/配置/依赖）"),
            ("devops-rollback", "回滚预案", "生成可执行的回滚步骤与影响面评估"),
        ],
    ),
    "content": (
        "内容生产",
        [
            ("content-draft", "初稿撰写", "按主题与受众生成结构化初稿"),
            ("content-edit", "编辑润色", "对初稿做语病修订、结构优化与精简"),
            ("content-translate", "译写", "在保留术语一致性的前提下做跨语言译写"),
            ("content-tone", "语气调校", "按目标语气（正式/亲和/简洁）调校文本"),
        ],
    ),
    "analysis": (
        "分析",
        [
            ("analysis-classify", "分类", "把输入归入预定义类目并给出置信度"),
            ("analysis-sentiment", "情感分析", "判定文本情感极性与强度"),
            ("analysis-score", "评分", "按多维度规则对对象打分并加权汇总"),
            ("analysis-extract", "信息抽取", "抽取实体、关系与关键字段"),
        ],
    ),
}

ROOT = Path(__file__).resolve().parent / "skills"


def _atomic_md(skill_id: str, display: str, desc: str) -> str:
    """渲染一个 atomic 叶子 skill 的 SKILL.md。"""
    # 注：atomic skill 不允许声明 model 偏好（loader 校验拒绝），故此处不带 model
    return f"""---
name: {display}
description: {desc}
version: 1.0.0
type: atomic
---
# {display}

{desc}

> 这是「技能舰队」demo 的一个原子叶子技能（atomic leaf），由所属领域编排器派发。
> 运行时它产出一次终态结果，触发一条 SkillExecutionRecord 战绩记录。

<<ROUTE:{skill_id}>>
"""


def _composite_md(
    skill_id: str,
    display: str,
    desc: str,
    children: list[str],
    *,
    entry: bool = False,
) -> str:
    """渲染一个 composite 编排器 skill 的 SKILL.md（含 child_skills 白名单）。"""
    children_yaml = ", ".join(children)
    entry_line = "entry: true\n" if entry else ""
    role = "领域编排器（entry composite）" if entry else "编排器（composite）"
    return f"""---
name: {display}
description: {desc}
version: 1.0.0
type: composite
{entry_line}child_skills: [{children_yaml}]
max_call_depth: 6
model: mock-model
---
# {display}

{desc}

> 这是「技能舰队」demo 的{role}。它通过 ``call_skill`` 并发派发下列子技能，
> 每个子技能终态都会落一条 SkillExecutionRecord：
>
> {children}

<<ROUTE:{skill_id}>>
"""


def _write(path: Path, text: str) -> None:
    """写一个 SKILL.md（建目录 + 落盘）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build() -> int:
    """生成全部 skill（每域自包含 mini-fleet），返回写出的 skill 数量。"""
    count = 0

    # ── 各领域：1 个 composite 域编排器（entry）+ 4 个 atomic 叶子，全在本域目录内 ──
    for domain, (domain_display, leaves) in DOMAINS.items():
        orch_id = f"{domain}-orchestrator"
        leaf_ids = [lid for (lid, _, _) in leaves]

        # 域编排器作为本域 entry（只引用本域叶子 → per-dir 校验通过）
        _write(
            ROOT / domain / orch_id / "SKILL.md",
            _composite_md(
                orch_id,
                f"{domain_display}编排器",
                f"{domain_display}领域入口：并发派发本域 {len(leaf_ids)} 个原子技能并汇总",
                leaf_ids,
                entry=True,
            ),
        )
        count += 1

        for lid, ldisplay, ldesc in leaves:
            _write(ROOT / domain / lid / "SKILL.md", _atomic_md(lid, ldisplay, ldesc))
            count += 1

    return count


if __name__ == "__main__":
    n = build()
    print(f"✅ 生成 {n} 个 skill 到 {ROOT}")
    for p in sorted(ROOT.rglob("SKILL.md")):
        print(f"  - {p.relative_to(ROOT.parent)}")
