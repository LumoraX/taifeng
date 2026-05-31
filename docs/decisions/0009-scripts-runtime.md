# ADR 0009: SKILL.md scripts 运行时（ScriptExecutor + run_script）

- 状态：Accepted
- 日期：2026-05-24
- Related: spec `docs/architecture/capabilities/script-execution.md`

## 背景

ADR 0006（统一 Skill 抽象）落地时，loader (`src/taifeng/skill/loader.py`) 会扫描 `skills/<id>/scripts/*.{sh,py,js,ts}` 并把路径写到 `SkillDefinition.scripts: tuple[str, ...]`。但运行时**完全没有执行入口** —— LLM 调不到、`shell_exec` 工具是通用 shell（不走 skill 声明），结果是：

- SKILL.md 中 `scripts:` 字段成为"装饰品"
- 业务侧若要让 LLM 跑 skill 内脚本，只能拼接 `shell_exec("./skills/x/scripts/y.sh")`，绕过权限粒度
- 缺审计：脚本执行不出现在 `EventMsg` 中，违反 R3 红线

## 决策

引入**独立的 script 运行时子系统**，与 tool / skill dispatch 平行：

### 1. 数据契约（`src/taifeng/skill/scripts/types.py`）

- `ScriptDescriptor`（frozen）：`skill_id / name / path / language / args_schema / timeout_seconds / max_output_bytes / description`
- `ScriptInvocation`：`descriptor + args + CancellationToken`
- `ScriptResult`：`exit_code / stdout / stderr / duration_ms / truncated / is_timeout / killed`
- `ScriptExecutionError(LLMError)`：仅系统级失败（spawn 不到 / 容器 down）抛出；正常退出码非 0 走 ScriptResult

### 2. 执行器协议（`src/taifeng/skill/scripts/executor.py`）

```python
@runtime_checkable
class ScriptExecutor(Protocol):
    async def execute(self, inv: ScriptInvocation) -> ScriptResult: ...
```

**业务侧通过 `EnginePool(script_executors={...})` 注入**自定义执行器（容器 / 沙箱 / 远程 RPC）。src 内仅提供 `ShellScriptExecutor` / `PythonScriptExecutor` 两个默认 subprocess 实现，跑在宿主进程内。

### 3. 内置工具 `run_script`（`src/taifeng/tool/builtins/run_script.py`）

输入：`{skill_id, script_name, args}`。9 阶段流程：

1. skill 查找
2. script_name ∈ skill.scripts 查找
3. args_schema 校验（required 字段）
4. executor 查找
5. `pre_script_use` hook 链（支持 `args_override` metadata）
6. `PermissionPolicy.check(scope='script_exec')`
7. executor.execute
8. `post_script_use` hook（仅审计；不能否决）
9. 打包 ToolResult + 5 类 EventMsg

### 4. SKILL.md 显式声明（loader 扩展）

```yaml
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: shell
    timeout_seconds: 30
    description: 把原始 CSV 标准化
    args_schema:
      type: object
      properties: {input: {type: string}}
      required: [input]
```

显式 + 隐式发现并存；同名以显式为准。`path` 必须落在 skill 目录下（loader 拒绝越权）。

## 备选方案（已拒绝）

| 方案 | 拒绝理由 |
| --- | --- |
| **直接 exec 在主进程** | 业务代码污染 agent 进程；任何 SIGKILL 会拖死整个 worker；违反 R4 取消传播 |
| **复用 `shell_exec` 工具** | 颗粒度太粗（PermissionPolicy 只能按命令字符串匹配），且绕过 args_schema 校验 |
| **内置 namespace / cgroup / seccomp 沙箱** | OS 强依赖（Linux only）；增加 src/ 复杂度；与"业务侧 wrap"原则冲突 |
| **支持多语言 runtime（Node/Deno/Ruby）** | 各 runtime 启动开销差异大；改成业务侧 `ScriptExecutor` 后零负担 |

## 安全模型

| 控制 | 实现 | 说明 |
| --- | --- | --- |
| 防 shell injection | `asyncio.create_subprocess_exec(*argv)` —— 不拼接字符串 | args 永远是独立 argv 元素 |
| 防 secret 泄漏 | env 默认白名单 `PATH / HOME / LANG / LC_ALL` | 业务 `OPENAI_API_KEY` 等不传递到子进程 |
| 防 stdin 注入 | `stdin=DEVNULL` | LLM 对话内容无法流入子进程 |
| 防 fd 泄漏 | `close_fds=True` | 子进程仅可见 stdout/stderr/stdin |
| timeout 强制 | `asyncio.wait` + process group SIGTERM(1s) → SIGKILL | grandchild（如 `sleep`）一起 kill |
| cancel 传播 | `inv.cancel.wait_cancelled()` 加入 race | 父 turn cancel → 立刻 kill |
| 输出截断 | per-stream `max_output_bytes` + 设 `truncated=True` | 防止 LLM context 被恶意脚本灌爆 |

### 为什么不内置 sandbox

OS 沙箱（namespace / cgroup / seccomp）和容器 runtime（Docker / Firecracker）的选型是**业务决策**：单机部署不需要、k8s 已有 PSP、自建 PaaS 已有 Falco。在 src/ 内捆绑 sandbox 实现 = 强加部署模型，违反 R1 业务零侵入。

业务侧需要 sandbox 时实现 `ScriptExecutor` 协议即可：

```python
class FirejailScriptExecutor:
    async def execute(self, inv: ScriptInvocation) -> ScriptResult:
        # 在 firejail / docker run / 内部 sandbox-svc 中执行
        ...

pool = EnginePool.create(
    ...,
    script_executors={"shell": FirejailScriptExecutor()},
)
```

## env 白名单的 os.getenv 豁免

CLAUDE.md §实现约束声明"src/ 内禁止 `os.getenv`"。`ShellScriptExecutor._default_safe_env()` 调用 `os.environ.get("PATH"/"HOME"/"LANG")` —— 这不是**业务配置**，而是 subprocess 启动所必需的 system-level env 准备。

业务配置（API key、tenant 配置）必须通过依赖注入；system env（PATH 等）属于 OS 接口，无可替代。本豁免在 ADR 0009（本文档）中显式记录，pre-commit / code review 时不视为违规。

## 与其他 capability 的关系

| 对比对象 | 区别 |
| --- | --- |
| `skill-dispatch` (`call_skill`) | call_skill 是 skill → skill 的 LLM 接力；run_script 是 skill → script 的确定性副作用执行 |
| `shell_exec` 内置工具 | shell_exec 是通用 shell 入口；run_script 限定到 skill 内声明的 script，权限粒度更细。生产建议 `shell_exec` 默认 deny、`run_script` 收口 |
| `Tool` 抽象 | `ScriptExecutor` 是 Tool 之下的子抽象，挂在 `run_script` 这一个 Tool 上。业务自定义 ScriptExecutor 不需注册 Tool，只需注入 `script_executors[language]` |

## R1-R5 红线影响

- **R1 业务零侵入**：src 内仅 stdlib subprocess + anyio。`script_executors` 字典通过 EnginePool 注入，无 宿主业务 业务 import
- **R2 cache 友好**：脚本执行**不污染 history mid-turn**（结果以 ToolResult 进入 history 一次；不需要 head 压缩）
- **R3 可观测**：5 类新 EventMsg（`script_execution_started / _completed / _failed / _timeout / _killed`）通过 `EngineLog` 渠道发出
- **R4 可取消**：cancel token race + process group kill，端到端测试覆盖（`test_subprocess_killed_on_cancel_e2e`）
- **R5 可 resume**：script 结果以 `function_call_output` 落 store；JSONL replay 能复现 LLM 视角的全部上下文（脚本副作用本身需业务自己 idempotent）

## Non-goals

- ❌ 跨 skill 调 script（必须显式 `skill_id=自己`）
- ❌ script ↔ script 编排（DAG / pipeline）
- ❌ 依赖管理（venv / npm install）
- ❌ LLM 向 script 注入 stdin
- ❌ shebang 读取（interpreter 必须通过 `descriptor.language` 显式声明）
- ❌ Windows v1 支持（信号语义差异需单独适配）
