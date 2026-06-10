"""derive_rewind_log 推导 + 冷场景 rewind。"""
from __future__ import annotations

import taifeng
from taifeng.conversation.models import (
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.rewind import derive_rewind_log
from taifeng.loop.submission import Rewind

T = "thr"


def _u(): return user_message("u", thread_id=T)
def _a(): return assistant_message("a", thread_id=T, model="m")
def _fc(cid, name="read_skill", args="{}"): return function_call(cid, name, args, thread_id=T)
def _fco(cid): return function_call_output(cid, output="ok", thread_id=T, is_error=False)


def test_derive_single_turn_iterations_and_dispatch():
    """一 turn:[u, a, fc, fco, a](2 圈,首圈 1 派发)→ 2 iteration + 1 dispatch。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1"), _a()]
    nodes = derive_rewind_log(hist)
    ids = [n.node_id for n in nodes]
    assert ids == ["t1:it1", "t1:disp0", "t1:it2"]
    disp = next(n for n in nodes if n.node_id == "t1:disp0")
    assert disp.history_len == 1          # 所属 it1 的 history_len(assistant_message 下标)
    assert disp.inner_history_len == 3    # fc 下标(2)+1
    assert disp.call_id == "c1"


def test_derive_same_iteration_multiple_dispatch_share_history_len():
    """同圈 2 派发:[u, a, fc1, fco1, fc2, fco2] → 两 dispatch 共享 it1 的 history_len=1。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1"), _fc("c2"), _fco("c2")]
    nodes = derive_rewind_log(hist)
    disps = [n for n in nodes if n.kind == "dispatch"]
    assert [d.node_id for d in disps] == ["t1:disp0", "t1:disp1"]
    assert all(d.history_len == 1 for d in disps)        # 归一到 it1 采样前
    assert [d.inner_history_len for d in disps] == [3, 5]  # 各自 fc 下标+1


def test_derive_empty_trailing_iteration_produces_no_node():
    """末圈空采样(无 assistant_message)不留 item → 不产节点。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1")]  # 首圈派发,末圈空(无尾 assistant)
    nodes = derive_rewind_log(hist)
    assert [n.node_id for n in nodes] == ["t1:it1", "t1:disp0"]


def test_derive_multi_turn_addressable():
    """两 turn → t1 / t2 前缀分别可寻址。"""
    hist = [_u(), _a(), _u(), _a()]
    ids = [n.node_id for n in derive_rewind_log(hist)]
    assert ids == ["t1:it1", "t2:it1"]


def test_derive_multi_turn_dispatch_seq_restarts_per_turn():
    """每 turn 的 dispatch 序号从 0 重启:[u,a,fc, u,a,fc] → disp 为 t1:disp0 / t2:disp0。"""
    hist = [_u(), _a(), _fc("c1"), _u(), _a(), _fc("c2")]
    disps = [n.node_id for n in derive_rewind_log(hist) if n.kind == "dispatch"]
    assert disps == ["t1:disp0", "t2:disp0"]


def test_derive_ignores_unknown_kinds_for_index():
    """spawn 等未知 kind 计入下标、不产节点,后续下标不偏移。"""
    from taifeng.conversation.models import spawn_item
    sp = spawn_item(handle_id="h", skill_id="s", child_thread_id="c", thread_id=T)
    hist = [_u(), sp, _a(), _fc("c1"), _fco("c1")]
    nodes = derive_rewind_log(hist)
    it1 = next(n for n in nodes if n.node_id == "t1:it1")
    disp = next(n for n in nodes if n.node_id == "t1:disp0")
    assert it1.history_len == 2          # assistant_message 在 spawn 之后,下标 2
    assert disp.inner_history_len == 4   # fc 下标(3)+1


# ── Task 5：奇偶校验 derive ≡ live RewindLog ─────────────────────────────


async def _run_to_root_end(engine: object, text: str) -> None:
    """提交 user message 并消费事件到 root turn 终结，等待节点表落地。

    复用 test_turn_rewind.py 的 settle 模式：turn_completed 在
    runner.run() 内发出，engine 状态在 run() 返回后回写；轮询直到
    rewind_nodes() 非空才返回。
    """
    import asyncio

    import taifeng

    assert isinstance(engine, taifeng.AgentEngine)

    sub_id = await engine.submit(taifeng.UserMessage(text=text))

    # 消费事件到 root turn 终结（参照 test_turn_rewind._consume_to_root_end）
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        kind = ev.msg.kind
        if kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break

    # 等节点表回写（engine 在 run() 返回后落 _rewind_checkpoints）
    for _ in range(100):
        if engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)


async def test_parity_derive_equals_live_recorded_events(
    skills_dir, threads_dir
) -> None:
    """奇偶校验：derive_rewind_log(history) ≡ turn.py 现场 live 记录事件序列。

    Task 8 后 engine.rewind_nodes() 直接返回 derive_rewind_log(self._history)，
    若继续与 rewind_nodes() 对比则变成 derive==derive 的同义反复，无法锁住
    「derive 结果与 TurnRunner live 增量记录路径一致」这一约束。

    本测试改为在 turn 开始前订阅事件流，收集所有 rewind_checkpoint_recorded
    事件，与 turn 结束后 derive_rewind_log(history_snapshot()) 的输出逐字段比对。
    这把 live 记录路径（turn.py emit 节点）作为独立真相源，即便 engine 改用
    derive 做热路径回写也不会降低锁的有效性。

    比对字段（事件 data 携带的字段子集）：
    - node_id、kind、iteration_index、history_len、target_id

    注：事件不携带 inner_history_len / args_digest / call_id / turn_index，
    故这些字段由其他单元测试（test_rewind_cold 的 pure derive 测试）覆盖。
    """
    import asyncio

    import taifeng
    from taifeng.llm.providers import SimClient, SimTurn

    # 脚本覆盖：圈 1/2 各一次 read_skill，圈 3 无工具纯文本收尾
    client = SimClient(turns=[
        SimTurn(text="圈1推理", tool_calls=[
            {
                "id": "tc1",
                "name": "read_skill",
                "arguments": '{"skill_id":"style-checker"}',
            },
        ]),
        SimTurn(text="圈2推理", tool_calls=[
            {
                "id": "tc2",
                "name": "read_skill",
                "arguments": '{"skill_id":"style-checker"}',
            },
        ]),
        SimTurn(text="收尾推理"),  # 圈 3：无工具调用
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s_parity", entry_skill_id="code-reviewer"
    )

    # ── 订阅事件流，在 turn 运行期间收集所有 rewind_checkpoint_recorded 事件 ──
    sub_id = await engine.submit(taifeng.UserMessage(text="请审查代码"))

    # 每个 rewind_checkpoint_recorded 事件的 data 字段
    live_event_data: list[dict] = []
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        if ev.msg.kind == "rewind_checkpoint_recorded":
            # 采集 live 路径（turn.py 现场 emit）记录的字段
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            live_event_data.append(dict(data))
        kind = ev.msg.kind
        if kind in ("turn_completed", "turn_failed"):
            event_data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if event_data.get("is_root"):
                break

    # 等节点表回写
    for _ in range(100):
        if engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)

    # ── 取 derive 推导结果（post-turn history）─────────────────────────────
    derived = derive_rewind_log(engine.history_snapshot())

    # 基本数量断言（5 节点：3 iteration + 2 dispatch）
    assert len(live_event_data) == 5, (
        f"live 事件应有 5 个 rewind_checkpoint_recorded，实得 {len(live_event_data)}: "
        f"{[e['node_id'] for e in live_event_data]}"
    )
    derived_ids = [n.node_id for n in derived]
    assert len(derived) == len(live_event_data), (
        f"冷推导节点数({len(derived)}) ≠ live 事件数({len(live_event_data)})\n"
        f"derived node_ids: {derived_ids}\n"
        f"live events node_ids: {[e['node_id'] for e in live_event_data]}"
    )

    # ── 逐字段奇偶校验（事件携带字段子集）──────────────────────────────────
    # 比对字段：node_id / kind / iteration_index / history_len / target_id
    # 这些是 rewind_checkpoint_recorded 事件 data 中存在的全部字段，
    # 足以锁定 derive 的 id 方案、切点坐标、派发目标与 live 路径一致。
    divergence_report: list[str] = []
    for i, (d, ev_data) in enumerate(
        zip(derived, live_event_data, strict=True)
    ):
        # node_id 是核心定位键，必须完全一致
        if d.node_id != ev_data.get("node_id"):
            divergence_report.append(
                f"[{i}] node_id 不一致: "
                f"derived={d.node_id!r} live_event={ev_data.get('node_id')!r}"
            )
        # kind：iteration / dispatch
        if d.kind != ev_data.get("kind"):
            divergence_report.append(
                f"[{i}:{d.node_id}] kind 不一致: "
                f"derived={d.kind!r} live_event={ev_data.get('kind')!r}"
            )
        # iteration_index：所属采样圈序号
        if d.iteration_index != ev_data.get("iteration_index"):
            divergence_report.append(
                f"[{i}:{d.node_id}] iteration_index 不一致: "
                f"derived={d.iteration_index} "
                f"live_event={ev_data.get('iteration_index')}"
            )
        # history_len：re_reason 截断切点（下标）
        if d.history_len != ev_data.get("history_len"):
            divergence_report.append(
                f"[{i}:{d.node_id}] history_len 不一致: "
                f"derived={d.history_len} "
                f"live_event={ev_data.get('history_len')}"
            )
        # target_id：工具 / call_skill 名称（iteration 为 None，dispatch 为 tool 名）
        if d.target_id != ev_data.get("target_id"):
            divergence_report.append(
                f"[{i}:{d.node_id}] target_id 不一致: "
                f"derived={d.target_id!r} "
                f"live_event={ev_data.get('target_id')!r}"
            )

    # 若有分歧，打印完整报告再 fail（便于 controller 定位首个分歧）
    assert not divergence_report, (
        "derive_rewind_log 与 live rewind_checkpoint_recorded 事件出现分歧！\n"
        "首个分歧节点在报告第 1 条：\n"
        + "\n".join(divergence_report)
    )

    await pool.close()


# ── Task 6：rewind/rollback marker 持久化 cut_index ──────────────────────────


async def _drain_to_root_end(
    engine: taifeng.AgentEngine, sub_id: str
) -> None:
    """消费某 submission 的事件直到 root turn 终结（is_root=True 的 turn_completed/failed）。"""
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        kind = ev.msg.kind
        if kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break


async def test_rewind_marker_persists_cut_index(
    skills_dir: object, threads_dir: object
) -> None:
    """rewind 后 store 里的 rewind marker payload 含 cut_index。

    具体：提交 re_reason rewind 到 t1:it2 节点，等重推完成，
    从 store 加载全部 items，找到 source=='rewind' 的 system_injection，
    断言其 payload['cut_index'] == it2.history_len（即 rewind 截断点）。
    """
    import asyncio

    # 三圈模型：圈 1/2 各一次 read_skill，圈 3 收尾；rewind 后重推用第 4 条
    client = SimClient(turns=[
        SimTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'},
        ]),
        SimTurn(text="圈2", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'},
        ]),
        SimTurn(text="原收尾"),
        # rewind 重推消费：直接无工具收尾
        SimTurn(text="REDRIVE-DONE"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,  # type: ignore[arg-type]
        threads_dir=threads_dir,  # type: ignore[arg-type]
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s_marker_cut", entry_skill_id="code-reviewer"
    )

    # 第一轮 turn，等节点表落地
    sub_id = await engine.submit(taifeng.UserMessage(text="请审查"))
    await _drain_to_root_end(engine, sub_id)
    for _ in range(100):
        if engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)

    # 取 t1:it2 节点（圈 2 的 iteration 节点）
    it2 = next(n for n in engine.rewind_nodes() if n.node_id == "t1:it2")
    expected_cut = it2.history_len

    # 提交 re_reason rewind 并等重推完成
    rw_sub_id = await engine.submit(Rewind(node_id="t1:it2", mode="re_reason"))
    await _drain_to_root_end(engine, rw_sub_id)

    # 从 store 加载所有 items，找 source=='rewind' 的 system_injection
    thread_id = engine.thread_id
    items = [it async for it in await engine._store.load_thread(thread_id)]  # type: ignore[attr-defined]
    rewind_markers = [
        it for it in items
        if it.kind == "system_injection" and it.payload.get("source") == "rewind"
    ]

    assert rewind_markers, "store 中应有 source='rewind' 的 system_injection marker"
    # 取最后一条 rewind marker（对应本次 rewind 操作）
    marker_payload = rewind_markers[-1].payload
    assert "cut_index" in marker_payload, (
        f"rewind marker payload 缺少 cut_index 字段: {marker_payload}"
    )
    assert marker_payload["cut_index"] == expected_cut, (
        f"cut_index={marker_payload['cut_index']} 应等于 it2.history_len={expected_cut}"
    )

    await pool.close()


# ── Task 7：冷加载重建 rewind 节点表 ─────────────────────────────────────────


async def test_cold_load_rebuilds_rewind_table(
    skills_dir: object, threads_dir: object
) -> None:
    """热跑一 turn → 用同线程 initial_history 构造新 engine → rewind_nodes() 非空。

    步骤：
    1. 热跑一个 turn，拿到 thread_id 与持久化 transcript；
    2. 用同一 store / thread_id 新建一个 engine，传 initial_history=该 transcript
       （模拟冷 worker：engine.__init__ 收到 initial_history）；
    3. 断言新 engine.rewind_nodes() 非空（冷重建成功）；
    4. 验证 node_id 与热路径一致（冷推导坐标系自洽）。
    """
    import asyncio
    from pathlib import Path

    import taifeng
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.llm.providers import SimClient, SimTurn
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    # ── 步骤 1：热跑一个 turn ──────────────────────────────────────────────
    client = SimClient(turns=[
        SimTurn(text="圈1推理", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'},
        ]),
        SimTurn(text="收尾"),   # 圈 2，无工具
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,  # type: ignore[arg-type]
        threads_dir=threads_dir,  # type: ignore[arg-type]
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s_cold_rebuild", entry_skill_id="code-reviewer"
    )

    # 等待 turn 完成 + 节点表落地
    sub_id = await engine.submit(taifeng.UserMessage(text="请审查"))
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break
    for _ in range(100):
        if engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)

    # 热 engine 的节点表与 thread_id
    live_nodes = engine.rewind_nodes()
    assert live_nodes, "热 engine 节点表不应为空"
    thread_id = engine.thread_id
    await pool.close()

    # ── 步骤 2：从 store 物化 transcript ─────────────────────────────────
    cold_store = JsonlMessageStore(Path(str(threads_dir)))  # type: ignore[arg-type]
    transcript = [it async for it in await cold_store.load_thread(thread_id)]
    assert transcript, "transcript 不应为空"

    # ── 步骤 3：构造冷 engine，验证 rewind_nodes() 非空 ─────────────────
    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()  # type: ignore[arg-type]
    entry = snapshot.get("code-reviewer")
    assert entry is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    cold_engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=client,
        store=cold_store,
        thread_id=thread_id,
        session_id="s_cold_rebuild_2",
        compressors=None,
        initial_history=transcript,  # 注入 transcript → 触发冷重建
    )

    cold_nodes = cold_engine.rewind_nodes()
    assert cold_nodes, (
        f"冷 engine 的 rewind_nodes() 不应为空，"
        f"transcript 长度={len(transcript)}，live 节点={[n.node_id for n in live_nodes]}"
    )

    # ── 步骤 4：验证 node_id 与热路径一致 ──────────────────────────────
    live_ids = [n.node_id for n in live_nodes]
    cold_ids = [n.node_id for n in cold_nodes]
    # 冷推导 cache_anchor 为 -1，但 node_id/history_len/kind 应与热路径一致
    assert cold_ids == live_ids, (
        f"冷重建 node_ids={cold_ids} 应与热路径 live_ids={live_ids} 一致"
    )


async def test_cold_load_then_re_reason_executes(
    skills_dir: object, threads_dir: object,
) -> None:
    """冷加载后提交 re_reason Rewind → 实际执行成功，turn_rewound 事件可观测。

    步骤：
    1. 热跑一个含多圈的 turn（圈 1 有 read_skill 派发，圈 2 收尾）；
    2. 从持久化 transcript 构造冷 engine（与热 engine 完全独立）；
    3. 启动冷 engine 的 run() task；
    4. 向冷 engine 提交 Rewind(node_id="t1:it1", mode="re_reason")；
    5. 消费事件到 root turn 终结，断言：
       - "turn_rewound" 事件被发出；
       - rewound.data["node_id"] == "t1:it1"；
       - rewound.data["mode"] == "re_reason"；
       - 重推的 LLM 文本（"COLD-REDRIVE-DONE"）出现在文本流中。

    这是端到端验证冷 rewind 不仅可寻址而且真正可执行的关键测试。
    """
    import asyncio
    from pathlib import Path

    import taifeng
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.submission import Rewind
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    # ── 步骤 1：热跑一个包含两圈的 turn ─────────────────────────────────
    # 热跑的 SimClient：圈 1 有 read_skill 派发，圈 2 收尾
    hot_client = SimClient(turns=[
        SimTurn(text="圈1推理", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'},
        ]),
        SimTurn(text="原收尾"),  # 圈 2，无工具
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,  # type: ignore[arg-type]
        threads_dir=threads_dir,  # type: ignore[arg-type]
        model_client=hot_client,
        compressors=[],
    )
    hot_engine = await pool.get_or_create(
        session_id="s_cold_rerun_hot", entry_skill_id="code-reviewer"
    )

    # 等待热 turn 完成 + 节点表落地
    hot_sub = await hot_engine.submit(taifeng.UserMessage(text="请审查代码"))
    async for ev in hot_engine.subscribe_all():
        if ev.submission_id != hot_sub:
            continue
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break
    for _ in range(100):
        if hot_engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)

    hot_nodes = hot_engine.rewind_nodes()
    assert hot_nodes, "热 engine 节点表不应为空"
    thread_id = hot_engine.thread_id
    await pool.close()

    # ── 步骤 2：从 store 物化 transcript ─────────────────────────────────
    cold_store = JsonlMessageStore(Path(str(threads_dir)))  # type: ignore[arg-type]
    transcript = [it async for it in await cold_store.load_thread(thread_id)]
    assert transcript, "transcript 不应为空"

    # ── 步骤 3：构造冷 engine ─────────────────────────────────────────────
    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()  # type: ignore[arg-type]
    entry = snapshot.get("code-reviewer")
    assert entry is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    # 冷 engine 的 SimClient：仅提供 rewind 重推消费的那一轮 LLM 回答
    cold_client = SimClient(turns=[
        SimTurn(text="COLD-REDRIVE-DONE"),  # re_reason 重推从 it1 截点采样，直接无工具收尾
    ])
    cold_engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=cold_client,
        store=cold_store,
        thread_id=thread_id,
        session_id="s_cold_rerun_cold",
        compressors=None,
        initial_history=transcript,  # 冷重建触发
    )

    cold_nodes = cold_engine.rewind_nodes()
    assert cold_nodes, "冷 engine 构造后节点表不应为空"

    # ── 步骤 4：启动冷 engine，提交 re_reason rewind ─────────────────────
    cancel = CancellationToken(name="cold-rerun-root")
    run_task = asyncio.create_task(cold_engine.run(cancel))

    # 取第一个 iteration 节点（t1:it1），用 re_reason 模式回访
    it1 = next(n for n in cold_nodes if n.node_id == "t1:it1")
    rw_sub_id = await cold_engine.submit(Rewind(node_id=it1.node_id, mode="re_reason"))

    # ── 步骤 5：消费事件到 root turn 终结，断言可观测结果 ─────────────────
    kinds: list[str] = []
    text_parts: list[str] = []
    rewound_data: dict = {}
    async for ev in cold_engine.subscribe_all():
        if ev.submission_id != rw_sub_id:
            continue
        kinds.append(ev.msg.kind)
        data = ev.msg.data if hasattr(ev.msg, "data") else {}
        if ev.msg.kind == "assistant_text" and data.get("delta"):
            text_parts.append(str(data["delta"]))
        if ev.msg.kind == "turn_rewound":
            rewound_data = dict(data)
        # root turn 终结（is_root=True 的 turn_completed 或 turn_failed）
        if ev.msg.kind in ("turn_completed", "turn_failed") and data.get("is_root"):
            break

    # 关停冷 engine
    await cold_engine.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        run_task.cancel()

    # ── 核心断言：turn_rewound 被发出且字段正确 ──────────────────────────
    assert "turn_rewound" in kinds, (
        f"应发出 turn_rewound 事件，实际 kinds={kinds}"
    )
    assert rewound_data.get("node_id") == "t1:it1", (
        f"rewound.node_id 应为 't1:it1'，实得 {rewound_data.get('node_id')!r}"
    )
    assert rewound_data.get("mode") == "re_reason", (
        f"rewound.mode 应为 're_reason'，实得 {rewound_data.get('mode')!r}"
    )
    assert "COLD-REDRIVE-DONE" in "".join(text_parts), (
        f"重推应走出新文本 'COLD-REDRIVE-DONE'，实得: {''.join(text_parts)!r}"
    )


async def test_cold_load_empty_history_empty_table(
    skills_dir: object, threads_dir: object
) -> None:
    """空 initial_history → 空 history + 空节点表，不报错。"""
    from pathlib import Path

    import taifeng
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.llm.providers import SimClient, SimTurn
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()  # type: ignore[arg-type]
    entry = snapshot.get("code-reviewer")
    assert entry is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    store = JsonlMessageStore(Path(str(threads_dir)))  # type: ignore[arg-type]
    client = SimClient(turns=[SimTurn(text="hello")])

    # 空 initial_history（明确传入空列表，模拟"有 initial_history 但内容为空"）
    cold_engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=client,
        store=store,
        thread_id="nonexistent-thread",
        session_id="s_empty_cold",
        compressors=None,
        initial_history=[],  # 空 transcript
    )

    assert cold_engine.rewind_nodes() == [], (
        "空 initial_history 应产生空节点表"
    )
    assert cold_engine.history_snapshot() == [], (
        "空 initial_history 应产生空 history"
    )


# ── Task 8：冷加载后跑新 turn，t1/t2 前缀严格递增无重复 ─────────────────────


async def test_cold_then_new_turn_node_ids_no_collision(
    skills_dir: object, threads_dir: object
) -> None:
    """冷加载 1 个历史 turn → 跑新 turn → t1 / t2 前缀严格递增、无重复。

    步骤：
    1. 热跑第一 turn（圈 1 有 read_skill 派发，圈 2 收尾）；
    2. 持久化 transcript，用该 transcript 构造冷 engine（initial_history）；
    3. 启动冷 engine 的 run() task，向其提交第二条 UserMessage 跑完；
    4. 断言：
       - rewind_nodes() 同时含 t1: 与 t2: 前缀节点；
       - 所有 node_id 无重复（set 长度 == list 长度）。

    这是关键回归：热路径改为 derive 重算后，冷加载推导的 t1 节点
    与新 turn 产出的 t2 节点共存，derive 在完整 history 上重算不会
    因 _turn_index 偏差导致 id 碰撞或旧节点被覆盖。
    """
    import asyncio
    from pathlib import Path

    import taifeng
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    # ── 步骤 1：热跑第一 turn ─────────────────────────────────────────────
    hot_client = SimClient(turns=[
        SimTurn(text="圈1推理", tool_calls=[
            {
                "id": "c1",
                "name": "read_skill",
                "arguments": '{"skill_id":"style-checker"}',
            },
        ]),
        SimTurn(text="原收尾"),  # 圈 2，无工具
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,  # type: ignore[arg-type]
        threads_dir=threads_dir,  # type: ignore[arg-type]
        model_client=hot_client,
        compressors=[],
    )
    hot_engine = await pool.get_or_create(
        session_id="s_t1t2_hot", entry_skill_id="code-reviewer"
    )

    # 等待热 turn 完成 + 节点表落地
    hot_sub = await hot_engine.submit(
        taifeng.UserMessage(text="请审查第一轮")
    )
    async for ev in hot_engine.subscribe_all():
        if ev.submission_id != hot_sub:
            continue
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break
    for _ in range(100):
        if hot_engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)

    t1_nodes = hot_engine.rewind_nodes()
    assert t1_nodes, "热 engine 节点表不应为空"
    assert all(n.node_id.startswith("t1:") for n in t1_nodes), (
        f"第一 turn 节点应全部以 t1: 开头，实得: "
        f"{[n.node_id for n in t1_nodes]}"
    )
    thread_id = hot_engine.thread_id
    await pool.close()

    # ── 步骤 2：从 store 物化 transcript，构造冷 engine ──────────────────
    cold_store = JsonlMessageStore(
        Path(str(threads_dir))  # type: ignore[arg-type]
    )
    transcript = [
        it async for it in await cold_store.load_thread(thread_id)
    ]
    assert transcript, "transcript 不应为空"

    snapshot = (
        await FilesystemSkillRegistry.load(
            skills_dir  # type: ignore[arg-type]
        )
    ).snapshot()
    entry = snapshot.get("code-reviewer")
    assert entry is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    # 冷 engine 的 SimClient：第二 turn 消费
    cold_client = SimClient(turns=[
        SimTurn(text="第二轮收尾"),  # 新 turn，无工具，直接收尾
    ])
    cold_engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=cold_client,
        store=cold_store,
        thread_id=thread_id,
        session_id="s_t1t2_cold",
        compressors=None,
        initial_history=transcript,  # 冷重建：t1 节点由 derive 产出
    )

    cold_nodes_before = cold_engine.rewind_nodes()
    assert cold_nodes_before, "冷 engine 构造后 t1 节点不应为空"
    assert all(
        n.node_id.startswith("t1:") for n in cold_nodes_before
    ), (
        f"冷加载后节点应全为 t1: 前缀，实得: "
        f"{[n.node_id for n in cold_nodes_before]}"
    )

    # ── 步骤 3：启动冷 engine，提交第二条 UserMessage 跑完 ─────────────────
    cancel = CancellationToken(name="t1t2-root")
    run_task = asyncio.create_task(cold_engine.run(cancel))

    warm_sub = await cold_engine.submit(
        taifeng.UserMessage(text="请审查第二轮")
    )
    async for ev in cold_engine.subscribe_all():
        if ev.submission_id != warm_sub:
            continue
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break

    # 等节点表回写（含新 t2 节点）
    for _ in range(100):
        nodes_after = cold_engine.rewind_nodes()
        if any(n.node_id.startswith("t2:") for n in nodes_after):
            break
        await asyncio.sleep(0.01)

    # 关停冷 engine
    await cold_engine.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        run_task.cancel()

    # ── 步骤 4：断言 t1/t2 前缀共存，无重复 ────────────────────────────────
    all_nodes = cold_engine.rewind_nodes()
    all_ids = [n.node_id for n in all_nodes]

    t1_ids = [nid for nid in all_ids if nid.startswith("t1:")]
    t2_ids = [nid for nid in all_ids if nid.startswith("t2:")]

    assert t1_ids, (
        f"应含 t1: 节点（冷加载历史 turn），实得 all_ids={all_ids}"
    )
    assert t2_ids, (
        f"应含 t2: 节点（新跑 turn），实得 all_ids={all_ids}"
    )
    # node_id 全局唯一：set 长度等于 list 长度
    assert len(set(all_ids)) == len(all_ids), (
        f"node_id 出现重复！all_ids={all_ids}"
    )


# ── Task 9：冷 rewind 惰性 resolve 指令层 ────────────────────────────────────


async def test_cold_rewind_resolves_instructions(
    skills_dir: object, threads_dir: object,
) -> None:
    """冷 engine 首次 rewind 前 _last_resolved 为空 → 惰性 resolve,re-run 带指令层。

    设计说明（spec §7 lazy-on-rewind）：
        - _last_resolved 在 _handle_rewind 时若为空且 _instruction_resolver 非 None
          且 _history 非空，应惰性 resolve（以构造期 entry skill 为锚点）。
        - 使用静态文本 InstructionLayer（source=str，scope="engine"）—— 永不发生
          远程 fetch 失败，结果确定性强，适合 CI 场景。
        - 热路径（正常 turn）在 resolve 后会把结果写入 _last_resolved；冷路径
          （initial_history 注入、未跑任何 turn）此字段为空，rewind 须惰性补齐。

    断言策略：
        - 冷构造后 _last_resolved 为空（证明 bug 存在条件）；
        - rewind 后 _last_resolved 非空，且含有静态层文本（证明惰性 resolve 生效）；
        - rewind 后 turn_rewound + turn_completed 事件均正常发出（rewind 流程未破坏）。

    注：热路径的 resolve 包含 engine/session/turn 三档；惰性 resolve 仅做
    engine+session+turn 三档（与 _handle_user_message 路径完全对称），因此
    静态 engine 层的文本必然出现在结果中。
    """
    import asyncio
    from pathlib import Path

    import taifeng
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.instructions.types import InstructionLayer
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.submission import Rewind
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    # 静态指令层文本（唯一性足够，避免误中 skill body 内容）
    instruction_text = "COLD_REWIND_INSTRUCTION_SENTINEL_XYZ"

    # ── 步骤 1：热跑一个含两圈的 turn，持久化 transcript ──────────────────
    hot_client = SimClient(turns=[
        SimTurn(text="圈1推理", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'},
        ]),
        SimTurn(text="原收尾"),  # 圈 2，无工具
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,  # type: ignore[arg-type]
        threads_dir=threads_dir,  # type: ignore[arg-type]
        model_client=hot_client,
        compressors=[],
        # 热 pool 也注入同一指令层，使热路径的 _last_resolved 有值可参照
        instruction_layers=[
            InstructionLayer(
                name="sentinel",
                source=instruction_text,
                scope="engine",
                priority=10,
            ),
        ],
    )
    hot_engine = await pool.get_or_create(
        session_id="s_instr_hot", entry_skill_id="code-reviewer"
    )
    hot_sub = await hot_engine.submit(taifeng.UserMessage(text="请审查代码"))
    async for ev in hot_engine.subscribe_all():
        if ev.submission_id != hot_sub:
            continue
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break
    for _ in range(100):
        if hot_engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)

    hot_last_resolved = hot_engine.instructions_snapshot()
    assert hot_last_resolved, "热 engine 跑完 turn 后 instructions_snapshot() 不应为空"
    hot_texts = [r.text for r in hot_last_resolved]
    assert any(instruction_text in t for t in hot_texts), (
        f"热路径 _last_resolved 应含 instruction_text，实得 texts={hot_texts}"
    )
    thread_id = hot_engine.thread_id
    await pool.close()

    # ── 步骤 2：从 store 物化 transcript ─────────────────────────────────
    cold_store = JsonlMessageStore(Path(str(threads_dir)))  # type: ignore[arg-type]
    transcript = [it async for it in await cold_store.load_thread(thread_id)]
    assert transcript, "transcript 不应为空"

    # ── 步骤 3：构造冷 engine（注入同一指令层，但不跑任何 turn）──────────
    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()  # type: ignore[arg-type]
    entry = snapshot.get("code-reviewer")
    assert entry is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    cold_client = SimClient(turns=[
        SimTurn(text="COLD-INSTR-REWIND-DONE"),  # re_reason 重推直接无工具收尾
    ])
    cold_engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=cold_client,
        store=cold_store,
        thread_id=thread_id,
        session_id="s_instr_cold",
        compressors=None,
        initial_history=transcript,
        instruction_layers=[
            InstructionLayer(
                name="sentinel",
                source=instruction_text,
                scope="engine",
                priority=10,
            ),
        ],
    )

    # ── 步骤 4：冷构造后，_last_resolved 应为空（验证 bug 存在条件）────────
    # 注：instructions_snapshot() 在 _last_resolved 为空时退回 _engine_scope_resolved；
    # 而 warmup_engine_scope 未在构造时调用 → 两者皆空。
    # 直接检查内部字段（测试白盒，验证 bug 先决条件）。
    assert cold_engine._last_resolved == [], (  # type: ignore[attr-defined]
        "冷 engine 构造后 _last_resolved 应为空（尚未跑任何 turn）"
    )
    cold_nodes = cold_engine.rewind_nodes()
    assert cold_nodes, "冷 engine 节点表不应为空（冷重建应已成功）"

    # ── 步骤 5：启动冷 engine，提交 re_reason rewind ──────────────────────
    cancel = CancellationToken(name="cold-instr-rewind-root")
    run_task = asyncio.create_task(cold_engine.run(cancel))

    it1 = next(n for n in cold_nodes if n.node_id == "t1:it1")
    rw_sub_id = await cold_engine.submit(Rewind(node_id=it1.node_id, mode="re_reason"))

    # ── 步骤 6：消费事件到 root turn 终结 ────────────────────────────────
    kinds: list[str] = []
    text_parts: list[str] = []
    async for ev in cold_engine.subscribe_all():
        if ev.submission_id != rw_sub_id:
            continue
        kinds.append(ev.msg.kind)
        data = ev.msg.data if hasattr(ev.msg, "data") else {}
        if ev.msg.kind == "assistant_text" and data.get("delta"):
            text_parts.append(str(data["delta"]))
        if ev.msg.kind in ("turn_completed", "turn_failed") and data.get("is_root"):
            break

    await cold_engine.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        run_task.cancel()

    # ── 步骤 7：核心断言 ──────────────────────────────────────────────────
    # 7a. rewind 流程正常（turn_rewound + root turn_completed）
    assert "turn_rewound" in kinds, (
        f"应发出 turn_rewound 事件，实际 kinds={kinds}"
    )
    assert "COLD-INSTR-REWIND-DONE" in "".join(text_parts), (
        f"重推应输出 'COLD-INSTR-REWIND-DONE'，实得: {''.join(text_parts)!r}"
    )

    # 7b. 惰性 resolve 生效：_last_resolved 已被填充，含有指令层文本
    cold_resolved = cold_engine._last_resolved  # type: ignore[attr-defined]
    assert cold_resolved, (
        "冷 rewind 后 _last_resolved 应非空（惰性 resolve 应已生效）"
    )
    cold_texts = [r.text for r in cold_resolved]
    assert any(instruction_text in t for t in cold_texts), (
        f"冷 rewind 后 _last_resolved 应含 instruction_text，"
        f"实得 texts={cold_texts}"
    )


# ── spec §9 缺口 Test 1：压缩过的 thread 冷 rewind ──────────────────────────────


async def test_cold_rewind_compacted_thread_reconstructed_correctly(
    skills_dir: object, threads_dir: object,
) -> None:
    """压缩过的 thread 冷 rewind：engine 构造后 history 是重建的逻辑 history。

    **选用合成 transcript（非真实压缩引擎触发）的原因**：
    强制触发真实 mid/pre-turn 压缩需要极小的 ContextBudget + 足够多的 turns，
    且压缩时机由 token estimate 控制，与 turn 调度有时序耦合；在 CI 场景下
    偶发性高，难以锁定确定性切点坐标。合成方案直接构造与压缩后磁盘布局等价
    的 transcript（raw_items 含被折叠项 + compacted placeholder），再注入真实
    AgentEngine.__init__，完整测试 reconstruct→derive 路径（这正是 spec §9 关注
    的核心：冷加载时废弃项被折叠、后续 turn 可寻址、前置废弃项不可寻址）。

    断言：
    1. 冷 engine.history_snapshot() = reconstruct 后的逻辑 history
       - 不含被 compacted 折叠的 superseded 项（id 不在 history 中）；
       - compacted placeholder 自身出现在 history 中；
       - 后续（未被折叠的）turn items 出现在 history 中。
    2. rewind_nodes() 仅含位于压缩边界之后的 turn 的节点（post-compaction 迭代可寻址）。
    3. 提交一个 bogus（压缩前废弃区域内）node_id → engine 发出 rewind_rejected，
       reason='unknown_node'（pre-compaction 折叠节点不可寻址）。
    """
    import asyncio
    from pathlib import Path

    import taifeng
    from taifeng.conversation.models import (
        ResponseItem,
        assistant_message,
        compacted,
        user_message,
    )
    from taifeng.conversation.reconstruct import reconstruct_logical_history
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.rewind import derive_rewind_log
    from taifeng.loop.submission import Rewind
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    tid = "thr-compacted-cold"

    # ── 构造合成 transcript（模拟 3 轮对话 + 压缩第 1-2 轮后的磁盘布局）──────
    # 热内存历史在压缩前：[u1, a1, u2, a2, u3, a3]（3 轮各一条 user+assistant）
    # 压缩后热内存变为：[ph, u3, a3]（replaced_range=(0,4) 折叠前 4 项）
    # 磁盘（append-only）保留全部原始项，compacted 追加到末尾

    u1 = user_message("第一轮用户消息", thread_id=tid)
    a1 = assistant_message("第一轮助手回复", thread_id=tid, model="mock-model")
    u2 = user_message("第二轮用户消息", thread_id=tid)
    a2 = assistant_message("第二轮助手回复", thread_id=tid, model="mock-model")
    u3 = user_message("第三轮用户消息", thread_id=tid)
    a3 = assistant_message("第三轮助手回复", thread_id=tid, model="mock-model")

    # placeholder 描述：压缩 [u1, a1, u2, a2]（history 中 index 0..3 → replaced_range=(0,4)）
    ph = compacted(
        "前两轮对话压缩摘要",
        thread_id=tid,
        replaced_range=(0, 4),
        cache_invalidated=True,
    )

    # 磁盘原始写入顺序（append-only）：先按 turn 顺序写入各项，压缩后 ph 追加末尾
    raw_items: list[ResponseItem] = [u1, a1, u2, a2, u3, a3, ph]

    # 把 raw_items 写入 JsonlMessageStore（供测试读取，同时也验证写入/读出路径）
    store = JsonlMessageStore(Path(str(threads_dir)))  # type: ignore[arg-type]
    for item in raw_items:
        await store.append(item)

    # 从 store 加载回来（验证落盘后读回与构造时一致）
    stored_items = [it async for it in await store.load_thread(tid)]
    assert len(stored_items) == len(raw_items), (
        f"落盘后读回 {len(stored_items)} 项，期望 {len(raw_items)} 项"
    )

    # 参照重建结果（纯函数，确认预期逻辑 history）
    expected_logical = reconstruct_logical_history(stored_items)
    # 预期：[ph, u3, a3]
    expected_kinds = [i.kind for i in expected_logical]
    assert expected_kinds == ["compacted", "user_message", "assistant_message"], (
        f"预期逻辑 history 为 [compacted, user_message, assistant_message]，"
        f"实得 {expected_kinds}"
    )

    # 被折叠的废弃项 id 集合（u1, a1, u2, a2）
    superseded_ids = {u1.id, a1.id, u2.id, a2.id}
    logical_ids = {i.id for i in expected_logical}
    assert superseded_ids.isdisjoint(logical_ids), (
        "逻辑 history 中不应含被折叠的废弃项 id"
    )

    # ── 构造冷 AgentEngine（注入 raw transcript → 触发 reconstruct 路径）────────
    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()  # type: ignore[arg-type]
    entry = snapshot.get("code-reviewer")
    assert entry is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    cold_client = SimClient(turns=[SimTurn(text="COLD-COMPACTED-REWIND-DONE")])
    cold_engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=cold_client,
        store=store,
        thread_id=tid,
        session_id="s_compacted_cold",
        compressors=None,
        initial_history=stored_items,  # 含被折叠废弃项 + compacted placeholder
    )

    # ── 断言 1：history_snapshot() = 重建后的逻辑 history（废弃项已折叠）────────
    snap = cold_engine.history_snapshot()
    snap_ids = {i.id for i in snap}
    snap_kinds = [i.kind for i in snap]

    # 1a. 废弃项 id 不存在于 history 中
    for sid in superseded_ids:
        assert sid not in snap_ids, (
            f"被折叠废弃项 {sid} 不应出现在冷 engine history 中"
        )
    # 1b. compacted placeholder 存在
    assert any(i.kind == "compacted" for i in snap), (
        f"compacted placeholder 应在逻辑 history 中，实得 kinds={snap_kinds}"
    )
    # 1c. post-compaction items（u3, a3）存在
    assert u3.id in snap_ids, "u3（压缩后第三轮用户消息）应在逻辑 history 中"
    assert a3.id in snap_ids, "a3（压缩后第三轮助手回复）应在逻辑 history 中"

    # ── 断言 2：rewind_nodes() 含压缩边界后 turn 的节点（post-compaction 可寻址）──
    cold_nodes = cold_engine.rewind_nodes()
    # 逻辑 history = [ph, u3, a3]，derive 推导出：turn1(含 u3,a3 这对)→ t1:it1
    # 若逻辑 history 有 1 个 user+1 个 assistant，则有至少 1 个节点
    assert cold_nodes, (
        f"rewind_nodes() 不应为空：post-compaction turn 应可寻址，"
        f"history kinds={snap_kinds}"
    )
    # 确认这些节点的 node_id 可从逻辑 history 推导（与独立 derive 一致）
    derived_ids = [n.node_id for n in derive_rewind_log(snap)]
    cold_ids = [n.node_id for n in cold_nodes]
    assert cold_ids == derived_ids, (
        f"冷 engine 节点表应与 derive_rewind_log(logical) 一致，"
        f"cold_ids={cold_ids}, derived_ids={derived_ids}"
    )

    # ── 断言 3：提交 bogus pre-compaction node_id → rewind_rejected(unknown_node) ──
    # bogus_node_id 模拟"如果压缩前的 turn 有节点会是什么 id"，在折叠后不可寻址
    bogus_node_id = "t0:it1-folded-away"  # 不在 cold_nodes 中的任意 id
    assert bogus_node_id not in cold_ids, f"{bogus_node_id} 不应是可寻址节点"

    cancel = CancellationToken(name="compacted-cold-root")
    run_task = asyncio.create_task(cold_engine.run(cancel))

    rw_sub_id = await cold_engine.submit(
        Rewind(node_id=bogus_node_id, mode="re_reason")
    )
    # 消费事件，等待 rewind_rejected 出现（engine 处于 idle 时直接处理 Rewind op）
    kinds: list[str] = []
    rejected_data: dict = {}
    async for ev in cold_engine.subscribe_all():
        if ev.submission_id != rw_sub_id:
            continue
        kinds.append(ev.msg.kind)
        if ev.msg.kind == "rewind_rejected":
            rejected_data = dict(ev.msg.data)
            break  # 拿到即可，无需等更多事件

    await cold_engine.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        run_task.cancel()

    assert "rewind_rejected" in kinds, (
        f"bogus node_id 应产生 rewind_rejected，实际 kinds={kinds}"
    )
    assert rejected_data.get("reason") == "unknown_node", (
        f"rewind_rejected reason 应为 'unknown_node'，实得 {rejected_data}"
    )
    assert rejected_data.get("node_id") == bogus_node_id


# ── spec §9 缺口 Test 2：resume 压缩 thread 无废弃项回归 ──────────────────────


async def test_resume_compacted_thread_no_superseded_items(
    skills_dir: object, threads_dir: object,
) -> None:
    """resume 压缩 thread 时 engine.history_snapshot() 不含废弃项（reconstruct 生效）。

    这个测试锁住 spec §9 顺带修复的 resume 隐患：若 reconstruct_logical_history
    未在 engine.__init__ 中被调用（或被跳过），resume 时 _history 会包含被折叠的
    中间废弃项，导致后续 turn 把已压缩的内容再次喂给 LLM。

    **等价合成方案**（与 Test 1 相同原因，使用合成 transcript）：
    直接手工构造含废弃项 + compacted placeholder 的 raw transcript，写入 store，
    用 AgentEngine(initial_history=stored_items) 模拟 resume 路径（这正是
    EnginePool.get_or_create(resume_thread_id=...) 内部所走的路径）。

    断言：
    - raw transcript（未经重建）含有废弃项（前置对比，证明 reconstruct 是有意义的）；
    - cold engine.history_snapshot()（经 reconstruct）不含这些废弃项；
    - engine.history_snapshot() 含 compacted placeholder（折叠后的摘要仍可见）；
    - 这两者之差证明 reconstruct 在 resume 路径上起到了清洁过滤的作用。
    """
    from pathlib import Path

    import taifeng
    from taifeng.conversation.models import (
        ResponseItem,
        assistant_message,
        compacted,
        user_message,
    )
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    tid = "thr-resume-compacted"

    # ── 构造合成 transcript（模拟多轮 + 中间压缩的磁盘布局）────────────────────
    # 热内存历史（压缩前）：[u1, a1, u2, a2, u3, a3, u4, a4]（4 轮对话）
    # 压缩后热内存：[ph, u3, a3, u4, a4]（replaced_range=(0, 4) 折叠前 4 项）
    # 磁盘 append-only：[u1, a1, u2, a2, u3, a3, u4, a4, ph]

    u1 = user_message("resume-test 轮1用户", thread_id=tid)
    a1 = assistant_message("resume-test 轮1助手", thread_id=tid, model="mock-model")
    u2 = user_message("resume-test 轮2用户", thread_id=tid)
    a2 = assistant_message("resume-test 轮2助手", thread_id=tid, model="mock-model")
    u3 = user_message("resume-test 轮3用户", thread_id=tid)
    a3 = assistant_message("resume-test 轮3助手", thread_id=tid, model="mock-model")
    u4 = user_message("resume-test 轮4用户", thread_id=tid)
    a4 = assistant_message("resume-test 轮4助手", thread_id=tid, model="mock-model")

    # placeholder：压缩 [u1, a1, u2, a2]（history 中 index 0..3）
    ph = compacted(
        "前两轮压缩摘要（resume 场景）",
        thread_id=tid,
        replaced_range=(0, 4),
        cache_invalidated=True,
    )

    # 磁盘写入顺序（append-only）
    raw_items: list[ResponseItem] = [u1, a1, u2, a2, u3, a3, u4, a4, ph]
    raw_ids = {i.id for i in raw_items}

    # 被折叠的废弃项 id（u1, a1, u2, a2 被 ph.replaced_range 覆盖）
    superseded_ids = {u1.id, a1.id, u2.id, a2.id}

    # ── 前置断言：raw transcript 确实含废弃项（证明 reconstruct 有意义）──────────
    assert superseded_ids.issubset(raw_ids), (
        "raw transcript 应含所有废弃项，否则测试本身无意义"
    )

    # 写入 store
    store = JsonlMessageStore(Path(str(threads_dir)))  # type: ignore[arg-type]
    for item in raw_items:
        await store.append(item)

    # 从 store 读回（模拟 resume 时 pool 从磁盘加载 transcript 的过程）
    stored_items = [it async for it in await store.load_thread(tid)]
    stored_ids = {i.id for i in stored_items}

    # 再次确认：磁盘上的 stored_items 仍含废弃项
    assert superseded_ids.issubset(stored_ids), (
        "从 store 读回的 stored_items 应仍含废弃项（pre-reconstruct 对比基准）"
    )

    # ── 构造冷 engine（等价于 resume 路径）────────────────────────────────────
    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()  # type: ignore[arg-type]
    entry = snapshot.get("code-reviewer")
    assert entry is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    cold_engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=SimClient(turns=[]),  # resume 不触发 turn，无需 LLM 回答
        store=store,
        thread_id=tid,
        session_id="s_resume_compacted",
        compressors=None,
        initial_history=stored_items,  # 含废弃项的 raw transcript → reconstruct 路径
    )

    # ── 核心断言：engine.history_snapshot() 不含废弃项 ────────────────────────
    snap = cold_engine.history_snapshot()
    snap_ids = {i.id for i in snap}
    snap_kinds = [i.kind for i in snap]

    # A. 废弃项 id 不在 history 中（reconstruct 已折叠）
    for sid in superseded_ids:
        assert sid not in snap_ids, (
            f"废弃项 {sid} 不应出现在 resume 后的 engine history 中"
            f"（reconstruct 应已折叠），实际 history kinds={snap_kinds}"
        )

    # B. compacted placeholder 存在（折叠后的摘要仍可见）
    assert any(i.kind == "compacted" for i in snap), (
        f"compacted placeholder 应在逻辑 history 中，实得 kinds={snap_kinds}"
    )

    # C. post-compaction items（u3, a3, u4, a4）均存在
    for item, label in [(u3, "u3"), (a3, "a3"), (u4, "u4"), (a4, "a4")]:
        assert item.id in snap_ids, (
            f"{label}（压缩边界后的 item）应在逻辑 history 中"
        )

    # D. 对比量化：raw 有废弃项而 snap 没有，长度差 = len(superseded_ids)=4
    assert len(snap) == len(stored_items) - len(superseded_ids), (
        f"逻辑 history 长度应为 {len(stored_items) - len(superseded_ids)}"
        f"（raw {len(stored_items)} - {len(superseded_ids)} 折叠项），"
        f"实得 {len(snap)}"
    )

    # E. rewind_nodes() 可由逻辑 history 推导（不为空，post-compaction turns 可寻址）
    cold_nodes = cold_engine.rewind_nodes()
    assert cold_nodes, (
        f"resume 后 rewind_nodes() 不应为空，history kinds={snap_kinds}"
    )


# ── 已知边界（ADR 0016 决策三 / spec §7）：冷 rewind 以【构造期】entry skill 为锚点 ──


class _EntrySkillEchoSource:
    """动态指令源：把每次 fetch 时 ctx.entry_skill_id 原样回显进指令文本。

    用于白盒观测「惰性 resolve 实际锚定了哪个 entry_skill_id」。指令层本身不落
    transcript（resolve 在 turn 时注入 prompt、不持久化），因此冷 engine 无从得知
    历史 turn 当初用的是哪个 entry skill —— 这正是被锁定的边界来源。

    Attributes:
        seen_entry_ids: 按调用序记录每次 fetch 收到的 entry_skill_id（供断言）。
    """

    def __init__(self) -> None:
        self.seen_entry_ids: list[str] = []

    async def fetch(self, ctx: object) -> str | None:
        """回显 entry_skill_id；ctx 为 InstructionContext（用 object 避免顶层 import）。"""
        entry_id = ctx.entry_skill_id  # type: ignore[attr-defined]
        self.seen_entry_ids.append(entry_id)
        return f"ENTRY_ECHO::{entry_id}"


def _write_entry_skill(skills_root: object, skill_id: str) -> None:
    """在 skills 目录写一个 entry-eligible composite skill（child=style-checker）。"""
    from pathlib import Path

    body = (
        "---\n"
        f"name: {skill_id}\n"
        f"description: 审查专家 {skill_id}\n"
        "version: 1.0.0\n"
        "type: composite\n"
        "entry: true\n"
        "model: mock-model\n"
        "child_skills: [style-checker]\n"
        "tool_names: []\n"
        "max_call_depth: 3\n"
        "---\n"
        f"# 审查专家 {skill_id}\n"
        f"你是审查专家 {skill_id}。\n"
    )
    d = Path(str(skills_root)) / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


async def test_cold_rewind_uses_construction_entry_skill_not_history_origin(
    tmp_path: object, threads_dir: object,
) -> None:
    """锁定已知边界：冷 rewind 的惰性指令 resolve 以【构造期】entry skill 为锚点。

    场景：同一条 thread，热态在 entry skill `reviewer-a` 下产出历史；进程重启后
    用 entry skill `reviewer-b` 冷加载同一 thread 并 rewind 历史节点。

    ADR 0016 决策三 / engine._handle_rewind §8a 注释明确：冷 rewind 不还原历史
    turn 当初使用的不同 entry skill 的指令层，统一以构造期 entry skill（此处
    reviewer-b）为锚点 —— v1 范围外的已知限制。

    本测试是**特征化（characterization）测试**，锁定当前行为：
        - 热路径 resolve 收到的 entry_skill_id == "reviewer-a"；
        - 冷 rewind 惰性 resolve 收到的 entry_skill_id == "reviewer-b"（构造期），
          **绝不**是历史来源的 "reviewer-a"。

    若日后实现「按 turn 还原历史 entry skill」，本测试将失败，从而强制实现者
    显式更新此处与 ADR 0016 文档化的边界（而非悄悄改变行为）。
    """
    import asyncio
    from pathlib import Path

    import taifeng
    from taifeng.conversation.transcript import JsonlMessageStore
    from taifeng.instructions.types import InstructionLayer
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.submission import Rewind
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.builtins import (
        make_call_skill_tool,
        make_read_skill_tool,
        make_run_script_tool,
    )
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    # ── 步骤 0：构造含两个 entry skill（reviewer-a / reviewer-b）的 skills 目录 ──
    skills_dir = Path(str(tmp_path)) / "two_entry_skills"
    sc = skills_dir / "style-checker"
    sc.mkdir(parents=True)
    (sc / "SKILL.md").write_text(
        "---\nname: style-checker\ndescription: 代码风格审查\n"
        "version: 1.0.0\ntype: atomic\n---\n# 风格审查\n按规范审查 diff。\n",
        encoding="utf-8",
    )
    _write_entry_skill(skills_dir, "reviewer-a")
    _write_entry_skill(skills_dir, "reviewer-b")

    # ── 步骤 1：热态在 reviewer-a 下跑一个单圈 turn，持久化 transcript ──────────
    hot_source = _EntrySkillEchoSource()
    hot_client = SimClient(turns=[SimTurn(text="原推理收尾")])  # 单圈，无工具 → t1:it1
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,  # type: ignore[arg-type]
        threads_dir=threads_dir,  # type: ignore[arg-type]
        model_client=hot_client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(name="echo", source=hot_source, scope="session", priority=10),
        ],
    )
    hot_engine = await pool.get_or_create(
        session_id="s_entry_hot", entry_skill_id="reviewer-a"
    )
    hot_sub = await hot_engine.submit(taifeng.UserMessage(text="请审查代码"))
    async for ev in hot_engine.subscribe_all():
        if ev.submission_id != hot_sub:
            continue
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break
    for _ in range(100):
        if hot_engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)
    thread_id = hot_engine.thread_id
    await pool.close()

    # 热路径 resolve 确实以 reviewer-a 为锚点（边界对照基准）
    assert "reviewer-a" in hot_source.seen_entry_ids, (
        f"热 turn 的指令 resolve 应收到 entry_skill_id='reviewer-a'，"
        f"实得 {hot_source.seen_entry_ids}"
    )
    assert "reviewer-b" not in hot_source.seen_entry_ids

    # ── 步骤 2：物化 transcript ───────────────────────────────────────────────
    cold_store = JsonlMessageStore(Path(str(threads_dir)))  # type: ignore[arg-type]
    transcript = [it async for it in await cold_store.load_thread(thread_id)]
    assert transcript, "transcript 不应为空"

    # ── 步骤 3：用 reviewer-b 冷加载同一 thread（不跑任何 turn）─────────────────
    snapshot = (await FilesystemSkillRegistry.load(skills_dir)).snapshot()  # type: ignore[arg-type]
    entry_b = snapshot.get("reviewer-b")
    assert entry_b is not None

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())
    tools.register(make_run_script_tool())

    cold_source = _EntrySkillEchoSource()
    cold_client = SimClient(turns=[SimTurn(text="COLD-ENTRY-REWIND-DONE")])
    cold_engine = taifeng.AgentEngine(
        entry_skill=entry_b,
        skill_snapshot=snapshot,
        tool_runtime=ToolCallRuntime(tools),
        model_client=cold_client,
        store=cold_store,
        thread_id=thread_id,
        session_id="s_entry_cold",
        compressors=None,
        initial_history=transcript,
        instruction_layers=[
            InstructionLayer(name="echo", source=cold_source, scope="session", priority=10),
        ],
    )

    cold_nodes = cold_engine.rewind_nodes()
    assert cold_nodes, "冷 engine 节点表不应为空"

    # ── 步骤 4：冷 engine 提交 re_reason rewind，触发惰性 resolve ────────────────
    cancel = CancellationToken(name="cold-entry-rewind-root")
    run_task = asyncio.create_task(cold_engine.run(cancel))
    it1 = next(n for n in cold_nodes if n.node_id == "t1:it1")
    rw_sub_id = await cold_engine.submit(Rewind(node_id=it1.node_id, mode="re_reason"))

    kinds: list[str] = []
    text_parts: list[str] = []
    async for ev in cold_engine.subscribe_all():
        if ev.submission_id != rw_sub_id:
            continue
        kinds.append(ev.msg.kind)
        data = ev.msg.data if hasattr(ev.msg, "data") else {}
        if ev.msg.kind == "assistant_text" and data.get("delta"):
            text_parts.append(str(data["delta"]))
        if ev.msg.kind in ("turn_completed", "turn_failed") and data.get("is_root"):
            break

    await cold_engine.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        run_task.cancel()

    # ── 步骤 5：核心断言 ──────────────────────────────────────────────────────
    # 5a. rewind 流程未被破坏
    assert "turn_rewound" in kinds, f"应发出 turn_rewound，实得 kinds={kinds}"
    assert "COLD-ENTRY-REWIND-DONE" in "".join(text_parts)

    # 5b. 边界锁定：冷 rewind 惰性 resolve 锚定【构造期】reviewer-b，绝非历史的 reviewer-a
    assert cold_source.seen_entry_ids, (
        "冷 rewind 应触发惰性 resolve（fetch 至少被调用一次）"
    )
    assert "reviewer-b" in cold_source.seen_entry_ids, (
        f"冷 rewind resolve 应锚定构造期 entry_skill_id='reviewer-b'，"
        f"实得 {cold_source.seen_entry_ids}"
    )
    assert "reviewer-a" not in cold_source.seen_entry_ids, (
        "已知边界：冷 rewind 不还原历史 turn 的 entry skill 'reviewer-a'；"
        f"若此断言失败，说明边界行为已变更，需同步更新 ADR 0016，"
        f"实得 {cold_source.seen_entry_ids}"
    )

    # 5c. 回显文本进入 _last_resolved，佐证锚点为 reviewer-b
    cold_texts = [r.text for r in cold_engine._last_resolved]  # type: ignore[attr-defined]
    assert any("ENTRY_ECHO::reviewer-b" in t for t in cold_texts), (
        f"_last_resolved 应含 reviewer-b 的回显文本，实得 {cold_texts}"
    )
