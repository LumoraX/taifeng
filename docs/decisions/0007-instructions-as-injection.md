# ADR 0007: Instructions 走业务侧注入协议 —— 不读文件 / 不读环境 / 不假设 cwd

- 状态：Accepted
- 日期：2026-05-24
- 关联 change：`docs/architecture/capabilities/instructions-injection.md`

## 背景

业务方有真实诉求把"系统级策略文本"注入到 LLM system prompt 里：

- 租户级合规要求（"本租户禁止讨论 X 类话题"）
- A/B 实验提示词（不同版本接不同 system prompt）
- 动态人格切换（每个 session 选不同的"医生人格 / 助手人格"）
- 运行时审稿风格切换（"严格 / 宽松"按用户选择立即生效）

参照实现（codex / claw-code）是 CLI agent，可以理直气壮地：

- 从 cwd 沿路径串联 `AGENTS.md` + `~/.codex/AGENTS.md` + `AGENTS.override.md`
- 读环境变量、监听 `AGENTS.override.md` 文件变更
- 进程级单例

这套范式**完全不适合 taifeng**：

| CLI 假设 | taifeng 现实 |
| --- | --- |
| 进程有稳定 cwd | web worker 的 cwd 与会话无关 |
| 文件系统可读 / 可监听 | 容器化、只读 fs、多副本部署，watcher 不可靠 |
| 用户 home 目录概念 | 多租户后端没有"用户 home"语义 |
| 环境变量可读 | R1 红线：`src/` 内禁止 `os.getenv` |
| 进程级单例 | 同进程可能多 EnginePool 实例 |

## 决策

**把"业务侧需要把 system-level 策略文本注入到 LLM prompt"抽象成协议 + 三档 scope**：

1. **`InstructionSource(Protocol)`**：业务侧实现 `async def fetch(ctx) -> str | None`
2. **`InstructionLayer(scope=...)`** 三档生命周期：
   - `engine` —— `EnginePool.create` 时一次性 resolve，进程内复用
   - `session` —— 每个 `AgentEngine` 实例缓存；`UpdateInstructions` 可热更
   - `turn` —— 每次 turn 启动前 resolve（受 cache_ttl 控制）
3. **装配位置**：entry_skill body **之前**，按 priority 升序输出多个 `<system_instructions>` 块
4. **运行时热更**：`Op.UpdateInstructions(layer_name, new_source)`
5. **外部读取**：`engine.instructions_snapshot() -> list[ResolvedInstruction]`
6. **可观测**：每次 fetch / cache hit / 热更 / 失败均通过 EventMsg + TelemetrySink

## 备选方案

### 备选 A（被拒）：内置 `FileInstructionSource`

```python
# 备选：库内提供常用 source 实现
InstructionLayer(source=FileInstructionSource("/etc/policy.md"), scope="engine")
```

**拒绝理由**：

- 引入 `Path` + 文件读取 → 破坏 R1 业务零侵入
- web 后端部署里"指令文件路径"是业务配置，不该写死在库里
- 容器化部署中文件路径假设经常出问题
- 同一段文本可能要从 DB / S3 / 配置中心读 —— 库不应该假设来源

业务侧实现一个 7 行的 `FileInstructionSource` 比把它写进库里简单得多，且与业务的配置体系一致。

### 备选 B（被拒）：通过 URL scheme 路由（`file://` / `http://` / `env://`）

**拒绝理由**：

- overengineering —— scheme 解析比业务自己写一个 source 还复杂
- 引入额外字符串协议层，类型不安全
- 不解决"业务侧如何告诉库要哪段文本"的核心问题

### 备选 C（被拒）：只有一档 scope，所有指令每 turn 都拉

**拒绝理由**：

- 浪费业务侧 IO 调用
- 静态策略文本每 turn 都重传 → prompt cache miss 率 100% → 严重违反 R2
- 业务侧没有空间表达"这段是永久不变的全局策略，那段是动态的 trace_id"

### 备选 D（被拒）：fetch 失败 silent fallback 到空字符串

**拒绝理由**：

- 违反"禁止 silent fallback"原则
- 指令文本可能含合规约束（"不讨论 X"）→ fetch 失败回退到空 = 合规约束消失 = 严重违规
- fail-fast 让业务侧明确感知（spec D5）

### 备选 E（被拒）：fetch 内部可以发起 HITL 询问

**拒绝理由**：

- 业务侧出现两套询问机制（fetch 内 vs PermissionPrompter）→ 心智混乱
- 维护两套用户决策记忆 → 状态漂移
- HITL prompter 慢会卡死 turn

**严格分层**：
- **数据级权限**（"这个租户能不能读这段指令"）→ `InstructionSource.fetch` 内部完成
- **动作级权限**（"LLM 想跑这个 tool / call_skill"）→ `PermissionPolicy + PermissionPrompter`

两套机制**严格不重叠**（spec D6）。

## 影响

### 业务侧需要做的

- 实现 `InstructionSource` 协议（或直接传 `str` 静态文本）
- 自行读 DB / 配置中心 / 文件 / 环境变量
- 在 `fetch` 内部完成数据级权限校验（raise 或返回 None）
- **禁止** 在 `fetch` 内发起 HITL 询问（弹窗 / SSE / Slack bot）

### 与 R1–R5 红线的关系

| 红线 | 影响 | 落实 |
| --- | --- | --- |
| R1 业务零侵入 | 强化 —— InstructionSource 是协议，业务侧自决数据源 | `src/` 不写 file IO / env 读取 |
| R2 Cache 友好 | 中性 —— scope=engine 静态走 prefix cache；scope=turn 动态可能破 cache | snapshot 暴露 `cache_volatile`，业务可见 |
| R3 可观测 | 强化 —— 5 个新 EventMsg：fetched / cache_hit / updated / fetch_failed / update_rejected | TelemetrySink 协议 |
| R4 可取消 | 强化 —— fetch 接收 `CancellationToken`；turn cancel 时 in-flight fetch 立即中止 | resolver 通过 `ctx.cancel` 透传 |
| R5 可 resume | 中性 —— 指令文本不入 JSONL；engine 重建时业务侧重新提供 layers | resume 路径不依赖指令历史 |

### 公共 API 变更

- `AgentEngine.__init__` 新增可选 `instruction_layers / session_id` 参数
- `EnginePool.create` 新增可选 `instruction_layers` 参数
- `Op` 新增 `UpdateInstructions(layer_name, new_source)` variant
- `AgentEngine.instructions_snapshot()` 新增方法
- `AgentEngine.warmup_engine_scope()` 新增方法（pool 自动调用，业务侧通常无需直接调）

不传 `instruction_layers` 时行为完全等同改前（向后兼容）。

## 参考

- spec：`docs/architecture/capabilities/instructions-injection.md`
- 设计文档：`docs/architecture/capabilities/instructions-injection.md`
- 实现：`src/taifeng/instructions/`
- 测试：`tests/test_instructions.py` / `tests/test_prompt_instructions.py`
- 示例：`examples/basic/instructions_basic.py`
