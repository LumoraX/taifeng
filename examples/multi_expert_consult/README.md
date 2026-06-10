# multi_expert_consult —— 并发多专家 + 错峰 HITL + 联合会诊聚合

演示内核 **detached-spawn** 能力的完整闭环：主编排在一个 turn 内**并发分离发起**多个专科专家，每个专家在**独立 child thread** 上各自**错峰 HITL**、各自完成，最终由 **join-barrier** 收齐自动起「联合会诊」聚合。

```
用户主诉（血压高 + 体重涨）
  → orchestrator 一个 turn：
       ├─ spawn_skill(cardio-expert)      ┐ 立即返回句柄、后台 child thread 独立推进
       ├─ spawn_skill(metabolic-expert)   ┘ 不阻塞编排 turn
       └─ await_skills([两句柄], then=joint-consult)   登记 join-barrier
  → 错峰 HITL：
       cardio  挂起 → Resume(cardio child thread) → 完成
       （过一会）metabolic 才挂起 → Resume(metabolic child thread) → 完成
  → 两句柄全终态 → join-barrier 自动触发 → joint-consult 聚合 turn → 最终会诊报告
```

## 运行（SimClient，无需 API key）

```bash
cd taifeng
PYTHONPATH=src uv run python examples/multi_expert_consult/demo.py
```

输出是一条清晰的事件时间线：`spawn_started ×2` → `cardio spawn_suspended → spawn_completed` → `metabolic spawn_suspended → spawn_completed` → `join_barrier_fired` → 联合会诊报告。

## 结构

| 文件 | 角色 |
| --- | --- |
| `skills/orchestrator/SKILL.md` | composite **entry**：child_skills 含两个专家 + joint-consult；tool_names 含 `spawn_skill / await_skills / join_skill / kill_skill` |
| `skills/cardio-expert/SKILL.md` | composite **非 entry**：tool_names=[request_user_input]，先 HITL 问诊再下结论 |
| `skills/metabolic-expert/SKILL.md` | 同上，独立节奏 |
| `skills/joint-consult/SKILL.md` | atomic 聚合器：种子参数带**全部专家句柄终态**（含取消 / 失败，不静默丢），综合成最终报告 |
| `demo.py` | 用 `RoutingSimClient` 按 body 标记路由，串行 staggered resume 两个专家，订阅 `subscribe_all` 打印时间线 |

> 本 demo 专家 / 聚合器用**非 entry**（一种设计选择，非硬性要求）。注意 spawn 与 call_skill 不同：**spawn 目标可为 entry skill**（spawn 把目标作为独立根分离发起，`DispatchPolicy.check(allow_entry_target=True)` 跳过「不可调 entry」门，与 `set_join_barrier` 的 then_skill 同理）。两者仍要求 target 在 caller 白名单内。

## 三种并发姿态对照

同样是「让多个专家并行干活」，taifeng 提供三种范式，**心智模型与收口方式不同**：

| 姿态 | 谁决定并发 | 收口方式 | HITL | 看哪个 demo |
| --- | --- | --- | --- | --- |
| **等待收齐**（concurrent call_skill） | LLM 临场把多个 `call_skill` 放进同一条消息 | **同步阻塞**等整批回流再续推 | 整批被一个 barrier 同步收口 | [`concurrent_fanout/`](../concurrent_fanout/) |
| **错峰各自发起**（detached spawn，本 demo） | LLM 调 `spawn_skill` 或业务调 `engine.spawn_skill` | 每个 spawn 立即返回句柄、**非阻塞**；`await_skills` 登记 join-barrier 异步收齐 | 每个专家在独立 child thread 上**各自错峰** HITL，互不耦合 | **`multi_expert_consult/`** |
| **业务确定性编排**（step_pipeline） | 业务侧代码用 SKILL.md `orchestration:` 或显式 step 序列驱动 | 业务层控制收口，确定性、可重放 | 业务层按需穿插 | [`step_pipeline/`](../step_pipeline/) |

一句话区分：**等待收齐 = 一批人开会等齐了才散**；**错峰各自发起 = 每个人按自己节奏跑、跑完登记，全到齐了系统自动开总结会**；**业务编排 = 流程由业务代码写死、可重放**。

## 关系图

- [`concurrent_fanout/`](../concurrent_fanout/)：批量同步 fan-out + 同步 HITL —— 本 demo 的「同步」对照面。
- [`turn_rewind/`](../turn_rewind/)：自治链一键跑完 + 回退任意节点重跑 —— 与本 demo 同样绕开 entry/call_skill 互斥（子 skill 全程非 entry）。

## 契约 / 决策

- 能力契约：[`docs/architecture/capabilities/detached-spawn.md`](../../docs/architecture/capabilities/detached-spawn.md)
- 决策记录：[ADR 0015 detached-skill-spawn](../../docs/decisions/0015-detached-skill-spawn.md)
