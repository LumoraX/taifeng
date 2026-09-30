"""EventMsg 的生命周期事件：挂起 / 恢复、turn 终态、指令、存储、rewind、spawn、barrier。

从 ``loop/event.py`` 原样搬出（W7.1 拆文件，零行为变更）；``MsgKind`` / ``Msg`` 联合与 ``EventMsg``
仍在 ``event.py``，所有事件类经 ``event.py`` 原名再导出。
"""

from __future__ import annotations

from typing import Literal

from taifeng.loop.event_base import _Msg


class TurnSuspended(_Msg):
    """turn 在中途挂起，实例可释放；业务凭 thread_id + record_id 提交 Resume 续跑。

    data = {
        "thread_id": str,
        "record_id": str,
        "pending": list[dict],     # 每项 {request_id, reason, payload_schema,
                                   #        related_call_id, detail}
        "cache_invalidated": bool, # tier-2 跨进程 resume 必须为 True
        "expires_at": int | None,  # suspension-ttl 到期时刻(None=永不过期)
        "usage": dict,             # 仅本段(挂起前)调用用量;续跑段另报,见 suspend-resume 契约
    }
    """

    kind: Literal["turn_suspended"] = "turn_suspended"


class SuspensionResolved(_Msg):
    """Resume 成功配对，turn 续跑。

    data = {"record_id": str, "request_ids": list[str]}
    """

    kind: Literal["suspension_resolved"] = "suspension_resolved"


class SuspensionPartiallyResolved(_Msg):
    """多 pending record 的部分核销(multi-pending-partial-resume)。

    record 含多个 pending(如 parallel 批多子同时挂起)时,Resume 按 request 级
    核销:本事件表示本次只结算了一部分,record 仍活跃、父 turn 不续跑——直到
    全部 pending 核销才落整体 resolved-marker + emit suspension_resolved + 续跑。

    data = {"record_id": str, "thread_id": str,
            "resolved_request_ids": list[str], "remaining_request_ids": list[str]}
    """

    kind: Literal["suspension_partially_resolved"] = "suspension_partially_resolved"


class SuspensionResolveRejected(_Msg):
    """Resume 被拒（resolution 不全 / 多余、record 已消费、payload 不符 schema 等）。

    data = {"reason": str, "record_id": str | None, "detail": dict}
    """

    kind: Literal["suspension_resolve_rejected"] = "suspension_resolve_rejected"


class SuspensionExpired(_Msg):
    """挂起 record 到期，内核自动裁决（suspension-ttl，R3 可观测）。

    随后的续跑 / 终态沿用既有 resume 事件族（suspension_resolved / turn_* /
    spawn_*），record_id 贯通可归因「挂起为何自行动作」。

    data = {"record_id": str, "thread_id": str, "on_expire": str,
            "reasons": list[str]}   # 该 record 各 pending 的 reason
    """

    kind: Literal["suspension_expired"] = "suspension_expired"


class ThreadResumed(_Msg):
    """EnginePool 从已有 thread_id 恢复了 engine —— history 已注入。

    business 侧可订阅本事件获悉哪些 session 走了 resume 路径；与新建 thread
    （无对应事件）区分开。emit 时机：pool.get_or_create 内部 engine.run 启动
    之后；订阅者若在此之前 attach 即可消费，否则会丢（既有 emit 语义）。
    """

    kind: Literal["thread_resumed"] = "thread_resumed"
    """data = {
        "thread_id": str,
        "item_count": int,
        "entry_skill_id_at_resume": str,
        "entry_skill_id_recorded": str | None,
        "recovered_unknown_call_ids": list[str],   # 结局仍未知（交人 / report 回填）
        "recovered_tool_calls": list[{"call_id", "name", "disposition"}],
            # tool-crash-reconciliation：崩溃遗留悬空调用的逐条处置；disposition ∈
            # safe_to_retry / reconciled / awaiting_operator / reported_unknown
    }"""


class TurnCompleted(_Msg):
    kind: Literal["turn_completed"] = "turn_completed"
    """data = {"iterations": int, "duration_ms": int, "usage": dict,
              "subtree_usage": dict, "thread_id": str, "skill_id": str,
              "end_reason": str, "success": bool, "is_root": bool}

    ``usage`` 是本 turn 自身采样用量；``subtree_usage`` 另含阻塞式 call_skill 子树
    （usage-tree-accounting，ADR 0044）。

    ``is_root`` —— 本 turn 是否是根 turn（从 Engine 直接派发，不是 ``call_skill``
    派发出来的子 turn）。订阅 ``engine.subscribe(submission_id)`` 的业务桥接层
    应当只在 ``is_root=True`` 时认为本 submission 已结束；子 turn 的 completed
    仍在父 turn 的执行窗口内，**不应**触发桥接层提前退出。

    向后兼容：旧事件流不含此字段；消费方 ``data.get("is_root", False)`` 兜底。
    """


class TurnFailed(_Msg):
    kind: Literal["turn_failed"] = "turn_failed"
    """data = {"error": str, "kind": str, "failure_class": str,
    "suggested_action": str, "recovery": dict, "iterations": int,
    "usage": dict, "is_root": bool}

    ``failure_class`` 是 G3 的稳定分类桶（见 ``llm.errors.FailureClass``），
    供 telemetry 聚合；``suggested_action`` 为人类可读处置建议；``recovery``
    是机读的结构化恢复配方（``llm.recovery.RecoveryPlan.to_dict()``）。

    ``is_root`` 语义同 ``TurnCompleted`` —— 子 turn 失败仍属于父 turn 执行窗口内的
    中间事件，业务桥接层不应据此判定 submission 终结。
    """


class EngineLog(_Msg):
    kind: Literal["engine_log"] = "engine_log"
    """data = {"level": str, "message": str, "extra": dict}"""


class InstructionFetched(_Msg):
    """instructions-injection: 动态 source 完成一次 fetch（cache miss）。"""

    kind: Literal["instruction_fetched"] = "instruction_fetched"
    """data = {"layer_name": str, "scope": str, "duration_ms": int, "text_length": int}"""


class InstructionCacheHit(_Msg):
    """instructions-injection: 缓存命中跳过 fetch。"""

    kind: Literal["instruction_cache_hit"] = "instruction_cache_hit"
    """data = {"layer_name": str, "cache_age_seconds": float}"""


class InstructionUpdated(_Msg):
    """instructions-injection: UpdateInstructions Op 成功执行。"""

    kind: Literal["instruction_updated"] = "instruction_updated"
    """data = {"layer_name": str, "new_source_kind": str}"""


class InstructionFetchFailed(_Msg):
    """instructions-injection: InstructionFetchError 抛出前发出。"""

    kind: Literal["instruction_fetch_failed"] = "instruction_fetch_failed"
    """data = {"layer_name": str, "cause_repr": str}"""


class InstructionUpdateRejected(_Msg):
    """instructions-injection: UpdateInstructions Op 拒绝（未知 name 等）。"""

    kind: Literal["instruction_update_rejected"] = "instruction_update_rejected"
    """data = {"layer_name": str, "reason": str}"""


class Shutdown(_Msg):
    kind: Literal["shutdown"] = "shutdown"


class TranscriptSkippedCorruptLine(_Msg):
    """持久化层事件 —— JsonlMessageWriter.load_history 跳过损坏行。

    data = {"thread_id": str, "line_no": int, "cause": str}
    构造 EventMsg 时通常用 submission_id="*" 标记系统级事件（写路径可能在 turn 外触发）。
    """

    kind: Literal["transcript_skipped_corrupt_line"] = "transcript_skipped_corrupt_line"


class SqliteSchemaRebuilt(_Msg):
    """SqliteThreadDirectory 启动期 schema 版本不匹配 → drop + 从 JSONL 重建后发出。

    data = {"old_version": int, "new_version": int, "rebuilt_thread_count": int, "elapsed_ms": float}
    """

    kind: Literal["sqlite_schema_rebuilt"] = "sqlite_schema_rebuilt"


class SqliteDbCorruptRebuilt(_Msg):
    """SqliteThreadDirectory 启动期 integrity_check 失败 → rename 备份 + 重建后发出。

    data = {"backup_path": str, "rebuilt_thread_count": int}
    """

    kind: Literal["sqlite_db_corrupt_rebuilt"] = "sqlite_db_corrupt_rebuilt"


class ThreadIndexedOrphan(_Msg):
    """ThreadDirectory.list_threads 即将返回的 thread 在主存中不存在 → 跳过 + 发事件。

    data = {"thread_id": str}
    """

    kind: Literal["thread_indexed_orphan"] = "thread_indexed_orphan"


class DirectoryCursorReset(_Msg):
    """ThreadDirectory.list_threads cursor 无法解析 → 从头返回 + 发事件。

    data = {"cursor": str, "cause": str}
    """

    kind: Literal["directory_cursor_reset"] = "directory_cursor_reset"


class IndexHookFailed(_Msg):
    """IndexHook 方法抛异常 → 捕获 + 发事件，主路径不受影响。

    data = {"method": str, "thread_id": str | None, "cause": str}
    """

    kind: Literal["index_hook_failed"] = "index_hook_failed"


class IndexHookAbandoned(_Msg):
    """engine.shutdown 5s grace period 后 IndexHook task 仍未完成 → cancel + 发事件。

    data = {"method": str, "thread_id": str | None}
    """

    kind: Literal["index_hook_abandoned"] = "index_hook_abandoned"


class RebuildSkippedCorrupt(_Msg):
    """rebuild_index 扫描时遇到首行损坏 / 不可解析的 thread → 计入 error_count + 发事件。

    data = {"path": str, "cause": str}
    """

    kind: Literal["rebuild_skipped_corrupt"] = "rebuild_skipped_corrupt"


class RewindCheckpointRecorded(_Msg):
    """root turn 记下一个回访节点（iteration / dispatch）时发出。

    data = {"node_id": str, "kind": str, "iteration_index": int,
            "history_len": int, "target_id": str | None}
    """

    kind: Literal["rewind_checkpoint_recorded"] = "rewind_checkpoint_recorded"


class TurnRewound(_Msg):
    """一次 Rewind Op 成功回退到某节点并重推时发出。

    data = {"node_id": str, "node_kind": str, "mode": str,
            "cut_index": int, "drop_index": int | None, "cache_anchor": int,
            "discarded_suspension": str | None, "undo_compaction": str | None,
            "redriven": bool, "thread_id"?: str}
    - drop_index: 并行批次 retry_tool 去掉的旧结果下标；无则 None
    - discarded_suspension: 随截断一并作废的挂起 record id；turn 未挂起为 None
    - undo_compaction: 被撤销的压缩条目 id；非 compaction 节点为 None
    - redriven: 是否随后重推；``restore`` 模式为 False，此时本事件即该 submission 的终结
    - cache_anchor / undo_compaction / redriven: 仅根路径；thread_id: 仅 spawn 子 thread 路径
    """

    kind: Literal["turn_rewound"] = "turn_rewound"


class RewindRejected(_Msg):
    """一次 Rewind Op 校验失败被拒时发出（禁 silent fallback）。

    data = {"node_id": str, "reason": str}
    reason ∈ {unknown_node, no_rewindable_turn, mode_kind_mismatch, turn_suspended,
              sibling_calls_pending, nothing_to_redrive, unsupported_node_kind,
              unknown_thread, thread_running}
    """

    kind: Literal["rewind_rejected"] = "rewind_rejected"


class RewindTableRebuilt(_Msg):
    """冷加载从逻辑 history 重建 rewind 节点表后发出（R3 可观测）。

    engine.__init__ 接收 initial_history 后调用 reconstruct_logical_history +
    derive_rewind_log 重建节点表；pool resume 路径在 _rebuild_spawn_state_from_history
    之后调用 _emit_rewind_table_rebuilt 发出本事件。

    data: {"thread_id": str, "turn_count": int, "node_count": int}
    - thread_id: 当前 thread 的唯一标识
    - turn_count: 重建后 history 中累积 user_message 数（= 已跑 turn 数）
    - node_count: 重建后节点表条目数
    """

    kind: Literal["rewind_table_rebuilt"] = "rewind_table_rebuilt"


class SpawnStarted(_Msg):
    """data = {handle_id, skill_id, child_thread_id}"""

    kind: Literal["spawn_started"] = "spawn_started"


class SpawnSuspended(_Msg):
    """data = {handle_id, thread_id, record_id, pending}(= 该 child thread 的挂起)。

    ``record_id`` 与 ``turn_suspended`` 同源(子 thread 落盘挂起 record 的幂等键):
    消费方按 (handle_id, record_id) 去重 / 分轮 —— 首挂与每次二次挂起(Resume 续跑
    后再挂)各带不同 record_id(新挂起点 = 新 record);同一 record_id 重放(冷恢复 /
    部分核销后仍挂)视作同一逻辑挂起。子 thread 无挂起 record 的边界下为 None。
    """

    kind: Literal["spawn_suspended"] = "spawn_suspended"


class SpawnCompleted(_Msg):
    """data = {handle_id, result}"""

    kind: Literal["spawn_completed"] = "spawn_completed"


class SpawnFailed(_Msg):
    """data = {handle_id, error}"""

    kind: Literal["spawn_failed"] = "spawn_failed"


class SpawnCancelled(_Msg):
    """data = {handle_id}"""

    kind: Literal["spawn_cancelled"] = "spawn_cancelled"


class JoinBarrierRegistered(_Msg):
    """data = {barrier_id, handle_ids, then_skill_id}"""

    kind: Literal["join_barrier_registered"] = "join_barrier_registered"


class JoinBarrierFired(_Msg):
    """data = {barrier_id, then_thread_id}"""

    kind: Literal["join_barrier_fired"] = "join_barrier_fired"


__all__ = [
    "DirectoryCursorReset",
    "EngineLog",
    "IndexHookAbandoned",
    "IndexHookFailed",
    "InstructionCacheHit",
    "InstructionFetchFailed",
    "InstructionFetched",
    "InstructionUpdateRejected",
    "InstructionUpdated",
    "JoinBarrierFired",
    "JoinBarrierRegistered",
    "RebuildSkippedCorrupt",
    "RewindCheckpointRecorded",
    "RewindRejected",
    "RewindTableRebuilt",
    "Shutdown",
    "SpawnCancelled",
    "SpawnCompleted",
    "SpawnFailed",
    "SpawnStarted",
    "SpawnSuspended",
    "SqliteDbCorruptRebuilt",
    "SqliteSchemaRebuilt",
    "SuspensionExpired",
    "SuspensionPartiallyResolved",
    "SuspensionResolveRejected",
    "SuspensionResolved",
    "ThreadIndexedOrphan",
    "ThreadResumed",
    "TranscriptSkippedCorruptLine",
    "TurnCompleted",
    "TurnFailed",
    "TurnRewound",
    "TurnSuspended",
]
