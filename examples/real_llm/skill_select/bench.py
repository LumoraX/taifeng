"""真实 LLM「技能选择准确率」基准 —— 在 200+ 候选里选对没？

这是回答「成百上千 skill 时，agent 能不能给每个任务**正确选到**对的那一个」的真实测量。
**只能用真实 LLM 测**（SimClient 的回答是写死的，不会"看一堆候选挑对的那个"）。

做法：
  1. router entry 把全部 N(=210) 个叶子挂为 child_skills → 一次 turn 的 prompt 里列出全部
     N 条 `id: 能力描述`，模型必须从中选**唯一最匹配**的一个并 call_skill。
  2. 每条 task 是一句**改写过的自然请求**（不照搬技能描述措辞 → 真考语义匹配），且有已知的
     唯一正确目标 skill（tasks.json 的 expected）。
  3. 抽样 M 条 task，真实跑：
     - 权限 deny `Skill(*)` + `max_iterations=1` → 路由器采样一次发出 call_skill 即停，
       **叶子不执行**，每任务恰好 1 次 LLM 调用。
     - 从 `tool_call_started`(name=call_skill) 捕获模型**选了哪个** skill_id。
  4. 对比 expected → 输出总准确率、按领域准确率、错选明细。

运行（需真实 LLM key，复用 examples/_provider_bootstrap 的 LLM_BOOTSTRAP_* env）：
    PYTHONPATH=src python examples/real_llm/skill_select/bench.py --sample 40
（若 skills/ 不存在会自动先 build。）
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
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.context.budget import ContextBudget  # noqa: E402
from taifeng.permission.policy import PermissionPolicy  # noqa: E402

SKILLS_DIR = HERE / "skills"
TASKS_PATH = HERE / "tasks.json"


def _domain_of(skill_id: str) -> str:
    """据 id 前缀归类领域（data-csv-parse → data）。"""
    return skill_id.split("-", 1)[0]


def _ensure_built() -> None:
    """skills/ 或 tasks.json 缺失则先跑 build_skills.build()。"""
    if SKILLS_DIR.exists() and TASKS_PATH.exists():
        return
    from build_skills import build  # 同目录脚本

    sys.path.insert(0, str(HERE))
    n = build()
    print(f"[build] 生成 {n} 个叶子 + router entry")


async def _select_one(pool: object, idx: int, task: str, timeout: float) -> str | None:
    """提交一条 task 给 router，捕获模型选中的 skill_id（call_skill 的 skill_id）。

    返回 None 表示模型没发出任何 call_skill（拒答 / 只输出文本）。
    """
    engine = await pool.get_or_create(  # type: ignore[attr-defined]
        session_id=f"sel-{idx}", entry_skill_id="router"
    )
    selected: str | None = None
    done = asyncio.Event()

    async def watch() -> None:
        nonlocal selected
        async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
            kind = ev.msg.kind
            if kind == "tool_call_started" and ev.msg.data.get("name") == "call_skill":
                try:
                    selected = json.loads(
                        ev.msg.data.get("arguments", "{}")
                    ).get("skill_id")
                except json.JSONDecodeError:
                    selected = None
                done.set()
                return
            if kind in ("turn_completed", "turn_failed"):
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
    return selected


async def main() -> None:
    """跑选择基准并打印准确率报告。"""
    ap = argparse.ArgumentParser(description="真实 LLM 技能选择准确率基准")
    ap.add_argument("--sample", type=int, default=40, help="抽样任务数（默认 40）")
    ap.add_argument("--seed", type=int, default=0, help="抽样随机种子")
    ap.add_argument("--timeout", type=float, default=90.0, help="单任务超时秒")
    args = ap.parse_args()

    _ensure_built()
    tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    total_pool = len(tasks)

    rng = random.Random(args.seed)
    sample = tasks if args.sample >= total_pool else rng.sample(tasks, args.sample)

    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        raise SystemExit(f"❌ 无法构造真实 LLM client：{exc}") from None

    print("=" * 72)
    print("技能选择准确率基准（真实 LLM）")
    print("=" * 72)
    print(f"候选规模: {total_pool} 个 skill（router 一次性暴露全部 child）")
    print(f"抽样任务: {len(sample)} 条 | provider={meta['provider']} model={meta['model']}")
    print("每任务 1 次 LLM 调用（deny Skill(*) + max_iterations=1，叶子不执行）\n")

    # 权限 deny Skill(*)：路由器发出的 call_skill 被拦，叶子不执行（省一半调用）
    policy = PermissionPolicy.from_dict({"default_mode": "allow", "deny": ["Skill(*)"]})

    results: list[tuple[str, str, str | None]] = []  # (task, expected, got)
    with tempfile.TemporaryDirectory() as td:
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=Path(td) / "threads",
            model_client=client,
            budget=ContextBudget(context_window=128_000),
            compressors=[],
            permission_policy=policy,
            max_iterations=1,  # 采样一次发 call_skill 即停
        )
        try:
            started = time.monotonic()
            for i, t in enumerate(sample):
                got = await _select_one(pool, i, t["task"], args.timeout)
                ok = got == t["expected"]
                results.append((t["task"], t["expected"], got))
                mark = "✓" if ok else "✗"
                print(f"  [{i + 1:>3}/{len(sample)}] {mark} 期望={t['expected']:<26} 实选={got}")
            elapsed = time.monotonic() - started
        finally:
            await pool.close()

    # ── 汇总 ──
    correct = sum(1 for _, exp, got in results if exp == got)
    none_cnt = sum(1 for _, _, got in results if got is None)
    acc = correct / len(results) if results else 0.0

    dom_total: dict[str, int] = defaultdict(int)
    dom_ok: dict[str, int] = defaultdict(int)
    for _, exp, got in results:
        d = _domain_of(exp)
        dom_total[d] += 1
        if exp == got:
            dom_ok[d] += 1

    print("\n" + "=" * 72)
    print(f"总准确率: {correct}/{len(results)} = {acc:.1%}"
          f"  （未选出: {none_cnt}）  耗时 {elapsed:.1f}s")
    print("\n按领域准确率")
    print(f"  {'domain':<12}{'acc':>10}")
    print("  " + "-" * 22)
    for d in sorted(dom_total):
        a = dom_ok[d] / dom_total[d]
        print(f"  {d:<12}{dom_ok[d]}/{dom_total[d]} = {a:>5.0%}")

    errs = [(exp, got, task) for task, exp, got in results if exp != got]
    if errs:
        print(f"\n错选明细（{len(errs)} 条）—— 期望 vs 实选 / 任务")
        for exp, got, task in errs:
            print(f"  ✗ {exp}  →  {got}")
            print(f"      「{task[:54]}」")

    print("\n🎯 选择基准完毕")


if __name__ == "__main__":
    asyncio.run(main())
