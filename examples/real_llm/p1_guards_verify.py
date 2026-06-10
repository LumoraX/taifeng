"""真实 LLM 验证两个 P1：compaction-surgical-trim + turn-resource-guards。

mock 已覆盖逻辑正确性；本脚本验真实 provider 维度的衔接：
  1. surgical-trim：真实多轮对话中按 ratio 触发剪枝（dedup/trim 计数非零），
     且**剪过的 history（占位符/截断文本）喂回真实 LLM 后 turn 正常继续**——
     这是 mock 永远验不了的（mock 不读 prompt）。
  2. denial 断路器：真实 LLM 发起真 tool call → 真实 permission deny 回流 →
     断路恰好一次 → turn 以 denial_circuit_open 终止（整链真实）。
  3. refund（best-effort）：真实流下 refunds_iteration 工具成功轮不耗预算；
     依赖模型遵循「多轮调用」指令，不遵循则如实记录为遵循度问题。

读 .env 的 LLM_BOOTSTRAP_*（见 examples/_provider_bootstrap.py）。

运行：
    PYTHONPATH=src uv run python examples/real_llm/p1_guards_verify.py
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
from taifeng import SurgicalTrimStrategy  # noqa: E402
from taifeng.context.budget import ContextBudget  # noqa: E402
from taifeng.loop.denial_breaker import DenialBreakerConfig  # noqa: E402
from taifeng.permission import PermissionPolicy  # noqa: E402
from taifeng.tool.spec import ToolResult, ToolSpec  # noqa: E402

# ── 场景 1：surgical-trim（重复大文档 + 小窗）──
_READER = """---
name: doc-reader
description: 文档阅读助手
version: 1.0.0
type: composite
entry: true
child_skills: [reader-note]
tool_names: [fetch_doc]
max_call_depth: 2
---
# 文档阅读助手
**每轮必须先调用工具 `fetch_doc`**（参数 `{}`）取当前文档，再用**一句话**总结。
不要凭记忆回答，必须每次都重新调用 fetch_doc。
"""

_NOTE = """---
name: reader-note
description: 占位
version: 1.0.0
type: atomic
---
# 占位
"""

# ── 场景 2/3：断路器 + refund ──
_GUARD = """---
name: guard-entry
description: 护栏验证入口
version: 1.0.0
type: composite
entry: true
child_skills: [specialist]
tool_names: [echo]
max_call_depth: 3
---
# 护栏验证入口
- 用户要求「会诊」时：**必须调用 `call_skill`**，参数
  `{"skill_id": "specialist", "args": {}}`。
- 用户要求「三连击」时：**连续调用工具 `echo` 三次**（每轮一次，参数 `{}`），
  三次之后再回答「完成」。
"""

_SPECIALIST = """---
name: specialist
description: 专科
version: 1.0.0
type: composite
tool_names: [echo]
max_call_depth: 2
---
# 专科
直接给一句结论。
"""

_BIG_DOC = ("《临床指南》第 1 节：糖代谢异常的分层管理要点。"
            + "空腹血糖受损与糖耐量异常的干预阈值、生活方式处方与随访周期细则。" * 120)


def _fetch_doc_tool() -> ToolSpec:
    async def _h(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok(_BIG_DOC)  # 每次同一大文档（dedup + trim 目标）

    return ToolSpec(name="fetch_doc", description="取当前文档全文",
                    input_schema={"type": "object", "properties": {}},
                    handler=_h, parallel_safe=True)


def _echo_tool(*, refunds: bool) -> ToolSpec:
    async def _h(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("ok")

    return ToolSpec(name="echo", description="回声（返回 ok）",
                    input_schema={"type": "object", "properties": {}},
                    handler=_h, parallel_safe=True,
                    refunds_iteration=refunds)


def _write(skills: Path, spec: dict[str, str]) -> None:
    for sub, body in spec.items():
        (skills / sub).mkdir(parents=True)
        (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")


async def _drive_turns(engine, texts: list[str], events: list) -> None:
    """逐条提交并等各自 root 终态（计数式等待，防上轮残留误判）。"""
    seen = 0
    for t in texts:
        await engine.submit(taifeng.UserMessage(text=t))
        for _ in range(1800):
            cur = sum(1 for m in events
                      if m.kind in ("turn_completed", "turn_failed"))
            if cur > seen:
                seen = cur
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)


async def _watch(engine, events: list):
    async def w():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break
    task = asyncio.create_task(w())
    await asyncio.sleep(0)
    return task


async def verify_surgical(client, root: Path) -> None:
    print("\n=== P1-1 surgical-trim（真实 LLM：剪枝触发 + 剪后历史可继续采样）===")
    skills = root / "s1"
    _write(skills, {"doc-reader": _READER, "reader-note": _NOTE})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t1", model_client=client,
        budget=ContextBudget(context_window=4000),
        compressors=[SurgicalTrimStrategy(min_dedup_chars=256,
                                          protect_tail_messages=2)],
        extra_tools=[_fetch_doc_tool()],
    )
    engine = await pool.get_or_create(session_id="p1s", entry_skill_id="doc-reader")
    events: list = []
    task = await _watch(engine, events)
    await _drive_turns(engine, [f"第{i + 1}轮：取文档并总结。" for i in range(3)],
                       events)

    comp_done = [m for m in events if m.kind == "compaction_completed"
                 and m.data.get("success")]
    details = [m.data.get("detail") for m in comp_done]
    finals = [m.kind for m in events
              if m.kind in ("turn_completed", "turn_failed")]
    trimmed = sum((d or {}).get("soft_trimmed", 0) + (d or {}).get("hard_cleared", 0)
                  + (d or {}).get("deduped", 0) for d in details)
    print(f"[1] 成功剪枝次数 = {len(comp_done)}  details = {details}")
    print(f"[2] turn 结局序列 = {finals}")
    ok = len(comp_done) > 0 and trimmed > 0 and "turn_failed" not in finals
    print(f"==> surgical 真实链路{'确证 ✅' if ok else '未确证 ❓'}"
          "（剪过的占位/截断历史喂回真实 LLM，后续 turn 正常完成）")
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def verify_breaker(client, root: Path) -> None:
    print("\n=== P1-2a denial 断路器（真实 LLM tool call → 真实 deny → 断路）===")
    skills = root / "s2"
    _write(skills, {"guard-entry": _GUARD, "specialist": _SPECIALIST})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t2", model_client=client,
        compressors=[], extra_tools=[_echo_tool(refunds=False)],
        permission_policy=PermissionPolicy.from_dict({"deny": ["Skill(*)"]}),
        denial_breaker_config=DenialBreakerConfig(max_consecutive_denials=1),
    )
    engine = await pool.get_or_create(session_id="p1b", entry_skill_id="guard-entry")
    events: list = []
    task = await _watch(engine, events)
    await _drive_turns(engine, ["请发起会诊。"], events)

    opened = [m for m in events if m.kind == "denial_circuit_open"]
    denied = [m for m in events if m.kind == "skill_dispatch_permission_denied"]
    done = [m for m in events if m.kind == "turn_completed"]
    er = done[0].data.get("end_reason") if done else "?"
    print(f"[1] 真实 LLM 发起 call_skill 且被 deny = {len(denied) > 0}")
    print(f"[2] denial_circuit_open 次数 = {len(opened)}  end_reason = {er}")
    if not denied:
        print("==> 真实 LLM 未发起 call_skill（遵循度），断路链未触发；机制由 mock 覆盖。")
    else:
        ok = len(opened) == 1 and er == "denial_circuit_open"
        print(f"==> 断路器真实链路{'确证 ✅' if ok else '未确证 ❌'}")
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def verify_refund(client, root: Path) -> None:
    print("\n=== P1-2b refund（真实 LLM 多轮 echo，best-effort 依赖遵循度）===")
    skills = root / "s3"
    _write(skills, {"guard-entry": _GUARD, "specialist": _SPECIALIST})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t3", model_client=client,
        compressors=[], extra_tools=[_echo_tool(refunds=True)],
        max_iterations=2,  # 3 次 echo + 收尾 > cap=2；refund 生效才能跑完
    )
    engine = await pool.get_or_create(session_id="p1r", entry_skill_id="guard-entry")
    events: list = []
    task = await _watch(engine, events)
    await _drive_turns(engine, ["请三连击。"], events)

    done = [m for m in events if m.kind == "turn_completed"]
    er = done[0].data.get("end_reason") if done else "?"
    iters = done[0].data.get("iterations") if done else "?"
    echo_calls = sum(1 for m in events if m.kind == "tool_call_completed")
    print(f"[1] echo 调用次数 = {echo_calls}  end_reason = {er}  净耗 = {iters}")
    if echo_calls >= 3 and er == "completed":
        print("==> refund 真实链路确证 ✅（cap=2 跑过 ≥3 轮 echo + 收尾）")
    elif echo_calls < 3:
        print("==> 真实 LLM 未按指令多轮调用（遵循度），refund 链未充分触发；"
              "机制由 mock 覆盖（cap=3 跑 5 轮净耗 1）。")
    else:
        print("==> 未确证 ❌")
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        await verify_surgical(client, root)
        await verify_breaker(client, root)
        await verify_refund(client, root)


if __name__ == "__main__":
    asyncio.run(main())
