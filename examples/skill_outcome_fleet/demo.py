"""skill_outcome_fleet demo —— 海量多领域 skill 舰队 + 战绩沉淀（认知回路 ⑦）。

25 个 skill = 5 个领域，每个是一个**自包含 mini-fleet**（1 个 composite 域编排器[entry]
+ 4 个 atomic 叶子）。**多目录加载**（每领域一个独立 skill root，验证 root 合并）。
本 demo 跑**几轮**，每轮把 5 个域 entry 各跑一遍，跨轮聚合每个 call_skill 叶子终态落下的
SkillExecutionRecord 战绩：

  R1 全绿        —— 5 个域全成功派发
  R2 注入故障叶  —— 一个叶子路由缺失 → 该叶失败（StructuralOutcomeJudge 判 failure）
  R3 业务判官    —— 注入自定义 OutcomeJudge（signal_source=business，演示 R1 业务注入缝）

最后打印「按 skill / 按领域 / 总览」三张战绩表，并自检 v1 不变量：
长相（selection_confidence）与战绩（outcome）分字段存、v1 恒 whitelist/None。

路由「从 skill 图自动生成」：composite → [fan-out call_skill 全部子, 汇总]；atomic → [终态文本]。
无需为 25 个 skill 手写脚本。

> 注：域编排器是各域 entry（根 turn），根不经 call_skill 故**不记**战绩；战绩只记被派发的叶子。

纯 SimClient，**无需 API key**：
    PYTHONPATH=src python examples/skill_outcome_fleet/demo.py
（若 skills/ 不存在，先跑 build_fleet.py 生成。）
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections import defaultdict
from pathlib import Path

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.skill.outcome import OutcomeVerdict, StructuralOutcomeJudge
from taifeng.skill.registry import FilesystemSkillRegistry, SkillSnapshot

HERE = Path(__file__).resolve().parent
SKILLS_ROOT = HERE / "skills"
# 5 个领域，每个一个独立 skill root（多目录加载 → root 合并）
DOMAINS = ("data", "research", "devops", "content", "analysis")
DOMAIN_DIRS = [SKILLS_ROOT / d for d in DOMAINS]


def _domain_of(skill_id: str) -> str:
    """据 skill_id 前缀归类领域（如 data-parse → data）。"""
    return skill_id.split("-", 1)[0]


def _usage() -> TokenUsage:
    """统一的每 turn 成本（demo 用固定值，便于核对聚合）。"""
    return TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30)


def build_routes(
    snapshot: SkillSnapshot, *, drop: set[str] | None = None
) -> dict[str, list[SimTurn]]:
    """从已加载的 skill 图自动生成 SimClient 路由。

    - composite skill → 两 turn：[并发 call_skill 全部子, 汇总文本]
    - atomic skill    → 一 turn：[终态文本]
    - ``drop`` 中的 skill 略去路由 → 被派发时 RoutingSimClient 抛 KeyError
      → 该子 turn end_reason=error → StructuralOutcomeJudge 判 failure（故障注入）。
    """
    drop = drop or set()
    routes: dict[str, list[SimTurn]] = {}
    for s in snapshot.skills:
        if s.id in drop:
            continue
        marker = f"<<ROUTE:{s.id}>>"  # 与 SKILL.md body 内嵌标记一致
        if s.type == "composite":
            children = sorted(s.child_skills)
            tool_calls = [
                {
                    "id": f"tc_{s.id}_{c}",
                    "name": "call_skill",
                    "arguments": json.dumps({"skill_id": c, "args": {}}),
                }
                for c in children
            ]
            routes[marker] = [
                SimTurn(
                    text=f"[{s.id}] 并发派发 {len(children)} 个子技能",
                    tool_calls=tool_calls,
                    usage=_usage(),
                ),
                SimTurn(text=f"[{s.id}] 子技能全部回流，汇总完成", usage=_usage()),
            ]
        else:
            routes[marker] = [SimTurn(text=f"[{s.id}] 执行完成 ✓", usage=_usage())]
    return routes


class _BusinessJudge:
    """演示业务注入 OutcomeJudge：复用结构性 status，但把信号来源标为 ``business``。

    真实业务可在 judge() 内回调权限/校验 API、或用 LLM-as-judge 决定战绩——内核只定协议。
    """

    def __init__(self) -> None:
        self._inner = StructuralOutcomeJudge()

    def judge(self, ctx: object) -> OutcomeVerdict:
        v = self._inner.judge(ctx)  # type: ignore[arg-type]
        return OutcomeVerdict(
            status=v.status, reason=f"business:{v.reason}", signal_source="business"
        )


async def _run_one_entry(engine: object, records: list[dict]) -> None:
    """提交一条 user 消息给某域 entry，跑到 outermost turn 完成，收集 skill_outcome 记录。"""
    done = asyncio.Event()

    async def watch() -> None:
        # 「未结算派发计数」推断 outermost turn 完成：dispatched(++)/returned(--)，
        # 仅当计数归零且根 turn 终态时退出。
        outstanding = 0
        async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
            kind = ev.msg.kind
            if kind == "skill_outcome_recorded":
                records.append(dict(ev.msg.data))
            elif kind == "skill_dispatched":
                outstanding += 1
            elif kind == "skill_returned":
                outstanding -= 1
            elif kind in ("turn_completed", "turn_failed") and outstanding == 0:
                done.set()
                return

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)  # 让 subscribe_all 先注册队列
    await engine.submit(taifeng.UserMessage(text="跑一遍本域 mini-fleet"))  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(done.wait(), timeout=30.0)
    finally:
        await asyncio.sleep(0.05)  # 给最后的旁路记账落盘留时间
        task.cancel()


async def run_round(
    name: str,
    threads_dir: Path,
    *,
    drop: set[str] | None = None,
    outcome_judge: object | None = None,
    max_session_tokens: int | None = None,
) -> list[dict]:
    """跑一轮：5 个域 entry 各跑一遍，返回本轮采集到的 skill_outcome 记录列表。

    ``max_session_tokens``：会话累计 token 上限（K2）。设很低时，跑到一半某子 turn
    触顶 → end_reason=resource_limit_exceeded → StructuralOutcomeJudge 判 abandoned，
    用于演示「放弃」战绩态。
    """
    # 先加载一次拿 snapshot 建路由（pool 内部会再加载一次，幂等）
    registry = await FilesystemSkillRegistry.load(DOMAIN_DIRS)
    routes = build_routes(registry.snapshot(), drop=drop)
    client = RoutingSimClient(routes=routes)
    pool = await taifeng.EnginePool.create(
        skills_dir=DOMAIN_DIRS,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        max_parallel_tool_calls=8,  # 让域编排器的 fan-out 真正并发
        outcome_judge=outcome_judge,  # None → 内核默认 StructuralOutcomeJudge
        max_session_tokens=max_session_tokens,
    )
    records: list[dict] = []
    try:
        for domain in DOMAINS:
            engine = await pool.get_or_create(
                session_id=f"fleet-{name}-{domain}",
                entry_skill_id=f"{domain}-orchestrator",
            )
            await _run_one_entry(engine, records)
    finally:
        await pool.close()
    return records


# ──────────────────────────────────────────────────────────────────────────
# 战绩聚合与打印
# ──────────────────────────────────────────────────────────────────────────

_OUTCOME_GLYPH = {"success": "✓", "failure": "✗", "abandoned": "⏸"}


def _print_per_skill(records: list[dict]) -> None:
    """按 skill 聚合：runs / 三态 / 成本 / 信号来源。"""
    agg: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "success": 0, "failure": 0, "abandoned": 0,
                 "tokens": 0, "iters": 0, "signals": set()}
    )
    for r in records:
        a = agg[r["skill_id"]]
        a["runs"] += 1
        a[r["outcome"]] += 1
        a["tokens"] += r["cost_tokens"]
        a["iters"] += r["cost_iterations"]
        a["signals"].add(r["outcome_signal_source"])

    print("\n按 skill 战绩（仅被派发的叶子；域编排器是各域 entry 根、不经 call_skill 故不记）")
    print(f"  {'skill_id':<22}{'runs':>5}{'✓':>4}{'✗':>4}{'⏸':>4}{'tokens':>8}{'iters':>6}  signal")
    print("  " + "-" * 68)
    for sid in sorted(agg):
        a = agg[sid]
        print(
            f"  {sid:<22}{a['runs']:>5}{a['success']:>4}{a['failure']:>4}"
            f"{a['abandoned']:>4}{a['tokens']:>8}{a['iters']:>6}  {','.join(sorted(a['signals']))}"
        )


def _print_per_domain(records: list[dict]) -> None:
    """按领域聚合：runs / 三态 / 成本。"""
    agg: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "success": 0, "failure": 0, "abandoned": 0, "tokens": 0}
    )
    for r in records:
        a = agg[_domain_of(r["skill_id"])]
        a["runs"] += 1
        a[r["outcome"]] += 1
        a["tokens"] += r["cost_tokens"]

    print("\n按领域战绩")
    print(f"  {'domain':<12}{'runs':>5}{'✓':>4}{'✗':>4}{'⏸':>4}{'tokens':>8}")
    print("  " + "-" * 41)
    for dom in sorted(agg):
        a = agg[dom]
        print(
            f"  {dom:<12}{a['runs']:>5}{a['success']:>4}{a['failure']:>4}"
            f"{a['abandoned']:>4}{a['tokens']:>8}"
        )


def _print_overview(records: list[dict]) -> None:
    """总览：总记录数、三态、信号来源、成本，以及 v1 不变量自检。"""
    by_outcome: dict[str, int] = defaultdict(int)
    by_signal: dict[str, int] = defaultdict(int)
    by_round: dict[str, int] = defaultdict(int)
    tokens = iters = 0
    for r in records:
        by_outcome[r["outcome"]] += 1
        by_signal[r["outcome_signal_source"]] += 1
        by_round[r.get("_round", "?")] += 1
        tokens += r["cost_tokens"]
        iters += r["cost_iterations"]

    print("\n总览")
    print(f"  战绩记录总数: {len(records)}（跨 {len(by_round)} 轮）")
    print("  按轮:   " + "  ".join(f"{k}={v}" for k, v in sorted(by_round.items())))
    print("  按战绩: " + "  ".join(
        f"{_OUTCOME_GLYPH[k]}{k}={v}" for k, v in sorted(by_outcome.items())))
    print("  按信号: " + "  ".join(f"{k}={v}" for k, v in sorted(by_signal.items())))
    print(f"  总成本: tokens={tokens}  iterations={iters}")

    # v1 不变量自检：长相 vs 战绩分离 —— selection 字段恒 whitelist/None，且不参与任何决策
    origins = {r["selection_origin"] for r in records}
    confidences = {r["selection_confidence"] for r in records}
    print("\nv1 不变量自检（长相 vs 战绩分离）")
    print(f"  selection_origin: {origins}（v1 恒 whitelist；discovered 待发现相位）")
    print(f"  selection_confidence: {confidences}（v1 恒 None；长相分只记录、严禁喂提拔）")
    assert origins == {"whitelist"}, "v1 selection_origin 应恒为 whitelist"
    assert confidences == {None}, "v1 selection_confidence 应恒为 None"
    print("  ✅ 长相字段存在但与战绩独立存储、未参与任何决策")


async def main() -> None:
    """加载舰队 → 跑 3 轮（每轮 5 个域 entry）→ 聚合并打印战绩三表。"""
    # R2 故意丢一个叶子路由制造失败（KeyError → end_reason=error → failure 战绩）；
    # 引擎会把这次「预期失败」记为 error 级日志打出整段 traceback——demo 自己的战绩表
    # 已清楚展示该失败，故抑制 taifeng 日志保持输出干净。
    logging.getLogger("taifeng").setLevel(logging.CRITICAL)
    if not SKILLS_ROOT.exists():
        raise SystemExit(
            "skills/ 不存在，请先运行：python examples/skill_outcome_fleet/build_fleet.py"
        )

    registry = await FilesystemSkillRegistry.load(DOMAIN_DIRS)
    snapshot = registry.snapshot()
    n_comp = sum(s.type == "composite" for s in snapshot.skills)
    n_atom = sum(s.type == "atomic" for s in snapshot.skills)
    print("=" * 72)
    print("技能舰队（skill_outcome_fleet）—— 海量多领域 skill + 战绩沉淀")
    print("=" * 72)
    print(
        f"舰队规模: {len(snapshot.skills)} skills "
        f"(composite={n_comp}, atomic={n_atom}, entry={len(snapshot.entries())}) "
        f"| 多目录加载: {len(DOMAIN_DIRS)} 个领域 root"
    )

    all_records: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        threads = Path(td) / "threads"
        rounds: list[tuple[str, dict]] = [
            ("R1-all-green", {}),
            ("R2-fault-leaf", {"drop": {"devops-rollback"}}),
            ("R3-business-judge", {"outcome_judge": _BusinessJudge()}),
        ]
        print(f"\n开始跑 {len(rounds)} 轮（每轮 5 个域 entry）：")
        for name, kw in rounds:
            recs = await run_round(name, threads, **kw)  # type: ignore[arg-type]
            for r in recs:
                r["_round"] = name
            n_fail = sum(r["outcome"] == "failure" for r in recs)
            print(f"  ▸ [{name}] 采集 {len(recs)} 条战绩（其中 failure={n_fail}）")
            all_records += recs

    _print_per_skill(all_records)
    _print_per_domain(all_records)
    _print_overview(all_records)
    print("\n🎉 skill_outcome_fleet：海量多领域舰队 + 战绩沉淀 演示完毕")


if __name__ == "__main__":
    asyncio.run(main())
