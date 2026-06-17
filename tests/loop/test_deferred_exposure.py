"""T6 deferred 暴露：per-turn search_skills 工具裁剪 + recall_threshold 透传。

覆盖三层：
- C3 工具裁剪：inline entry 的 per-turn 工具清单**不含** search_skills；
  deferred entry **含** search_skills（与 inline 文本判定同源，effective_child_recall）；
- 阈值业务可配：构造 EnginePool 时传不同 recall_threshold，行为随之变；
- frontmatter 强制 override：deferred 小 entry 也走 deferred、inline 大 entry 也走 inline。

工具可见性断言走 SimClient ledger（``client.ledger.tool_names()`` = 本轮真实请求 tools），
这是「LLM 真实看得到哪些工具」的唯一真相，比内部裁剪函数断言更贴 C3 契约。
"""

from __future__ import annotations

from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.skill.recall import KeywordSkillRecall, SkillRecall


def _write_entry_with_children(
    root: Path, *, child_recall: str | None, n_children: int
) -> Path:
    """写一个 composite entry + n 个 atomic child；child_recall 可选显式声明。

    Args:
        root: 临时根目录（skills 写到 root/skills 下）。
        child_recall: ``inline`` / ``deferred`` / ``None``（None=不写=默认 auto）。
        n_children: 生成的 atomic child 数量。

    Returns:
        skills 目录路径（供 EnginePool.create 的 skills_dir）。
    """
    skills = root / "skills"
    child_ids = [f"child-{i:02d}" for i in range(n_children)]
    exposure_block = (
        f"exposure:\n  child_recall: {child_recall}\n"
        if child_recall is not None
        else ""
    )
    entry_md = (
        "---\n"
        "name: orchestrator\n"
        "description: 编排\n"
        "version: 1.0.0\n"
        "type: composite\n"
        "entry: true\n"
        "model: mock-model\n"
        f"child_skills: [{', '.join(child_ids)}]\n"
        f"{exposure_block}"
        "---\n# 编排\n"
    )
    d = skills / "orchestrator"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(entry_md, encoding="utf-8")
    for cid in child_ids:
        cd = skills / cid
        cd.mkdir(parents=True)
        (cd / "SKILL.md").write_text(
            f"---\nname: {cid}\ndescription: 子技能 {cid}\n"
            "version: 1.0.0\ntype: atomic\n---\n# {cid}\n",
            encoding="utf-8",
        )
    return skills


async def _offered_tools(
    skills_dir: Path,
    threads_dir: Path,
    *,
    recall_threshold: int,
    skill_recall: SkillRecall | None = None,
) -> set[str]:
    """跑一个 turn，返回本轮请求真实暴露的 tool 名集（SimClient ledger）。

    ``skill_recall`` 默认 None = 无召回后端 = inline（设计稿默认，不暴露 search）；
    需要 deferred / search_skills 生效的用例须显式注入后端。
    """
    client = SimClient(turns=[SimTurn(text="完成")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        recall_threshold=recall_threshold,
        skill_recall=skill_recall,
    )
    try:
        engine = await pool.get_or_create(
            session_id="s", entry_skill_id="orchestrator"
        )
        sub = await engine.submit(taifeng.UserMessage(text="开始"))
        async for ev in engine.subscribe(sub):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                assert ev.msg.kind == "turn_completed"
                break
        return set(client.ledger.tool_names())
    finally:
        await pool.close()


# ── C3：inline entry 不暴露 search_skills；deferred entry 暴露 ───────────────


async def test_inline_entry_omits_search_skills(tmp_path: Path) -> None:
    """auto entry + child 数 <= threshold → inline → 不暴露 search_skills。"""
    skills = _write_entry_with_children(tmp_path, child_recall=None, n_children=3)
    tools = await _offered_tools(
        skills, tmp_path / "threads", recall_threshold=50
    )
    assert "search_skills" not in tools
    # 内核恒备二件套仍在（向后兼容）
    assert "read_skill" in tools
    assert "call_skill" in tools


async def test_deferred_entry_offers_search_skills(tmp_path: Path) -> None:
    """auto entry + child 数 > threshold + 注入召回后端 → deferred → 暴露 search_skills。"""
    skills = _write_entry_with_children(tmp_path, child_recall=None, n_children=4)
    tools = await _offered_tools(
        skills,
        tmp_path / "threads",
        recall_threshold=2,
        skill_recall=KeywordSkillRecall(),
    )
    assert "search_skills" in tools


async def test_default_no_backend_always_inline(tmp_path: Path) -> None:
    """默认 skill_recall=None：即便 auto entry child 数远超 threshold 也恒 inline。

    设计稿 §3.1 默认语义——无召回后端 = 工作记忆/LLM 注意力 = inline（LLM 自己找）：
    prompt 逐条列 child、per-turn 工具**不含** search_skills（根本未注册）。
    """
    skills = _write_entry_with_children(tmp_path, child_recall=None, n_children=10)
    # threshold=1，child=10 远超——若有后端会 deferred；无后端（默认）恒 inline
    tools = await _offered_tools(
        skills, tmp_path / "threads", recall_threshold=1
    )
    assert "search_skills" not in tools
    # 内核恒备二件套仍在（inline 路径，child 列在 prompt 文本里）
    assert "read_skill" in tools
    assert "call_skill" in tools


# ── 阈值业务可配：同一 skills 目录，threshold 不同 → 行为不同 ────────────────


async def test_recall_threshold_is_configurable(tmp_path: Path) -> None:
    """同一 3-child auto entry（均注入后端）：threshold=50 → inline 无 search；
    threshold=1 → deferred 有。阈值业务可配（前提是注入了召回后端）。"""
    skills = _write_entry_with_children(tmp_path, child_recall=None, n_children=3)
    high = await _offered_tools(
        skills,
        tmp_path / "threads-high",
        recall_threshold=50,
        skill_recall=KeywordSkillRecall(),
    )
    low = await _offered_tools(
        skills,
        tmp_path / "threads-low",
        recall_threshold=1,
        skill_recall=KeywordSkillRecall(),
    )
    assert "search_skills" not in high
    assert "search_skills" in low


# ── frontmatter 强制 override：声明优先于阈值 ──────────────────────────────


async def test_deferred_override_small_entry_offers_search(tmp_path: Path) -> None:
    """child_recall: deferred 的小 entry（child 远低于 threshold）也暴露 search_skills。"""
    skills = _write_entry_with_children(
        tmp_path, child_recall="deferred", n_children=2
    )
    tools = await _offered_tools(
        skills,
        tmp_path / "threads",
        recall_threshold=50,
        skill_recall=KeywordSkillRecall(),
    )
    assert "search_skills" in tools


async def test_inline_override_large_entry_omits_search(tmp_path: Path) -> None:
    """child_recall: inline 的大 entry（child 远超 threshold）也不暴露 search_skills。"""
    skills = _write_entry_with_children(
        tmp_path, child_recall="inline", n_children=10
    )
    tools = await _offered_tools(
        skills, tmp_path / "threads", recall_threshold=2
    )
    assert "search_skills" not in tools


# ── M1 去重：作者显式声明 search_skills + deferred → 清单里只出现一次 ──────────


def _write_deferred_entry_declaring_search(root: Path) -> Path:
    """写一个 deferred composite entry，并在 tool_names 显式声明 search_skills。

    这构造 M1 评审的撕裂场景：deferred 分支会想 append search_skills，而作者已在
    tool_names 里声明它——若不去重，per-turn 清单里 search_skills 会出现两次。

    Args:
        root: 临时根目录。

    Returns:
        skills 目录路径。
    """
    skills = root / "skills"
    entry_md = (
        "---\n"
        "name: orchestrator\n"
        "description: 编排\n"
        "version: 1.0.0\n"
        "type: composite\n"
        "entry: true\n"
        "model: mock-model\n"
        "child_skills: [child-00, child-01]\n"
        # 作者显式声明 search_skills（合法：composite 可声明任意已注册工具名）
        "tool_names: [search_skills]\n"
        "exposure:\n  child_recall: deferred\n"
        "---\n# 编排\n"
    )
    d = skills / "orchestrator"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(entry_md, encoding="utf-8")
    for cid in ("child-00", "child-01"):
        cd = skills / cid
        cd.mkdir(parents=True)
        (cd / "SKILL.md").write_text(
            f"---\nname: {cid}\ndescription: 子技能 {cid}\n"
            "version: 1.0.0\ntype: atomic\n---\n# {cid}\n",
            encoding="utf-8",
        )
    return skills


async def _offered_tool_list(
    skills_dir: Path,
    threads_dir: Path,
    *,
    recall_threshold: int,
    skill_recall: SkillRecall | None = None,
) -> list[str]:
    """跑一个 turn，返回本轮请求 tools 的**原始名列表**（保留重复，供去重断言）。

    注意：必须读 ``last_request().request.tools`` 的原始序列——``ledger.tool_names()``
    返回 set 会先把重复折叠掉，无法捕获「同名工具被 append 两次」的 M1 撕裂。

    ``skill_recall`` 默认 None；deferred 用例须注入后端才会暴露 search_skills。
    """
    client = SimClient(turns=[SimTurn(text="完成")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        recall_threshold=recall_threshold,
        skill_recall=skill_recall,
    )
    try:
        engine = await pool.get_or_create(
            session_id="s", entry_skill_id="orchestrator"
        )
        sub = await engine.submit(taifeng.UserMessage(text="开始"))
        async for ev in engine.subscribe(sub):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                assert ev.msg.kind == "turn_completed"
                break
        last = client.ledger.last_request()
        assert last is not None
        # 读原始 tools 序列（保留重复），而非去重后的 tool_names() 集合
        return [t.name for t in last.request.tools]
    finally:
        await pool.close()


async def test_deferred_entry_declaring_search_skills_lists_it_once(
    tmp_path: Path,
) -> None:
    """deferred entry 在 tool_names 显式声明 search_skills → per-turn 清单里只出现一次。"""
    skills = _write_deferred_entry_declaring_search(tmp_path)
    names = await _offered_tool_list(
        skills,
        tmp_path / "threads",
        recall_threshold=50,
        skill_recall=KeywordSkillRecall(),
    )
    # 仍然暴露（deferred 模式必须有），但不能重复
    assert "search_skills" in names
    assert names.count("search_skills") == 1


# ── 禁 silent fallback：显式 deferred 但无召回后端 → turn 失败（抛错，不静默降级）──


async def test_deferred_without_backend_fails_turn(tmp_path: Path) -> None:
    """显式 child_recall: deferred 但 pool 无召回后端（默认）→ turn 失败。

    effective_child_recall 抛 SkillValidationError（禁止静默降级 inline），被
    turn 主循环捕获为 turn_failed——验证「作者显式要召回却没后端」不被吞掉。
    """
    skills = _write_entry_with_children(
        tmp_path, child_recall="deferred", n_children=2
    )
    client = SimClient(turns=[SimTurn(text="完成")])
    # 不注入 skill_recall（默认 None = 无后端）
    pool = await taifeng.EnginePool.create(
        skills_dir=skills,
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        recall_threshold=50,
    )
    try:
        engine = await pool.get_or_create(
            session_id="s", entry_skill_id="orchestrator"
        )
        sub = await engine.submit(taifeng.UserMessage(text="开始"))
        kinds: list[str] = []
        async for ev in engine.subscribe(sub):
            kinds.append(ev.msg.kind)
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break
        # 显式 deferred 无后端：禁静默降级 → turn 失败而非 inline 完成
        assert kinds[-1] == "turn_failed"
    finally:
        await pool.close()
