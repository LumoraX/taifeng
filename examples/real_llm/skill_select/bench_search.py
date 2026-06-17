"""真实 LLM「**deferred 召回**版」技能选择准确率基准 —— search_skills 先召回再选。

这是相位 0 基线 ``bench.py`` 的对照实验。两者测同一问题「成百上千 skill 时能不能选对」，
但**暴露方式相反**：

- ``bench.py``（inline 基线）：router 把全部 210 个候选**一次性内联**进 prompt，模型在
  210 条里**硬选**一个 call_skill。
- ``bench_search.py``（deferred 召回，本脚本）：router 走 **deferred 暴露**——prompt 里
  **不列** child，模型必须先 ``search_skills(query)`` 召回 top-K 个候选，**再**从召回结果里
  ``call_skill`` 选一个。把「在 200+ 里硬选」降为「在少数召回候选里选」。

复用 ``build_skills`` 的领域数据（``raw/*.json``），但**另建一棵临时 skills 树**：叶子照旧，
router 换成「先搜后调」指令体，且不在 frontmatter 声明 child_recall（保持 ``auto``）——deferred
由 ``EnginePool.create(recall_threshold=0)`` 强制触发（auto 模式下可见 child 数 >0 即 deferred）。

每任务的 LLM 调用：``search_skills`` 一次 + ``call_skill`` 一次（两轮），故 ``max_iterations=2``；
权限 deny ``Skill(*)`` 拦在 call_skill（叶子不执行），但**放行** ``search_skills``。

运行（需真实 LLM key，复用 examples/_provider_bootstrap 的 LLM_BOOTSTRAP_* env）：
    PYTHONPATH=src python examples/real_llm/skill_select/bench_search.py --sample 40

⚠️ 本脚本**只跑真实 LLM**（SimClient 不会"看召回候选挑对的那个"）。CI / 自检禁跑；
结果数值由集成者真实跑后填入 RESULTS_SEARCH.md（**严禁造假数据**）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # examples/ 进 sys.path 取共享 bootstrap
sys.path.insert(0, str(HERE))  # 同目录取 build_skills 的领域数据加载
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.context.budget import ContextBudget  # noqa: E402
from taifeng.permission.policy import PermissionPolicy  # noqa: E402

# 与 bench.py 一致的领域归类 + 叶子渲染（复用同目录 build_skills，避免重复实现）
from build_skills import _leaf_md, _load_entries  # noqa: E402

# deferred 召回版 router 指令体：不再说「立即 call_skill」，而是「先搜后调」。
# 注意 frontmatter **不声明** exposure.child_recall（保持 auto），deferred 由
# recall_threshold=0 在 EnginePool 侧强制——这样测的是「auto 超阈值自动切 deferred」真路径。
_SEARCH_ROUTER_BODY = """---
name: 技能路由器（召回版）
description: 顶层入口，先用 search_skills 召回最相关子技能，再从召回结果中选唯一最匹配的调用
version: 1.0.0
type: composite
entry: true
max_call_depth: 3
child_skills: [{children}]
---
# 技能路由器（召回版）

[Context 背景] 你面对成百上千个子技能，它们未列在 prompt 里，需主动检索发现。
用户的话口语化，往往不照搬技能描述的措辞——同一意图有多种说法，需按语义匹配。

[Role 角色] 你是技能路由器，也是语义检索专家：擅长把口语意图翻译成精准检索关键词，
并在召回候选中识别唯一最匹配的能力。

[Execute 执行指令] 为用户当前请求，找出并调用唯一最匹配的那个子技能。

[Action 动作步骤]
1. 拆意图：判断用户要完成什么、属哪类能力，写下 3-5 个能力关键词 + 同义词
   （如「留存」扩为「留存 复购 cohort 同期群 活跃」）——不要直接复述用户原话。
2. 召回：调用 `search_skills`，`query` = 上述关键词拼接（不是用户原话的复述）。
3. 评估+反思：读召回候选（每条含 skill_id / description / confidence）；
   有明确贴切的 → 进第 4 步；都不贴切或 confidence 普遍偏低 → 换一组关键词与同义词，回第 2 步再召回（最多 3 次）。
4. 派发：对选定候选立即 `call_skill`（`skill_id` = 选中 id，`args` = {{}}）。

[Target 目标与格式] 最终动作恰好一次 `call_skill`，调用唯一最匹配的子技能；不解释、不寒暄、不输出多余文本。
"""


def _domain_of(skill_id: str) -> str:
    """据 id 前缀归类领域（data-csv-parse → data），与 bench.py 一致。"""
    return skill_id.split("-", 1)[0]


def _build_search_skills_tree(dest: Path) -> tuple[int, list[dict]]:
    """在 ``dest`` 下生成「召回版」skills 树：N 个叶子 + 1 个召回 router entry。

    叶子与 bench.py 完全一致（复用 ``_leaf_md``）；router 换成「先搜后调」指令体。
    不写 tasks.json——任务直接从 ``_load_entries`` 取（task ↔ expected id），与 bench.py 同源。

    Args:
        dest: 临时 skills 根目录（写到这里，不污染仓库内固定路径）。

    Returns:
        (叶子数量, 全局技能条目列表)。
    """
    entries = _load_entries()
    ids = [e["id"] for e in entries]
    # 叶子（与 inline 基线同样的 atomic skill，保证两实验唯一变量是「暴露方式」）
    for e in entries:
        d = dest / e["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            _leaf_md(e["name"], e["description"]), encoding="utf-8"
        )
    # 召回版 router entry：child_skills 挂全部叶子，但指令是「先 search 再 call」
    rd = dest / "router"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "SKILL.md").write_text(
        _SEARCH_ROUTER_BODY.format(children=", ".join(ids)), encoding="utf-8"
    )
    return len(entries), entries


async def _select_one(
    pool: object, idx: int, task: str, timeout: float
) -> tuple[str | None, bool]:
    """提交一条 task 给召回 router，捕获最终 call_skill 的 skill_id + 是否调过 search_skills。

    与 bench.py 的 ``_select_one`` 区别：deferred 流是两步（search → call），故这里要
    等到 ``call_skill`` 的 tool_call_started 才算选定，并顺带记录是否先发生过 search_skills。

    Args:
        pool: EnginePool 实例。
        idx: 任务序号（拼 session_id）。
        task: 任务自然语言描述。
        timeout: 单任务超时秒。

    Returns:
        (选中的 skill_id 或 None, 是否调用过 search_skills)。
        None 表示模型最终没发出 call_skill（拒答 / 只搜不调 / 只输出文本）。
    """
    engine = await pool.get_or_create(  # type: ignore[attr-defined]
        session_id=f"selsearch-{idx}", entry_skill_id="router"
    )
    selected: str | None = None
    searched = False
    done = asyncio.Event()

    async def watch() -> None:
        nonlocal selected, searched
        async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
            kind = ev.msg.kind
            if kind == "tool_call_started":
                name = ev.msg.data.get("name")
                if name == "search_skills":
                    # 记录确实先走了召回（deferred 流的前置步）
                    searched = True
                elif name == "call_skill":
                    # 召回后的最终选择：捕获 skill_id 即收工
                    try:
                        selected = json.loads(
                            ev.msg.data.get("arguments", "{}")
                        ).get("skill_id")
                    except json.JSONDecodeError:
                        selected = None
                    done.set()
                    return
            if kind in ("turn_completed", "turn_failed"):
                # 没等到 call_skill 就终态（只搜不调 / 拒答）→ 未选出
                done.set()
                return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text=task))  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        watcher.cancel()
    return selected, searched


async def main() -> None:
    """跑 deferred 召回版选择基准并打印准确率报告（含召回触达率）。"""
    ap = argparse.ArgumentParser(description="真实 LLM 技能选择准确率基准（deferred 召回版）")
    ap.add_argument("--sample", type=int, default=40, help="抽样任务数（默认 40）")
    ap.add_argument("--seed", type=int, default=0, help="抽样随机种子")
    ap.add_argument("--timeout", type=float, default=120.0, help="单任务超时秒（两步流更长）")
    args = ap.parse_args()

    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        raise SystemExit(f"❌ 无法构造真实 LLM client：{exc}") from None

    # 权限：deny Skill(*) 拦最终 call_skill（叶子不执行）；search_skills 不在 Skill() 域，放行
    policy = PermissionPolicy.from_dict({"default_mode": "allow", "deny": ["Skill(*)"]})

    results: list[tuple[str, str, str | None, bool]] = []  # (task, expected, got, searched)
    with tempfile.TemporaryDirectory() as td:
        skills_dir = Path(td) / "skills"
        total_pool, entries = _build_search_skills_tree(skills_dir)
        tasks = [{"task": e["task"], "expected": e["id"]} for e in entries]

        rng = random.Random(args.seed)
        sample = (
            tasks if args.sample >= total_pool else rng.sample(tasks, args.sample)
        )

        print("=" * 72)
        print("技能选择准确率基准（真实 LLM · deferred 召回版 search_skills）")
        print("=" * 72)
        print(f"候选规模: {total_pool} 个 skill（router 走 deferred，prompt 不内联 child）")
        print(
            f"抽样任务: {len(sample)} 条 | provider={meta['provider']} model={meta['model']}"
        )
        print("ReAct 召回循环：search_skills（可重搜精炼）→ call_skill 选定"
              "（deny Skill(*) + max_iterations=5，留出重搜空间）\n")

        pool = await taifeng.EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=Path(td) / "threads",
            model_client=client,
            budget=ContextBudget(context_window=128_000),
            compressors=[],
            permission_policy=policy,
            # 关键：recall_threshold=0 → auto 模式下可见 child >0 即 deferred，强制走召回
            recall_threshold=0,
            # ReAct 召回循环：留出重搜空间（最多 3 次 search + 1 次 call），故 5 轮
            max_iterations=5,
        )
        try:
            started = time.monotonic()
            for i, t in enumerate(sample):
                got, searched = await _select_one(
                    pool, i, t["task"], args.timeout
                )
                ok = got == t["expected"]
                results.append((t["task"], t["expected"], got, searched))
                mark = "✓" if ok else "✗"
                srch = "🔍" if searched else "··"
                print(
                    f"  [{i + 1:>3}/{len(sample)}] {mark}{srch} "
                    f"期望={t['expected']:<26} 实选={got}"
                )
            elapsed = time.monotonic() - started
        finally:
            await pool.close()

    # ── 汇总 ──
    correct = sum(1 for _, exp, got, _ in results if exp == got)
    none_cnt = sum(1 for _, _, got, _ in results if got is None)
    searched_cnt = sum(1 for _, _, _, s in results if s)
    acc = correct / len(results) if results else 0.0

    dom_total: dict[str, int] = defaultdict(int)
    dom_ok: dict[str, int] = defaultdict(int)
    for _, exp, got, _ in results:
        d = _domain_of(exp)
        dom_total[d] += 1
        if exp == got:
            dom_ok[d] += 1

    print("\n" + "=" * 72)
    print(
        f"总准确率: {correct}/{len(results)} = {acc:.1%}"
        f"  （未选出: {none_cnt}）  耗时 {elapsed:.1f}s"
    )
    print(
        f"召回触达率: {searched_cnt}/{len(results)} = "
        f"{(searched_cnt / len(results) if results else 0.0):.1%}"
        "  （先调 search_skills 再选的比例）"
    )
    print("\n按领域准确率")
    print(f"  {'domain':<12}{'acc':>10}")
    print("  " + "-" * 22)
    for d in sorted(dom_total):
        a = dom_ok[d] / dom_total[d]
        print(f"  {d:<12}{dom_ok[d]}/{dom_total[d]} = {a:>5.0%}")

    errs = [(exp, got, task) for task, exp, got, _ in results if exp != got]
    if errs:
        print(f"\n错选明细（{len(errs)} 条）—— 期望 vs 实选 / 任务")
        for exp, got, task in errs:
            print(f"  ✗ {exp}  →  {got}")
            print(f"      「{task[:54]}」")

    print("\n🎯 deferred 召回版选择基准完毕")


if __name__ == "__main__":
    asyncio.run(main())
