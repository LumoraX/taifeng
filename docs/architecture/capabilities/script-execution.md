# script-execution Specification

## Purpose
TBD - created by archiving change scripts-runtime. Update Purpose after archive.
## Requirements
### Requirement: 数据契约 ScriptDescriptor / Invocation / Result / Executor

系统 SHALL 提供以下类型作为协议的输入输出：

- `ScriptDescriptor`（frozen）：`skill_id: str / name: str / path: Path / language: Literal['shell','python','custom'] / args_schema: dict / description: str / timeout_seconds: float / max_output_bytes: int = 16384`（timeout 必填无默认）
- `ScriptInvocation`（frozen）：`descriptor / args: dict / cancel: CancellationToken`（args 已通过 args_schema 校验）
- `ScriptResult`（frozen）：`exit_code: int / stdout: str / stderr: str / duration_ms: int / truncated: bool / is_timeout: bool / killed: bool`
- `ScriptExecutor(Protocol)`：`async def execute(inv: ScriptInvocation) -> ScriptResult`；SHALL NOT 在主进程 exec 业务代码；SHALL 在 `inv.cancel` 触发时尽快终止 subprocess
- `ScriptExecutionError(LLMError)`：`descriptor / cause`

#### Scenario: 顶层导入
- **WHEN** 业务侧 `from taifeng import ScriptDescriptor, ScriptExecutor, ScriptInvocation, ScriptResult, ScriptExecutionError`
- **THEN** 五个符号 SHALL 全部可用

#### Scenario: descriptor 不可变
- **WHEN** 代码尝试 `descriptor.timeout_seconds = 999`
- **THEN** SHALL raise `FrozenInstanceError`

### Requirement: Loader 显式 + 隐式发现

`FilesystemSkillRegistry.load(skills_dir)` 解析 SKILL.md 时，若 frontmatter 含 `scripts:` 字段，系统 SHALL 按显式声明构建 ScriptDescriptor 列表。若该 skill 目录下 `scripts/*.{sh,py,js,ts}` 文件未在显式声明中出现，SHALL 自动生成 ScriptDescriptor 补足（默认 `timeout_seconds=60 / args_schema={"type":"object"} / description=""`）。同名以显式声明为准。

#### Scenario: path 越权拦截
- **WHEN** frontmatter 中 `path: ../../etc/passwd`
- **THEN** SHALL raise `SkillValidationError("script_path_outside_skill_dir")`，加载失败

#### Scenario: 显式声明缺 timeout
- **WHEN** 显式 scripts 条目无 `timeout_seconds`
- **THEN** SHALL raise `SkillValidationError("script_timeout_required")`

#### Scenario: 隐式发现兜底
- **WHEN** SKILL.md 无 scripts 字段，但目录下有 `scripts/foo.sh`
- **THEN** loader SHALL 自动生成 ScriptDescriptor(name='foo.sh', timeout_seconds=60, args_schema={"type":"object"})

### Requirement: run_script 工具入口

LLM 调用 `run_script(skill_id, script_name, args)` 时，系统 SHALL 按以下顺序处理：

1. 从 `skill_snapshot` 取 target skill；不存在 → `ToolResult.error("unknown_skill")`
2. 查 `script_name ∈ skill.scripts`；不存在 → `ToolResult.error("unknown_script")`
3. 用 `args_schema` 校验 args；失败 → `ToolResult.error("invalid_args", details=...)`
4. 查 `script_executors[descriptor.language]`；找不到 → `ToolResult.error("no_executor_for_language")`
5. 运行 `pre_script_use` hook 链；任一 deny → `ToolResult.error("hook_denied", reason=...)`
6. 运行 `PermissionPolicy.check(scope='script_exec', target=f'{skill_id}/{script_name}')`；deny → `ToolResult.error("permission_denied")`
7. `executor.execute(invocation)` → ScriptResult
8. 运行 `post_script_use` hook 链（仅审计，不能否决）
9. 包成 ToolResult：`exit_code == 0` → ok；否则 → error，含 `stderr / exit_code / is_timeout / killed`

#### Scenario: 权限 deny 不执行
- **WHEN** PermissionPolicy 对 `script_exec / code-reviewer/risk_score.py` 返回 deny
- **THEN** executor.execute SHALL NOT 被调用
- **AND** post_script_use hook SHALL NOT 触发

#### Scenario: pre_script_use hook 改 args
- **WHEN** pre_script_use hook 通过 `HookDecision.metadata['args_override']` 替换 args
- **THEN** 系统 SHALL 用替换后的 args 继续后续流程

#### Scenario: 跨 skill 调用被拒
- **WHEN** skill A 的 LLM 调 `run_script(skill_id='B', script_name='x')`（B 在注册表内）
- **THEN** SHALL 返回 `ToolResult.error("unknown_script")`（除非 `ctx.extras["allow_cross_skill_script"] is True`）；校验位于 skill 查找之后、script 查找之前，policy / hook / executor MUST NOT 被触达；`current_skill` 缺失 → `config_error`；不存在的 skill_id 仍 `unknown_skill`

### Requirement: subprocess 隔离与安全

ShellScriptExecutor / PythonScriptExecutor 执行 script 时 SHALL 满足以下隔离条件（防 shell injection / 防 secret 泄漏 / 防 LLM 注入子进程输入）：

- SHALL 用 argv 数组形式 spawn（不拼接 shell 字符串），避免 injection
- SHALL 默认 env 仅含 `PATH / HOME / LANG`（业务侧可通过自定义 executor 注入更多）
- SHALL 默认 cwd = `descriptor.path.parent`（skill 目录）
- subprocess 退出前 SHALL NOT 与 taifeng 主进程共享文件描述符（除 stdout/stderr）
- subprocess 的 stdin SHALL 被关闭（防止 LLM 把对话内容注入到子进程；如需结构化输入走 argv / 临时文件）
- interpreter SHALL 由 `descriptor.language` 显式决定（shell→`/bin/sh`、python→`sys.executable`、custom→业务自定义 executor）；SHALL NOT 读取 script 文件的 shebang 行

#### Scenario: shell metacharacter 不被解析
- **WHEN** args 中含 `"; rm -rf /"`
- **THEN** 该字符串 SHALL 作为单个 argv 元素传给 script，shell 不解析

#### Scenario: env 仅白名单 + 业务 secret 不泄漏
- **WHEN** 父进程 env 含 `OPENAI_API_KEY=sk-xxx`
- **THEN** subprocess 内 `printenv OPENAI_API_KEY` SHALL 输出空（不在白名单内）

#### Scenario: stdin 关闭
- **WHEN** script 内 `read line` 试图读 stdin
- **THEN** SHALL 立即 EOF（stdin 已关闭）

### Requirement: timeout 强制

subprocess 运行时间超过 `descriptor.timeout_seconds` 时，系统 SHALL 强制终止子进程并返回带 timeout 标记的 ScriptResult：

- 系统 SHALL 发送 SIGTERM
- 1 秒内未退出 SHALL 发送 SIGKILL
- SHALL 返回 `ScriptResult(is_timeout=True, killed=True, exit_code=-1)`
- SHALL 发 EventMsg `script_execution_timeout`（含 `skill_id / script_name / elapsed_ms`）

#### Scenario: 脚本忽略 SIGTERM 被 SIGKILL
- **WHEN** script trap SIGTERM 死循环，descriptor.timeout_seconds=2
- **THEN** 总用时 SHALL ≤ 3.5s（timeout 2s + SIGTERM grace 1s + 调度松弛 0.5s）
- **AND** ScriptResult.killed SHALL = True

### Requirement: 取消传播

`inv.cancel` 被触发（父 turn cancel）时，系统 SHALL 立即终止子进程并返回带 killed 标记的 ScriptResult：

- 系统 SHALL 立即 SIGTERM subprocess
- 1 秒内未退出 SHALL SIGKILL
- SHALL 返回 `ScriptResult(killed=True, is_timeout=False, exit_code=-1)`
- SHALL 发 EventMsg `script_execution_killed`

#### Scenario: cancel 在 spawn 前
- **WHEN** cancel 在 executor.execute 进入 spawn 之前已触发
- **THEN** SHALL 不 spawn subprocess，直接 raise `anyio.get_cancelled_exc_class()`

#### Scenario: cancel 在 subprocess 运行中
- **WHEN** subprocess 正跑 `sleep 60`，500ms 后 cancel
- **THEN** SHALL 1.5s 内进程已退出；ScriptResult.killed SHALL = True

### Requirement: 输出截断

stdout 或 stderr 累计字节超过 `descriptor.max_output_bytes` 时，系统 SHALL 截断输出并在 ScriptResult 中标记 truncated：

- 系统 SHALL 停止读取该流（subprocess 仍可继续写但被丢弃）
- 截断后 `ScriptResult.truncated` SHALL = True
- stdout / stderr 字符串长度 SHALL ≤ max_output_bytes

#### Scenario: 100KB 输出被截到 16KB
- **WHEN** script 输出 100KB，descriptor.max_output_bytes=16384
- **THEN** ScriptResult.stdout 长度 SHALL ≤ 16384；truncated SHALL = True

#### Scenario: 仅 stderr 超限
- **WHEN** stdout 1KB（不超），stderr 100KB（超）
- **THEN** stdout 完整保留；stderr 被截；truncated SHALL = True

### Requirement: 可观测 EventMsg

系统 SHALL 通过 TelemetrySink 发以下 EventMsg：

| EventMsg kind | 触发时机 | 必含字段 |
| --- | --- | --- |
| `script_execution_started` | spawn subprocess 之后 | `skill_id / script_name / language / pid` |
| `script_execution_completed` | exit_code == 0 | `skill_id / script_name / duration_ms / stdout_bytes / truncated` |
| `script_execution_failed` | exit_code != 0 且非 timeout 非 killed | `skill_id / script_name / exit_code / duration_ms / stderr_excerpt` |
| `script_execution_timeout` | 超 timeout_seconds | `skill_id / script_name / elapsed_ms` |
| `script_execution_killed` | cancel 触发 SIGKILL | `skill_id / script_name / elapsed_ms` |

#### Scenario: 成功执行发 started + completed
- **WHEN** script `echo hello` 成功 exit 0
- **THEN** EventMsg `script_execution_started` 和 `script_execution_completed` SHALL 各发出一次

