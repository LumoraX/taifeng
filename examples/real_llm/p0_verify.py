"""真实 LLM 验证第三轮两个 P0：midturn-input-steering（+ overflow 自愈尝试）。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/p0_verify.py

读 .env 的 LLM_BOOTSTRAP_*（见 examples/_provider_bootstrap.py）。tests/ 用 mock，
真实 LLM 验证只在本目录（CLAUDE.md 约定）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.context.budget import ContextBudget  # noqa: E402
from taifeng.loop.submission import InjectUserInput, Shutdown  # noqa: E402

ATOMIC = """---
name: style-checker
description: 代码风格规则集
version: 1.0.0
type: atomic
---
# 风格规则集
- 函数 ≤ 80 行；圈复杂度 ≤ 10；命名 snake_case
- 禁止魔法值、禁止 silent fallback、禁止 SQL 字符串拼接
"""

COMPOSITE = """---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite
entry: true
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家
你是资深代码审查工程师。**工作流程**：
1. 收到 diff 时**首先调用 `read_skill("style-checker")`** 获取风格规则
2. 再按风格 / 安全 / 性能逐项分析，给出按严重性排序的建议
"""


async def _consume(engine: object, events: list) -> asyncio.Task:
    async def run() -> None:
        async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    return task


async def _wait(
    events: list, kinds: tuple[str, ...], limit_s: float = 90.0
) -> str | None:
    steps = int(limit_s / 0.1)
    for _ in range(steps):
        for m in events:
            if m.kind in kinds:
                return m.kind
        await asyncio.sleep(0.1)
    return None


async def verify_steering(pool: object) -> None:
    print("\n=== P0-2 midturn-input-steering（真实 LLM 多轮 turn 中途注入）===")
    engine = await pool.get_or_create(  # type: ignore[attr-defined]
        session_id="p0-steer", entry_skill_id="code-reviewer"
    )
    events: list = []
    task = await _consume(engine, events)

    sub_id = await engine.submit(
        taifeng.UserMessage(
            text=(
                "请审查这段 Python diff：user_service.py:45 新增 "
                "`def get_user(id): cur.execute(f'SELECT * FROM u WHERE id={id}')`，"
                "函数从 60 行增到 130 行。按你的工作流程分析。"
            )
        )
    )
    # 给 turn task 注册 _pending + 进入首轮采样的窗口（真实采样秒级、窗口充足；
    # 不等 tool_call_started，因部分 model 可能单轮直接作答不调 read_skill）。
    await asyncio.sleep(0.8)
    started = any(m.kind == "turn_started" for m in events)
    early_done = any(m.kind in ("turn_completed", "turn_failed") for m in events)
    print(f"[1] 注入前快照：turn_started={started} 已提前完成={early_done}")
    # 运行中注入增量要求
    await engine.submit(
        InjectUserInput(
            submission_id=sub_id,
            text=(
                "【运行中补充】请额外重点评估**并发安全**与**输入校验**，"
                "并在结论里单列一节《补充评估》。"
            ),
        )
    )
    await _wait(events, ("turn_completed", "turn_failed"), limit_s=120.0)
    await asyncio.sleep(0.3)

    injected = [m for m in events if m.kind == "user_input_injected"]
    delivered = bool(injected and injected[0].data["delivered"])
    hist = engine.history_snapshot()  # type: ignore[attr-defined]
    in_history = any("运行中补充" in str(it.payload) for it in hist)
    final = "".join(
        str(it.payload.get("text", "")) for it in hist if it.kind == "message"
    )
    reflected = ("并发" in final) or ("校验" in final) or ("补充评估" in final)

    print(f"[2] user_input_injected delivered = {delivered}")
    print(f"[3] 注入文本进入 history       = {in_history}")
    print(f"[4] LLM 最终回复体现注入内容   = {reflected}（并发/校验/补充评估）")
    print(f"==> R5 不丢注入 = {in_history}；delivered={delivered}"
          f"（单轮快 turn 时 delivered 可能为 false → 退化落历史，仍不丢）")
    print(f"==> steering 能力{'确证 ✅' if in_history else '未确证 ❌'}"
          f"（reflected 仅供参考，取决于模型遵循度）")
    # 关掉这个 engine 的事件流
    await engine.submit(Shutdown())
    await asyncio.wait_for(task, timeout=5.0)


async def verify_overflow(
    skills_dir: Path, client: object, threads_dir: Path
) -> None:
    print("\n=== P0-1 reactive-compaction-recovery（真实 provider context overflow 自愈）===")
    # budget 设高于真实 128k → 本地估算（中文按 char/4 偏低）不预压；
    # 真实 provider 判超长才报 overflow → 正是 A1 「本地乐观、provider 已判超长」场景。
    budget = ContextBudget(context_window=200_000)
    pool = await taifeng.EnginePool.create(  # type: ignore[attr-defined]
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, budget=budget,
    )
    engine = await pool.get_or_create(
        session_id="p0-overflow", entry_skill_id="code-reviewer"
    )
    events: list = []
    task = await _consume(engine, events)
    # ~ 超 128k token 的超长上下文（中文段落重复）；本地估算偏低、真实 provider 判超长
    filler = "需要审查的历史代码与上下文段落。" * 30000
    await engine.submit(
        taifeng.UserMessage(text=f"请总结以下超长上下文的主要风险：\n{filler}")
    )
    await _wait(events, ("turn_completed", "turn_failed"), limit_s=180.0)
    await asyncio.sleep(0.3)

    retried = sum(1 for m in events if m.kind == "provider_retry")
    ov_comp = sum(
        1 for m in events
        if m.kind == "compaction_started" and m.data.get("phase") == "overflow"
    )
    final = next(
        (m for m in events if m.kind in ("turn_completed", "turn_failed")), None
    )
    print(f"[1] provider_retry 自愈触发次数 = {retried}")
    print(f"[2] compaction phase=overflow   = {ov_comp}")
    fk = final.kind if final else "无"
    extra = (
        f" failure_class={final.data.get('failure_class')}"
        if final and final.kind == "turn_failed" else ""
    )
    print(f"[3] turn 结局 = {fk}{extra}")
    if retried > 0:
        print("==> overflow 自愈确证 ✅"
              "（provider 判超长 → 强制压缩 + 重采样，未直接硬失败丢 turn）")
    else:
        print("==> 自愈未触发：provider 未返回 context-overflow 类错误"
              "（或 body 关键字不匹配 / 本地先压）。如实记录，逻辑已由 mock 覆盖。")
    await engine.submit(Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def verify_local_compaction(
    skills_dir: Path, client: object, threads_dir: Path
) -> None:
    print("\n=== 机制1：本地 budget 到达上限即主动压缩（不依赖 provider overflow）===")
    # 配小 context_window：本地估算到达 soft_limit 时 taifeng 主动 handoff 压缩。
    # 这才是「到达配置上限就处理」的常态路径；A1(provider overflow) 只是兜底。
    budget = ContextBudget(
        context_window=5000, soft_limit_ratio=0.6, preserve_tail_messages=2
    )
    pool = await taifeng.EnginePool.create(  # type: ignore[attr-defined]
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, budget=budget,
    )
    engine = await pool.get_or_create(
        session_id="p0-localcomp", entry_skill_id="code-reviewer"
    )
    events: list = []
    task = await _consume(engine, events)
    # 连续多轮累积 history，越过 soft_limit(=3000 token) 触发本地主动压缩
    seg = "需要审查的代码上下文段落，含函数实现与注释细节。" * 300
    seen = 0
    for i in range(3):
        await engine.submit(
            taifeng.UserMessage(text=f"第{i + 1}条审查材料：\n{seg}")
        )
        # 等「本轮」turn 完成（turn_completed 计数增加；不能用 _wait 检查存在性，
        # 否则上一轮残留的 turn_completed 会让本轮误判为立即完成）。
        for _ in range(1800):
            cur = sum(
                1 for m in events
                if m.kind in ("turn_completed", "turn_failed")
            )
            if cur > seen:
                seen = cur
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)

    comp = [m for m in events if m.kind == "compaction_started"]
    done = [m for m in events if m.kind == "compaction_completed"]
    phases = [m.data.get("phase") for m in comp]
    success = [m.data.get("success") for m in done]
    print(f"[1] compaction_started 次数 = {len(comp)}  phases={phases}")
    print(f"[2] compaction_completed success = {success}")
    for m in done:
        print(f"    完成明细: success={m.data.get('success')} "
              f"reason={m.data.get('reason')} removed={m.data.get('removed_count')}")
    rolled = [m for m in events if m.kind == "compaction_integrity_rolled_back"]
    skipped = [m for m in events if m.kind == "pre_compact_hook_skipped"]
    finals = [m.kind for m in events if m.kind in ("turn_completed", "turn_failed")]
    print(f"    integrity_rolled_back={len(rolled)} hook_skipped={len(skipped)}")
    print(f"    turn 结局序列={finals}")
    kinds_count: dict = {}
    for m in events:
        kinds_count[m.kind] = kinds_count.get(m.kind, 0) + 1
    print(f"    全事件统计: {kinds_count}")
    ok = len(comp) > 0 and any(success)
    print(f"==> 本地到达上限主动压缩{'确证 ✅' if ok else '未触发 ❓'}"
          f"（真实 handoff LLM 摘要，不依赖 provider 报错）")
    await engine.submit(Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}"
          f" key={meta.get('api_key_tail', '-')}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        (skills / "style-checker").mkdir(parents=True)
        (skills / "style-checker" / "SKILL.md").write_text(ATOMIC, encoding="utf-8")
        (skills / "code-reviewer").mkdir(parents=True)
        (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE, encoding="utf-8")
        pool = await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=root / "threads", model_client=client
        )
        await verify_steering(pool)
        await pool.close()

        await verify_local_compaction(skills, client, root / "threads-lc")
        await verify_overflow(skills, client, root / "threads-of")


if __name__ == "__main__":
    asyncio.run(main())
