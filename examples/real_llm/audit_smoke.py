"""真实 LLM 冒烟：审计模式（SessionJournal）的挂起 / 接管 / 分离派发 / Timeline / 录后重放。

能力矩阵（capability_matrix.py）跑的是非审计路径。本脚本用真实 provider 走一遍审计模式里
最依赖 provider 行为的几段，确认真实端点接受 Journal 重建出的历史：

    A. 工具调用处停下等审批 → 释放 Session（session_detached）→ 新 pool 接管 → Resume 批准
       → provider 接受重建的历史并续跑到完成；
    B. 分离式派发：模型 spawn 一个 worker、等它结束、汇报结果；子 thread 的 LLM 调用记在子 thread；
    C. Timeline 三种视图读取整个 Journal；脱敏视图里不含用户原文；
    D. 录后重放：用录下的 Journal（JournalReplayClient + replay_tools）不触网重放整个 Session。

每一段都断言 Journal strict verify 健康、没有未结算 effect。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/audit_smoke.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.conversation.journal import JournalHealth  # noqa: E402
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore  # noqa: E402
from taifeng.experimental import (  # noqa: E402
    AuditConfig,
    JournalReplayClient,
    JournalTimelineProjector,
    recorded_submissions,
    recorded_tool_calls,
    replay_session,
    replay_tools,
)
from taifeng.llm.audit import AttemptObservableClientAdapter  # noqa: E402
from taifeng.loop.audit_resume_scan import find_unsettled_effects  # noqa: E402
from taifeng.loop.submission import Resume  # noqa: E402
from taifeng.permission import (  # noqa: E402
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    SuspendingPrompter,
)
from taifeng.tool.builtins import (  # noqa: E402
    make_join_skill_tool,
    make_spawn_skill_tool,
    make_wait_peer_tool,
)
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec  # noqa: E402

_SECRET = "核对码 ORBIT-73KD"

_ENTRY = """---
name: entry
description: 审计冒烟入口
version: 1.0.0
type: composite
entry: true
child_skills: [worker]
tool_names: [write_note, spawn_skill, wait_peer, join_skill]
max_call_depth: 2
---
# 审计冒烟入口

按用户的指示办事，回答尽量简短。

- 用户让你「记一笔」时：调用一次工具 `write_note`，参数 `{"text": <要记的内容>}`，然后用一句话确认。
- 用户让你「派人算」时：调用 `spawn_skill`（`skill_id` 为 `worker`，`args` 为 `{"a": 17, "b": 25}`，
  `reason` 写一句理由），拿到 `handle_id` 后调用 `wait_peer`（`handle_id` 与 `timeout_seconds: 10`），
  如果超时就再调用一次 `wait_peer`，最后把 worker 的结果原样告诉用户。
"""

_WORKER = """---
name: worker
description: 做加法的工人
version: 1.0.0
type: atomic
---
# 工人

输入是 JSON，含 `a` 与 `b`。只回复一行：`SUM=<a+b 的结果>`，不要多说。
"""


def _skills(root: Path) -> Path:
    for name, body in (("entry", _ENTRY), ("worker", _WORKER)):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _tools(written: list[str]) -> list[ToolSpec]:
    async def write_note(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        decision = await ctx.extras["permission_policy"].check(
            PermissionRequest.for_tool_call(
                "write_note", args, thread_id=ctx.thread_id,
                submission_id=str(ctx.extras.get("submission_id") or ""),
                entry_skill_id="entry", turn_index=int(ctx.extras.get("turn_index") or 0),
                call_chain=("entry",), extra_metadata={"call_id": ctx.call_id},
                reason="写入笔记需要审批",
            )
        )
        if not decision.granted:
            return ToolResult.error(f"permission_denied: {decision.reason}", reason="permission_denied")
        written.append(str(args.get("text")))
        return ToolResult.ok("已记下")

    note = ToolSpec(
        name="write_note", description="记一笔笔记（需要审批）",
        input_schema={
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        },
        handler=write_note, effect_kind="external_non_idempotent", reconciliation="manual",
    )
    return [note, make_spawn_skill_tool(), make_wait_peer_tool(), make_join_skill_tool()]


async def _drive(engine: Any, op: Any, *, timeout: float = 180.0) -> str:
    """提交并等 root turn 停下；返回终态事件的 kind。"""
    holder: list[str] = []
    last: list[str] = []
    done = asyncio.Event()

    async def collect() -> None:
        async for ev in engine.subscribe_all():
            if not holder or ev.submission_id != holder[0]:
                continue
            kind = ev.msg.kind
            if kind == "turn_suspended" or (
                kind in ("turn_completed", "turn_failed") and ev.msg.data.get("is_root")
            ):
                last.append(f"{kind}:{ev.msg.data.get('error') or ''}" if kind == "turn_failed" else kind)
                done.set()
                return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    holder.append(await engine.submit(op))
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return last[0]


class _Smoke:
    """一次冒烟的共享状态。"""

    def __init__(self, root: Path, client: Any, meta: dict[str, Any]) -> None:
        self.root = root
        self.client = client
        self.provider = str(meta["provider"])
        self.model = str(meta["model"])
        self.written: list[str] = []
        self.session_id = "ses-audit-smoke"
        self.failures: list[str] = []

    def check(self, condition: bool, label: str) -> None:
        print(f"  {'✅' if condition else '❌'} {label}")
        if not condition:
            self.failures.append(label)

    async def pool(self, *, model_client: Any, journal: str, threads: str, tools: list[ToolSpec]) -> Any:
        return await taifeng.EnginePool.create(
            skills_dir=_skills(self.root / "skills"),
            threads_dir=self.root / threads,
            model_client=model_client,
            compressors=[],
            extra_tools=tools,
            permission_policy=PermissionPolicy(
                rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
                default_mode="ask", prompter=SuspendingPrompter(),
            ),
            audit=AuditConfig(
                journal_core=JsonlSessionJournalCore(self.root / journal),
                writer_id="smoke", max_attachment_bytes=65536, max_total_attachment_bytes=1048576,
            ),
        )

    def real_client(self) -> Any:
        return AttemptObservableClientAdapter(
            self.client, provider=self.provider, default_model=self.model,
        )

    async def journal(self, name: str = "journal") -> list[Any]:
        core = JsonlSessionJournalCore(self.root / name)
        return [e async for e in core.load(self.session_id)]

    async def healthy(self, label: str, name: str = "journal") -> None:
        core = JsonlSessionJournalCore(self.root / name)
        verification = await core.verify(self.session_id)
        envelopes = await self.journal(name)
        self.check(verification.health is JournalHealth.HEALTHY, f"{label}: Journal strict verify 健康")
        self.check(find_unsettled_effects(envelopes) == (), f"{label}: 没有未结算 effect")


async def _part_a(smoke: _Smoke) -> str:
    """挂起 → 释放 → 接管 → Resume。"""
    print("\n[A] 审批挂起 → 释放 → 接管 → Resume")
    pool = await smoke.pool(
        model_client=smoke.real_client(), journal="journal", threads="threads",
        tools=_tools(smoke.written),
    )
    engine = await pool.get_or_create(session_id=smoke.session_id, entry_skill_id="entry")
    outcome = await _drive(engine, taifeng.UserMessage(text=f"记一笔：{_SECRET}"))
    smoke.check(outcome == "turn_suspended", f"turn 在工具调用处停下等审批（{outcome}）")
    thread_id = engine.thread_id
    await pool.close()
    types = [e.record_type for e in await smoke.journal()]
    smoke.check("turn_suspended" in types and "session_detached" in types,
                "挂起与释放落账（turn_suspended / session_detached），Session 未终结")
    smoke.check("session_ended" not in types, "没有 session_ended")

    pool = await smoke.pool(
        model_client=smoke.real_client(), journal="journal", threads="threads",
        tools=_tools(smoke.written),
    )
    engine = await pool.get_or_create(
        session_id=smoke.session_id, entry_skill_id="entry", resume_thread_id=thread_id,
    )
    record = engine._find_active_suspension()  # noqa: SLF001
    smoke.check(record is not None, "接管后挂起仍在等待")
    if record is None:
        await pool.close()
        return thread_id
    resolutions = {p.request_id: {"granted": True, "reason": "冒烟批准"} for p in record.pending}
    outcome = await _drive(engine, Resume(thread_id=thread_id, resolutions=resolutions))
    smoke.check(outcome == "turn_completed", f"provider 接受重建的历史并续跑完成（{outcome}）")
    smoke.check(len(smoke.written) == 1 and "ORBIT-73KD" in smoke.written[0],
                "获批的调用执行了恰好一次")
    smoke.pool_obj = pool  # type: ignore[attr-defined]
    smoke.engine = engine  # type: ignore[attr-defined]
    return thread_id


async def _part_b(smoke: _Smoke) -> None:
    """分离式派发。"""
    print("\n[B] 分离式派发（spawn_skill → wait_peer）")
    engine = smoke.engine  # type: ignore[attr-defined]
    outcome = await _drive(engine, taifeng.UserMessage(text="派人算一下。"))
    smoke.check(outcome == "turn_completed", f"派发的 turn 完成（{outcome}）")
    for _ in range(200):
        if not engine.has_live_spawns():
            break
        await asyncio.sleep(0.1)
    envelopes = await smoke.journal()
    started = [e for e in envelopes if e.record_type == "spawn_started"]
    settled = [e for e in envelopes if e.record_type == "spawn_settled"]
    smoke.check(len(started) >= 1 and len(settled) == len(started), "派发的发起与终态都落账")
    if settled:
        smoke.check(settled[0].payload["status"] == "done", f"worker 正常结束（{settled[0].payload['status']}）")
        smoke.check("42" in str(settled[0].payload.get("result")), "worker 的结果是 17+25=42")
        child = started[0].payload["child_thread_id"]
        child_llm = [e for e in envelopes if e.record_type == "llm_request_committed" and e.thread_id == child]
        smoke.check(len(child_llm) >= 1, "子 skill 的 LLM 调用记在子 thread 名下")
    final = [i for i in engine.history_snapshot() if i.kind == "assistant_message"]
    smoke.check(bool(final) and "42" in str(final[-1].payload.get("text")), "root 把 worker 的结果告诉了用户")
    await smoke.pool_obj.close()  # type: ignore[attr-defined]
    await smoke.healthy("A+B")


async def _part_c(smoke: _Smoke) -> None:
    """Timeline 与脱敏视图。"""
    print("\n[C] Timeline 三种视图")
    core = JsonlSessionJournalCore(smoke.root / "journal")
    projector = JournalTimelineProjector(core)
    full = await projector.read(smoke.session_id)
    redacted = await projector.read(smoke.session_id, view="redacted")
    metadata = await projector.read(smoke.session_id, view="metadata_only")
    smoke.check(len(full.items) == len(redacted.items) == len(metadata.items) > 0,
                f"三种视图条目数一致（{len(full.items)} 条）")
    smoke.check(any("ORBIT-73KD" in str(i.payload) for i in full.items), "完整视图里能读到用户原文")
    smoke.check(not any("ORBIT-73KD" in str(i.payload) for i in redacted.items), "脱敏视图里没有用户原文")
    smoke.check(all(a.payload_hash == b.payload_hash for a, b in zip(full.items, redacted.items, strict=True)),
                "脱敏视图保留原 payload hash")
    smoke.check(metadata.audit_complete is False, "metadata_only 显式不完整")


async def _part_d(smoke: _Smoke) -> None:
    """录后重放：不触网。"""
    print("\n[D] 录后重放（不触网）")
    records = await smoke.journal()
    replay_written: list[str] = []
    tools, ledger = replay_tools(_tools(replay_written), recorded_tool_calls(records))
    client = JournalReplayClient.from_records(records)
    pool = await smoke.pool(
        model_client=AttemptObservableClientAdapter(
            client, provider=smoke.provider, default_model=smoke.model,
        ),
        journal="replay_journal", threads="replay_threads", tools=tools,
    )
    engine = await pool.get_or_create(session_id=smoke.session_id, entry_skill_id="entry")
    live: dict[str, Any] = {"pool": pool, "engine": engine}

    async def reopen(current: Any) -> Any:
        # 录制里的释放 → 接管在重放里照做一遍：关掉 pool，用同一份回放客户端与工具台账重建
        thread_id = current.thread_id
        await live["pool"].close()
        live["pool"] = await smoke.pool(
            model_client=AttemptObservableClientAdapter(
                client, provider=smoke.provider, default_model=smoke.model,
            ),
            journal="replay_journal", threads="replay_threads", tools=tools,
        )
        live["engine"] = await live["pool"].get_or_create(
            session_id=smoke.session_id, entry_skill_id="entry", resume_thread_id=thread_id,
        )
        return live["engine"]

    report = await replay_session(engine, recorded_submissions(records), reopen=reopen)
    engine, pool = live["engine"], live["pool"]
    for _ in range(200):
        if not engine.has_live_spawns():
            break
        await asyncio.sleep(0.1)
    smoke.check(not report.diverged, f"重放没有分叉（{[s.outcome for s in report.steps]}）")
    smoke.check(replay_written == [], "业务工具没有被执行")
    smoke.check(ledger.remaining == 0 and client.remaining == 0,
                f"录制全部被消费（工具剩 {ledger.remaining}，LLM 剩 {client.remaining}）")
    await pool.close()
    await smoke.healthy("重放", "replay_journal")


async def main() -> int:
    try:
        client, meta = build_model_client(retry=False)
    except ProviderBootstrapError as exc:
        print(f"❌ 无法构造客户端：{exc}")
        return 2
    print(f"provider={meta['provider']} model={meta['model']}")
    keep = os.environ.get("TAIFENG_SMOKE_DIR")  # 留下 Journal 供事后查看；不设则用临时目录
    with tempfile.TemporaryDirectory() as tmp:
        smoke = _Smoke(Path(keep or tmp), client, meta)
        await _part_a(smoke)
        if hasattr(smoke, "engine"):
            await _part_b(smoke)
            await _part_c(smoke)
            await _part_d(smoke)
    if smoke.failures:
        print(f"\n❌ {len(smoke.failures)} 项失败：" + "；".join(smoke.failures))
        return 1
    print("\n✅ 审计模式真实 LLM 冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
