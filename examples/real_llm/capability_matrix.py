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
import os
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# examples/ 进 sys.path，import 共享 bootstrap
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _drivers import DRIVERS  # noqa: E402
from _ledger import LedgerWriter, R3Audit, ScenarioRecord  # noqa: E402
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
    resolve_bootstrap_env,
)
from _recorder import RecordingClient  # noqa: E402
from test_openai_image_matrix import (  # noqa: E402
    ImageMatrixResult,
    run_openai_image_matrix,
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
from taifeng.tool.builtins import (  # noqa: E402
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
    make_request_user_input_tool,
    make_send_message_tool,
    make_spawn_skill_tool,
)

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
    driver: str | None = None      # 多步编排剧本（_drivers.DRIVERS 键；None=单 prompt）
    forbid: set[str] = field(default_factory=set)  # 出现即 FAIL 的事件（防假 PASS）
    tools: tuple[str, ...] = ()    # 需注册的 extra_tools 工厂名
    pool_kwargs: dict[str, Any] = field(default_factory=dict)  # EnginePool.create 追加参数


# Scenario.tools → extra_tools 工厂映射
TOOL_FACTORIES = {
    "request_user_input": make_request_user_input_tool,
    "spawn_skill": make_spawn_skill_tool,
    "await_skills": make_await_skills_tool,
    "join_skill": make_join_skill_tool,
    "kill_skill": make_kill_skill_tool,
    "send_message": make_send_message_tool,
}

def _post_turn_hook_runner() -> object:
    """构造一个注册了 post_turn 钩子的 HookRunner（供 post_turn_review 场景注入）。

    钩子做最小固化(校验本轮 history 已回写后返回 ok)——真实运行中触发即 emit
    post_turn_hook_fired,矩阵据此事件判定 post_turn 在真实 LLM 链路被触发。
    """
    from taifeng.hooks import HookDecision, HookRegistry, HookRunner

    reg = HookRegistry()

    async def _consolidate(hook: Any, ctx: Any) -> object:
        # 真实场景的"记忆固化"落脚点;此处仅审计型 no-op(事件触发即验证)
        return HookDecision.ok()

    reg.register("post_turn", _consolidate)
    return HookRunner(reg)


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
             "请就『家用储能电池的主流技术路线』做调研：请**同时**从网络、学术、新闻三个相互独立的信息源各自取证（在同一条消息里 fan-out 三个 call_skill 并发执行），最后综合成结论。",  # 原主题连续三次触发网关 content filter，中性化改写
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
             "请对这份方案同时安排两项评审：产品需求评估与战略定位分析。方案：面向开发者的智能体引擎工具包，提供事件流观测与上下文管理。",  # 原措辞触发网关 content filter（连续两次复现），中性化改写
             capability="差异化授权 + 多路派发",
             expect={"skill_dispatched", "turn_completed"}),
    Scenario("travel_planner", "travel_planner", "trip-planner",
             "帮我规划 9 月 12-15 日从北京到巴黎的 3 天旅行。",
             capability="三路 fan-out（航班/酒店/活动）+ 综合",
             expect={"skill_dispatched", "turn_completed"}),
    # ── P0 高发链路（driver 多步编排）──
    Scenario("suspend_resume", "real_llm/skills_extra/suspend_resume", "intake-assistant",
             "",  # driver 自行提交
             capability="HITL 挂起 → Resume 续跑（R5）",
             expect={"turn_suspended", "suspension_resolved", "turn_completed"},
             driver="suspend_resume", tools=("request_user_input",)),
    Scenario("turn_rewind", "turn_rewind", "orchestrator",
             "",
             capability="turn 回访重跑（Rewind re_reason）",
             expect={"rewind_checkpoint_recorded", "turn_completed"},
             forbid={"rewind_rejected"},
             driver="turn_rewind"),
    Scenario("thread_rewind", "turn_rewind", "orchestrator",
             "",
             capability="thread 寻址 rewind（spawn 子 thread 截断重推）",
             expect={"spawn_started", "spawn_completed", "turn_rewound",
                     "turn_completed"},
             forbid={"rewind_rejected", "spawn_failed"},
             driver="thread_rewind"),
    Scenario("spawn_join", "multi_expert_consult", "orchestrator",
             "",
             capability="分离式并发 spawn + 错峰 HITL + join-barrier 聚合",
             expect={"spawn_started", "spawn_suspended", "spawn_completed",
                     "join_barrier_fired"},
             driver="spawn_join",
             tools=("spawn_skill", "await_skills", "join_skill", "kill_skill",
                    "request_user_input")),
    Scenario("peer_messaging", "real_llm/skills_extra/peer_messaging",
             "research-coordinator",
             "",
             capability="谱系 peer 消息投递（spawn + send_message）",
             expect={"spawn_started", "peer_message_sent", "spawn_completed",
                     "turn_completed"},
             driver="peer_messaging",
             tools=("spawn_skill", "send_message", "await_skills")),
    Scenario("kernel_knobs", "real_llm/skills_extra/kernel_knobs", "budget-analyst",
             "请按口径分析：某部门年度预算 1200 万元，Q3 实际支出 410 万元，是否超支？",
             capability="K2 会话 token 天花板真实触发（resource_limit）",
             expect={"resource_limit_exceeded"},
             pool_kwargs={"max_session_tokens": 200}),
    Scenario("post_turn_review", "compression_showcase", "chatty-assistant",
             "",
             capability="post_turn 钩子（turn 收尾审计/记忆固化 + 跨 turn 顺序）",
             expect={"post_turn_hook_fired", "turn_completed"},
             driver="post_turn_review",
             pool_kwargs={"hooks": _post_turn_hook_runner()}),
    # budget-awareness：小窗(ctx_window=3000 → soft=2550/hard=2850)+ 约 9300 字符长
    # prompt，使 pre-turn 估算用量(len/3.5≈2659)落在 soft 与 hard 之间 → 穿越 soft 注
    # 中性预算事实。期望真实链路 emit budget_hint_injected。
    Scenario("budget_awareness", "compression_showcase", "chatty-assistant",
             "背景资料汇总，请通读后给出要点分析。"
             + "这是一段需要纳入上下文的背景资料文本。" * 489,
             capability="预算自知提示（穿越 soft_limit 注中性预算事实，ADR 0020）",
             expect={"budget_hint_injected", "turn_completed"},
             ctx_window=3000),
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
    events: list = field(default_factory=list)  # driver 模式的全量 EventMsg（轮询用）


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
        extra_tools=[TOOL_FACTORIES[n]() for n in sc.tools],
        **sc.pool_kwargs,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id=sc.entry)
    # 三路采集：console（人读）+ JsonlSink（机读落盘）+ 内存计数
    attach_console_sink(engine, color=False)
    attach_jsonl_sink(engine, logs_dir / f"{sc.demo_id}.jsonl")

    if sc.driver is not None:
        # driver 模式：subscribe_all 全量采集（spawn 子轨事件不挂在父 submission 上），
        # 编排剧本轮询 res.events 推进；超时整体兜底
        async def _collect_all() -> None:
            async for ev in engine.subscribe_all():
                res.kinds[ev.msg.kind] += 1
                res.events.append(ev.msg)
                if ev.msg.kind == "turn_failed" and not res.error:
                    res.error = str(ev.msg.data.get("error", ""))[:140]

        collector = asyncio.create_task(_collect_all())
        try:
            await asyncio.wait_for(DRIVERS[sc.driver](engine, res), timeout=420.0)
            await asyncio.sleep(0.2)  # 事件总线 flush
            res.completed = res.kinds.get("turn_completed", 0) > 0
            res.failed = res.kinds.get("turn_failed", 0) > 0
        except TimeoutError as exc:
            res.error = res.error or f"driver 超时/等待失败: {exc}"
        finally:
            collector.cancel()
            res.grants = prompter.grants
            res.duration_s = time.monotonic() - started
            await pool.close()
            await asyncio.sleep(0.05)
        return res

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
    forbidden = res.scenario.forbid & seen
    if res.failed:
        return "❌FAIL", f"turn_failed: {res.error}"
    if forbidden:
        return "❌FAIL", f"出现禁止事件: {sorted(forbidden)}"
    if not res.completed:
        return "❌FAIL", res.error or "未 turn_completed"
    if missing:
        return "⚠️PART", f"完成但缺关键事件: {sorted(missing)}"
    return "✅PASS", ""


async def main() -> None:
    parser = argparse.ArgumentParser(description="真实 LLM 能力矩阵跑测")
    parser.add_argument("--only", help="只跑指定 scenario_id（台账增量合并，其余标 stale）")
    parser.add_argument("--provider", help="覆盖 LLM_BOOTSTRAP_PROVIDER")
    parser.add_argument("--model", help="覆盖 LLM_BOOTSTRAP_MODEL")
    parser.add_argument(
        "--record", action="store_true",
        help="金样录制模式（需真实 key）：把 PASS 场景的事件流形状签名写入 "
             "tests/llm/golden/<scenario>.jsonl（FAIL/PART 场景不动旧金样）；"
             "与 --only 组合时只更新该场景金样",
    )
    args = parser.parse_args()
    if args.provider:
        os.environ["LLM_BOOTSTRAP_PROVIDER"] = args.provider
    if args.model:
        os.environ["LLM_BOOTSTRAP_MODEL"] = args.model
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
    recorder: RecordingClient | None = None
    if args.record:
        # 金样录制：包装 client（ModelClient 协议同构），PASS 场景跑完统一 flush
        recorder = RecordingClient(client)
        client = recorder
    print(f"[setup] provider={meta['provider']} model={meta['model']}"
          + (" [金样录制中]" if recorder else ""))
    print(f"[setup] 共 {len(scenarios)} 个能力场景，真实 LLM 逐个跑\n")

    with tempfile.TemporaryDirectory() as td:
        logs_dir = Path(td) / "logs"
        logs_dir.mkdir(parents=True)
        results: list[Result] = []
        for i, sc in enumerate(scenarios, 1):
            print(f"\n{'━' * 70}\n[{i}/{len(scenarios)}] {sc.demo_id} —— {sc.capability}\n{'━' * 70}")
            if recorder is not None:
                recorder.begin_scenario(sc.demo_id)
            try:
                results.append(await run_scenario(client, sc, logs_dir))
            except Exception as exc:  # noqa: BLE001  —— 单场景异常不拖垮整矩阵
                r = Result(scenario=sc, failed=True, error=f"{type(exc).__name__}: {exc}"[:120])
                results.append(r)
                print(f"  ⚠️ 场景异常: {r.error}")

        image_results: list[ImageMatrixResult] = []
        if meta["provider"] == "openai" and args.only is None:
            provider, _, api_key, resolved_model, base_url = resolve_bootstrap_env()
            assert provider == "openai" and api_key is not None
            print(f"\n{'━' * 70}\nOpenAI 图片双协议矩阵\n{'━' * 70}")
            image_results = await run_openai_image_matrix(
                api_key=api_key,
                model=resolved_model,
                base_url=base_url or "https://api.openai.com/v1",
                logs_dir=logs_dir / "openai-image",
            )
            for result in image_results:
                icon = "✅" if result.verdict == "PASS" else "❌"
                print(
                    f"  {icon}{result.verdict:4s}  {result.scenario_id:42s} "
                    f"{result.note}"
                )

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
        image_pass = sum(result.verdict == "PASS" for result in image_results)
        if image_results:
            print(f"  图片：{image_pass}/{len(image_results)} 双协议场景 PASS")

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
        for result in image_results:
            ledger_records.append(ScenarioRecord(
                scenario_id=result.scenario_id,
                capability=result.capability,
                verdict=result.verdict,
                note=result.note,
                expect=sorted(result.kinds) if result.verdict == "PASS" else [],
                missing=[],
                kinds=dict(result.kinds),
                grants=0,
                duration_s=result.duration_s,
                commit=run_commit,
                timestamp_utc=now_utc,
            ))
        jp, mp = LedgerWriter().merge_and_write(
            provider=meta["provider"], model=meta["model"],
            records=ledger_records,
            r3=R3Audit(emitted_kinds=sorted(all_kinds), unmapped=unmapped,
                       canonical_missing=sorted(r3_missing)),
            full_run=args.only is None,
        )
        print(f"\n  台账已更新: {jp.name} + {mp.name}（docs/）")

        # ── 金样落盘（--record：只固化 PASS 场景，FAIL/PART 不动旧金样）──
        if recorder is not None:
            flushed = []
            for r in results:
                tag, _ = _verdict(r)
                if not tag.startswith("✅"):
                    continue
                gp = recorder.flush_golden(
                    r.scenario.demo_id,
                    provider=meta["provider"], model=meta["model"],
                    commit=run_commit, recorded_at=now_utc,
                )
                if gp is not None:
                    flushed.append(gp.name)
            print(f"  金样已落盘: {len(flushed)} 个场景 → tests/llm/golden/ "
                  f"(截断签名滤除 {recorder.truncated_skipped} 条)")

        ok = (
            npass == len(results)
            and image_pass == len(image_results)
            and not unmapped
        )
        print(f"\n{'=' * 70}\n{'✅ 全能力场景真实 LLM 通过 + 日志完整' if ok else '⚠️ 见上方未通过/不完整项'}\n{'=' * 70}")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
