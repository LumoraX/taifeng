"""真实 LLM 能力矩阵 —— 把所有能力场景用真实 key 跑一遍，采集事件日志后判定成败 + 审计可观测完整性。

这是"所有能力场景覆盖都用真实 key 测试 + 分析 logs"的可复跑产物。对每个能力场景：
    1. 复用 examples/<demo>/skills 下的真实 skill 包（与 web_ui 同源）；
    2. 真实 LLM 跑一轮代表性 prompt；
    3. 同时挂 console sink（人读）+ JsonlSink（机读，落 logs/<demo>.jsonl）+ 内存采集；
    4. 跑完按事件流判定：是否 turn_completed、关键能力事件是否出现。

收尾两份报告：
    A. 能力成败矩阵（逐 demo：完成？关键事件齐？）
    B. R3 可观测完整性审计（所有发出的事件 kind 是否都有专用渲染，无静默吞没/`?` 兜底）

权限/HITL 场景用 AutoGrantPrompter 自动放行（无人值守），放行动作本身进事件流可被观测。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# examples/ 进 sys.path，import 共享 bootstrap
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _ledger import LedgerWriter, R3Audit, ScenarioRecord  # noqa: E402
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.context.budget import ContextBudget  # noqa: E402
from taifeng.context.strategies.sliding import SlidingWindowStrategy  # noqa: E402
from taifeng.permission import (  # noqa: E402
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
)
from taifeng.skill.scripts.python import PythonScriptExecutor  # noqa: E402
from taifeng.skill.scripts.shell import ShellScriptExecutor  # noqa: E402
from taifeng.telemetry.console import _KIND_TAG, attach_console_sink  # noqa: E402
from taifeng.telemetry.jsonl_sink import attach_jsonl_sink  # noqa: E402

EXAMPLES_DIR = Path(__file__).resolve().parent.parent

# R3 红线要求关键路径必打的"经典事件"——审计时核对这些是否在矩阵里被观测到
R3_CANONICAL = {
    "turn_started", "tool_call_started", "tool_call_completed",
    "skill_dispatched", "skill_returned", "turn_completed",
}


@dataclass
class Scenario:
    """一个能力场景的跑测定义。"""

    demo_id: str
    skills_subdir: str            # examples/<subdir>/skills
    entry: str
    prompt: str
    capability: str               # 这条覆盖的能力名（报告用）
    expect: set[str]              # 期望出现的关键事件 kind（判定能力是否真触发）
    sliding: bool = False         # 挂 SlidingWindow 压缩器
    ctx_window: int | None = None  # 覆盖 context_window（小窗逼出压缩）


# 能力矩阵 —— skill 包与 web_ui DEMOS 同源；prompt 取代表性输入
SCENARIOS: list[Scenario] = [
    Scenario("composite_dispatch", "code_review", "programmer",
             "请审查这段代码：\n```python\ndef login(u, p):\n    q = \"SELECT * FROM users WHERE name='\" + u + \"'\"\n    return db.exec(q)\n```",
             capability="composite call_skill 派发 + HITL",
             expect={"skill_dispatched", "turn_completed"}),
    Scenario("read_skill_lazy", "read_skill_lazy", "knowledge-router",
             "我的登录接口怎么防 SQL 注入？",
             capability="read_skill 懒加载（skill-as-context）",
             expect={"tool_call_started", "turn_completed"}),
    Scenario("orchestration", "orchestration", "trip-planner",
             "帮我规划一次 3 天周末出游，给两条线路对比并按需提示天气。",
             capability="声明式编排 parallel/serial/when",
             expect={"skill_dispatched", "turn_completed"}),
    Scenario("concurrent_fanout", "concurrent_fanout", "research-fanout",
             "请就『城市夜间经济的利弊』做调研：请**同时**从网络、学术、新闻三个相互独立的信息源各自取证（在同一条消息里 fan-out 三个 call_skill 并发执行），最后综合成结论。",
             capability="并发 fan-out（LLM 自主并行派发）",
             expect={"skill_dispatched", "turn_completed"}),
    Scenario("research_pipeline", "research_assistant", "research-lead",
             "请围绕「AI agent 框架的市场格局与瓶颈」做一次简短调研。",
             capability="串行 pipeline（采集→提炼→写作）",
             expect={"skill_dispatched", "turn_completed"}),
    Scenario("product_review", "product_review", "product-manager",
             "请评审以下 PRD：做一个面向独立开发者的 LLM agent 可观测面板，提供事件流、token 占比、压缩可视化。",
             capability="fan-out 多 reviewer + 评分聚合",
             expect={"skill_dispatched", "turn_completed"}),
    Scenario("numeric_loop", "numeric_loop", "numeric-tuner",
             "请把 current=10.0 调谐到 target=42.0，用 run_script(apply_delta) 多轮逼近，误差 ±0.5 即可。",
             capability="多轮 run_script 数值调谐（工具循环）",
             expect={"tool_call_started", "turn_completed"}),
    Scenario("compression", "compression_showcase", "chatty-assistant",
             "请详细讲讲 Python 装饰器怎么工作，举 3 个例子并比较它们的差异，越详细越好。",
             capability="上下文压缩（sliding，小窗触发）",
             expect={"turn_completed"}, sliding=True, ctx_window=1024),
    Scenario("selective_approval", "selective_approval", "analysis-orchestrator",
             "请同时做 PRD 评估 + SWOT 战略分析：目标是一个 LLM agent 微内核的开发者工具。",
             capability="差异化授权 + 多路派发",
             expect={"skill_dispatched", "turn_completed"}),
    Scenario("travel_planner", "travel_planner", "trip-planner",
             "帮我规划 9 月 12-15 日从北京到巴黎的 3 天旅行。",
             capability="三路 fan-out（航班/酒店/活动）+ 综合",
             expect={"skill_dispatched", "turn_completed"}),
]


class AutoGrantPrompter:
    """无人值守 prompter —— 所有 HITL 审批自动放行，并记录放行次数。"""

    def __init__(self) -> None:
        self.grants = 0

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        self.grants += 1
        return PermissionDecision.allow(reason="capability_matrix_auto_grant")


@dataclass
class Result:
    """单场景跑测结果。"""

    scenario: Scenario
    completed: bool = False
    failed: bool = False
    error: str = ""
    kinds: Counter = field(default_factory=Counter)
    grants: int = 0
    duration_s: float = 0.0


async def run_scenario(client: object, sc: Scenario, logs_dir: Path) -> Result:
    """跑单个能力场景，返回采集到的事件统计。"""
    res = Result(scenario=sc)
    started = time.monotonic()
    root = logs_dir / sc.demo_id
    storage = root / "store"
    storage.mkdir(parents=True, exist_ok=True)
    skills_dir = EXAMPLES_DIR / sc.skills_subdir / "skills"

    prompter = AutoGrantPrompter()
    # 统一策略：默认放行，但对任意 skill 派发走 ask（→ 自动放行），
    # 让 HITL 路径在有派发的 demo 上真实触发并进事件流（可观测）。
    policy = PermissionPolicy.from_dict({"ask": ["Skill(*)"]}, prompter=prompter,
                                        prompter_timeout_seconds=60.0)
    compressors: list[object] = [SlidingWindowStrategy(keep_tail=2)] if sc.sliding else []
    budget = ContextBudget(context_window=sc.ctx_window or 128_000)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, storage_dir=storage,
        model_client=client, budget=budget, compressors=compressors,
        max_iterations=30,
        script_executors={"shell": ShellScriptExecutor(),
                          "python": PythonScriptExecutor()},
        permission_policy=policy,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id=sc.entry)
    # 三路采集：console（人读）+ JsonlSink（机读落盘）+ 内存计数
    attach_console_sink(engine, color=False)
    attach_jsonl_sink(engine, logs_dir / f"{sc.demo_id}.jsonl")

    sub = await engine.submit(taifeng.UserMessage(text=sc.prompt))
    try:
        # 判定方式：subscribe(sub) 的事件流在该 submission **处理完毕时自然结束**
        # （StopAsyncIteration）——这是"整个 submission 收尾"的可靠信号。注意：子 skill
        # 派发的子 turn 与父共用 submission_id，且 composite 流常只有子 turn emit
        # turn_completed（父 turn 不必再 emit），所以**不能**靠"等到顶层 turn_completed"
        # 判定（会误杀），而应消费完整条流后按"见到过 turn_completed 且无 turn_failed"判。
        # 240s 安全超时只为防极端卡死。
        async def _consume() -> None:
            async for ev in engine.subscribe(sub):
                res.kinds[ev.msg.kind] += 1
                if ev.msg.kind == "turn_failed" and not res.error:
                    res.error = str(ev.msg.data.get("error", ""))[:140]
        await asyncio.wait_for(_consume(), timeout=240.0)
        res.completed = res.kinds.get("turn_completed", 0) > 0
        res.failed = res.kinds.get("turn_failed", 0) > 0
    except TimeoutError:
        res.error = "timeout(240s)"
    finally:
        res.grants = prompter.grants
        res.duration_s = time.monotonic() - started
        await pool.close()
        await asyncio.sleep(0.05)
    return res


def _verdict(res: Result) -> tuple[str, str]:
    """判定单场景：成功条件 = turn_completed 且未 failed 且期望关键事件齐。"""
    seen = set(res.kinds)
    missing = res.scenario.expect - seen
    if res.failed:
        return "❌FAIL", f"turn_failed: {res.error}"
    if not res.completed:
        return "❌FAIL", res.error or "未 turn_completed"
    if missing:
        return "⚠️PART", f"完成但缺关键事件: {sorted(missing)}"
    return "✅PASS", ""


async def main() -> None:
    parser = argparse.ArgumentParser(description="真实 LLM 能力矩阵跑测")
    parser.add_argument("--only", help="只跑指定 scenario_id（台账增量合并，其余标 stale）")
    args = parser.parse_args()
    scenarios = SCENARIOS
    if args.only:
        scenarios = [sc for sc in SCENARIOS if sc.demo_id == args.only]
        if not scenarios:
            print(f"❌ 未知场景 {args.only!r}，可选: {[s.demo_id for s in SCENARIOS]}",
                  file=sys.stderr)
            sys.exit(2)
    try:
        client, meta = build_model_client(timeout_seconds=180.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")
    print(f"[setup] 共 {len(scenarios)} 个能力场景，真实 LLM 逐个跑\n")

    with tempfile.TemporaryDirectory() as td:
        logs_dir = Path(td) / "logs"
        logs_dir.mkdir(parents=True)
        results: list[Result] = []
        for i, sc in enumerate(scenarios, 1):
            print(f"\n{'━' * 70}\n[{i}/{len(scenarios)}] {sc.demo_id} —— {sc.capability}\n{'━' * 70}")
            try:
                results.append(await run_scenario(client, sc, logs_dir))
            except Exception as exc:  # noqa: BLE001  —— 单场景异常不拖垮整矩阵
                r = Result(scenario=sc, failed=True, error=f"{type(exc).__name__}: {exc}"[:120])
                results.append(r)
                print(f"  ⚠️ 场景异常: {r.error}")

        # ── 报告 A：能力成败矩阵 ──
        print(f"\n\n{'=' * 70}\nA. 能力成败矩阵\n{'=' * 70}")
        npass = 0
        for r in results:
            tag, note = _verdict(r)
            if tag.startswith("✅"):
                npass += 1
            disp = (f"disp={r.kinds.get('skill_dispatched', 0)} "
                    f"tool={r.kinds.get('tool_call_started', 0)} "
                    f"comp={r.kinds.get('compaction_started', 0)} "
                    f"grant={r.grants}")
            print(f"  {tag}  {r.scenario.demo_id:20s} {disp:42s} {note}")
        print(f"\n  小计：{npass}/{len(results)} 能力场景 PASS")

        # ── 报告 B：R3 可观测完整性审计 ──
        print(f"\n{'=' * 70}\nB. R3 可观测完整性审计\n{'=' * 70}")
        all_kinds: Counter = Counter()
        for r in results:
            all_kinds.update(r.kinds)
        # B1：发出的每个 kind 是否有专用渲染（不是 `?`/兜底）
        unmapped = sorted(k for k in all_kinds if k not in _KIND_TAG)
        print(f"  发出的事件 kind 种类: {len(all_kinds)} —— {sorted(all_kinds)}")
        if unmapped:
            print(f"  ⚠️ 无专用 console 渲染（落 `?` 兜底，信息不完整）: {unmapped}")
        else:
            print("  ✅ 所有发出的事件 kind 都有专用 console 渲染（无静默吞没）")
        # B2：R3 经典事件覆盖
        r3_missing = R3_CANONICAL - set(all_kinds)
        if r3_missing:
            print(f"  ℹ️ R3 经典事件本轮未触发（场景未覆盖到，非缺陷）: {sorted(r3_missing)}")
        else:
            print(f"  ✅ R3 经典事件全部在真实运行中被观测到: {sorted(R3_CANONICAL)}")
        print(f"\n  JSONL 机读日志已落: {logs_dir}/<demo>.jsonl（每行一个事件）")

        # ── 台账落盘（D1 双格式 + 增量合并）──
        from _ledger import git_short_commit
        run_commit = git_short_commit()
        from datetime import UTC, datetime
        now_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        ledger_records = []
        for r in results:
            tag, note = _verdict(r)
            verdict = {"✅PASS": "PASS", "⚠️PART": "PART", "❌FAIL": "FAIL"}[tag]
            ledger_records.append(ScenarioRecord(
                scenario_id=r.scenario.demo_id,
                capability=r.scenario.capability,
                verdict=verdict, note=note,
                expect=sorted(r.scenario.expect),
                missing=sorted(r.scenario.expect - set(r.kinds)),
                kinds=dict(r.kinds), grants=r.grants,
                duration_s=r.duration_s,
                commit=run_commit, timestamp_utc=now_utc,
            ))
        jp, mp = LedgerWriter().merge_and_write(
            provider=meta["provider"], model=meta["model"],
            records=ledger_records,
            r3=R3Audit(emitted_kinds=sorted(all_kinds), unmapped=unmapped,
                       canonical_missing=sorted(r3_missing)),
        )
        print(f"\n  台账已更新: {jp.name} + {mp.name}（docs/）")

        ok = npass == len(results) and not unmapped
        print(f"\n{'=' * 70}\n{'✅ 全能力场景真实 LLM 通过 + 日志完整' if ok else '⚠️ 见上方未通过/不完整项'}\n{'=' * 70}")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
